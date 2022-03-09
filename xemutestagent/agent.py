#!/usr/bin/env python3
"""
xemu Test Agent
"""

import datetime
import glob
import io
import json
import logging
import os
import platform
import requests
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from typing import Optional, Mapping
from zipfile import ZipFile

try:
    import docker
except ImportError:
    docker = None


JOB_MAX_RUNTIME_SECONDS = 300
JOB_STATUS_UPDATE_INTERVAL_SECONDS = 5

AGENT_VERSION = '0'
AGENT_PKG_URL = 'https://github.com/mborgerson/xemu-test-agent/archive/refs/heads/master.zip'
TEST_PKG_RELEASE_URL = 'https://api.github.com/repos/mborgerson/xemu-test/releases/latest'
TEST_CONTAINER_IMAGE_NAME = 'ghcr.io/mborgerson/xemu-test:master'


log = logging.getLogger(__name__)


class Job:
    """
    Work to be done by an agent on a given payload.
    """

    def __init__(self, id_: str, payload_file: tempfile.NamedTemporaryFile, created_at: datetime):
        self.id: str = id_
        self.payload: tempfile.NamedTemporaryFile = payload_file
        self.created_at: datetime.datetime = created_at
        self.state: str = 'active'
        self.conclusion: str = 'failure'
        self.last_reported_logfile_position: int = 0
        self.logfile: tempfile.NamedTemporaryFile = tempfile.NamedTemporaryFile(mode='w+', encoding='utf-8')

    def __del__(self):
        log.info('Job is deleted!')

    def __str__(self):
        s = f' id={self.id} created_at={self.created_at.isoformat()} state={self.state}'
        if self.state == 'completed':
            s += f' conclusion={self.conclusion}'
        return f'<Job{s}>'

    def get_state_update_dict(self) -> Mapping[str, str]:
        self.logfile.seek(self.last_reported_logfile_position)
        log_text = self.logfile.read()
        self.last_reported_logfile_position = self.logfile.tell()
        return {'state': self.state, 'conclusion': self.conclusion, 'log': log_text}


class Agent:
    """
    Agent that receives and executes jobs.
    """

    def __init__(self, orchestrator: str, token: str, platform: str, private: str, verify_cert: bool = True):
        self._private_dir_path = private
        self._agent_endpoint: str = orchestrator + '/agent'
        self._job_endpoint: str = orchestrator + '/job'
        self._agent_headers: Mapping[str, str] = {
            'X-XemuTest-AgentToken': token,
            'X-XemuTest-AgentPlatform': platform,
            'X-XemuTest-AgentVersion': AGENT_VERSION
        }
        self._should_run: bool = True
        self._job: Optional[Job] = None
        self._last_status_update_time: float = 0.0
        self._verify_cert = verify_cert

        self._job_results_archive_path: Optional[str] = None

    def run(self):
        while self._should_run:
            try:
                self._wait_and_execute()
            except SystemExit:
                raise
            except KeyboardInterrupt:
                raise
            except:
                log.exception('An unexpected error occured during job execution')
                time.sleep(10)

    def _update_and_restart(self):
        try:
            with tempfile.TemporaryDirectory(prefix='xemu-update-') as work_dir:
                log.info('Downloading...')
                subprocess.run([sys.executable, '-m', 'pip', 'download', '--no-cache-dir', AGENT_PKG_URL], check=True, cwd=work_dir)

                log.info('Installing...')
                subprocess.run([sys.executable, '-m', 'pip', 'install', './master.zip'], check=True, cwd=work_dir)

            log.info('Relaunching...')
            os.execv(sys.executable, [sys.executable] + ([] if sys.argv == [''] else sys.argv))
        finally:
            log.error('Failed to install update. Exiting.')
            exit(1)

    def _wait_and_execute(self):
        log.info('Waiting for job from orchestrator...')
        try:
            r = requests.get(self._agent_endpoint, headers=self._agent_headers, timeout=10, verify=self._verify_cert)
        except requests.ReadTimeout:
            # Apparently requests cannot be interrupted with Ctrl-C? Just use a timeout
            # to break out within 10s of interrupt
            return

        if r.status_code == 401:
            if r.text == 'Update Required':
                log.info('Orchestrator requires agent update')
                self._update_and_restart()
                return
            else:
                log.warning('This agent has not been authorized. Contact admin to get testing token.')
                self._should_run = False
                return
        elif r.status_code != 200:
            log.error('Unexpected response from orchestrator.')
            r.raise_for_status()

        job_id = r.headers['X-XemuTest-JobId']
        job_created_at = datetime.datetime.fromisoformat(r.headers['X-XemuTest-JobCreatedAt'])
        log.info('Received new job %s created at %s', job_id, job_created_at.isoformat())

        log.info('Receiving payload...')
        job_payload_file = tempfile.NamedTemporaryFile(prefix='job-payload-')
        for chunk in r.iter_content(chunk_size=1*1024*1024):
            job_payload_file.write(chunk)

        self.job = Job(job_id, job_payload_file, job_created_at)
        job_log_output_handler = logging.StreamHandler(self.job.logfile)
        job_log_output_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-8s %(message)s'))
        log.addHandler(job_log_output_handler)
        try:
            success = self._execute_job()
        except:
            log.exception('Unexpected error occured while processing job')
            success = False
        self.job.state = 'completed'
        self.job.conclusion = 'success' if success else 'failure'
        self._post_job_status_update()
        log.removeHandler(job_log_output_handler)
        self.job = None
        if self._job_results_archive_path:
            os.unlink(self._job_results_archive_path)
            self._job_results_archive_path = None

    def _execute_job(self) -> bool:
        log.info('Updating tester package')
        self._post_job_status_update()

        release_pkg_url = requests.get(TEST_PKG_RELEASE_URL).json()['assets'][0]['browser_download_url']
        log.info('Latest tester package is at: %s', release_pkg_url)
        subprocess.run([sys.executable, '-m', 'pip', 'install', release_pkg_url], check=True)
        log.info('Installed packages: \n%s', subprocess.check_output(['pip', 'freeze']))

        with tempfile.TemporaryDirectory(prefix='xemu-job-') as work_dir:
            success = True
            self._extract_payload(work_dir)

            results_dir_path = os.path.join(work_dir, 'results')
            os.mkdir(results_dir_path)
            job_log_file = open(os.path.join(results_dir_path, 'log.txt'), 'wb')

            try:
                log.info('Launching tester')
                now = time.time()
                start_time = now
                last_status_update_time = now

                p = subprocess.Popen([sys.executable, '-m', 'xemutest', self._private_dir_path, results_dir_path],
                                     stdout=job_log_file,
                                     stderr=subprocess.STDOUT,
                                     cwd=work_dir)

                while True:
                    poll_status = p.poll()
                    now = time.time()

                    if poll_status is not None:
                        log.info('Tester exited %d', poll_status)
                        if poll_status != 0:
                            success = False
                        break
                    if (now - start_time) > JOB_MAX_RUNTIME_SECONDS:
                        log.info('Tester exceeded maximum time. Terminating.')
                        p.kill()
                        success = False
                        break
                    if (now - last_status_update_time) > JOB_STATUS_UPDATE_INTERVAL_SECONDS:
                        self._post_job_status_update()
                        last_status_update_time = now
                    time.sleep(1)
            except:
                log.exception('Error occured while executing job!')
                success = False
            finally:
                job_log_file.close()

            self._archive_results(results_dir_path)
            return success

    def _extract_payload(self, target_dir_path: str):
        log.info('Extracting job payload')
        original_cwd = os.getcwd()
        os.chdir(target_dir_path)
        try:
            with ZipFile(self.job.payload, 'r') as zip_obj:
                zip_obj.extractall()
            log.info('Package directory listing:')
            for f in os.listdir('.'):
                log.info('- %s', f)

            log.info('Extracting build package')
            if platform.system() == 'Windows':
                release_zip = glob.glob('xemu-win-*.zip')[0]
                with ZipFile(release_zip, 'r') as zip_obj:
                    zip_obj.extractall()
                os.unlink(release_zip)
            elif platform.system() == 'Linux':
                subprocess.run(['tar', 'xf', glob.glob(f'xemu-*.tgz')[0]], check=True)
            else:
                assert False, 'Unsupported agent platform'

            log.info('Package directory listing:')
            for f in os.listdir('.'):
                log.info('- %s', f)
        finally:
            os.chdir(original_cwd)

    def _post_job_status_update(self):
        log.info('Posting job status update')
        state_dict = self.job.get_state_update_dict()
        state_file = io.BytesIO(json.dumps(state_dict).encode('utf-8'))
        files = [('state', ('state', state_file, 'application/json'))]

        if self._job_results_archive_path:
            files += [('results', ('results.tgz', open(self._job_results_archive_path, 'rb'), 'application/gzip'))]

        r = requests.post(self._job_endpoint + '/' + self.job.id, files=files, headers=self._agent_headers, verify=self._verify_cert)

    def _archive_results(self, results_dir_path: str):
        archive = tempfile.NamedTemporaryFile(prefix='xemu-results-', suffix='.tgz', delete=False)
        self._job_results_archive_path = archive.name
        archive.close()

        try:
            log.info('Generating results archive')
            with tarfile.open(self._job_results_archive_path, "w:gz") as tar:
                tar.add(results_dir_path, arcname=os.path.basename(results_dir_path))
        except:
            log.exception('Failed to create results archive')
            os.unlink(self._job_results_archive_path)
            self._job_results_archive_path = None
            raise


class ContainerTestingAgent(Agent):
    """
    Agent that receives jobs and executes in test container.
    """

    @staticmethod
    def copy_from_container(c, src: str, dst: str, **kwargs):
        subprocess.run(['docker', 'cp', f'{c.name}:{src}', dst], check=True, **kwargs)

    @staticmethod
    def copy_to_container(c, src: str, dst: str, **kwargs):
        subprocess.run(['docker', 'cp', src, f'{c.name}:{dst}'], check=True, **kwargs)

    def _execute_job(self) -> bool:
        """
        Executes current job in test container.
        """
        assert docker is not None, "Docker package not installed"
        d = docker.from_env()

        log.info('Pulling test container')
        try:
            d.images.pull(TEST_CONTAINER_IMAGE_NAME, 'master')
        except:
            log.exception('Failed to pull container')
            raise

        with tempfile.TemporaryDirectory(prefix='xemu-job-') as temp_path:
            success = True
            inputs_dir_path = os.path.join(temp_path, 'inputs')
            os.makedirs(inputs_dir_path)
            self._extract_payload(inputs_dir_path)
            shutil.copyfile(glob.glob(f'{inputs_dir_path}/xemu/*.deb')[0],
                            os.path.join(inputs_dir_path, 'xemu.deb'))

            results_dir_path = os.path.join(temp_path, 'results')
            os.makedirs(results_dir_path)

            try:
                log.info('Creating container')
                c = d.containers.create(TEST_CONTAINER_IMAGE_NAME, detach=True, auto_remove=False, network_mode='none', mem_limit=1280*1024*1024)
                self.copy_to_container(c, self._private_dir_path, '/work')
                self.copy_to_container(c, inputs_dir_path, '/work')
                self.copy_to_container(c, results_dir_path, '/work')
                c.start()
            except:
                log.exception('Failed to launch container')
                raise

            log.info('Container started. Waiting for container to exit...')
            now = time.time()
            start_time = now
            last_status_update_time = now

            while True:
                c.reload()

                now = time.time()
                if c.status != 'running':
                    exit_code = c.attrs['State']['ExitCode']
                    log.info('Container exit code: %d', exit_code)
                    if exit_code != 0:
                        success = False
                    break
                if (now - start_time) > JOB_MAX_RUNTIME_SECONDS:
                    log.info('Tester exceeded maximum time. Terminating.')
                    c.kill()
                    success = False
                    break
                if (now - last_status_update_time) > JOB_STATUS_UPDATE_INTERVAL_SECONDS:
                    self._post_job_status_update()
                    last_status_update_time = now
                time.sleep(1)

            # Pack results
            try:
                self.copy_from_container(c, '/work/results', os.path.dirname(results_dir_path))
                log.info('Saving container logs')
                with open(os.path.join(results_dir_path, 'log.txt'), 'wb') as f:
                    f.write(c.logs(timestamps=True))
            except:
                log.exception('Failed to save logs')
                success = False

            log.info('Removing container')
            c.remove()

            self._archive_results(results_dir_path)
            return success
