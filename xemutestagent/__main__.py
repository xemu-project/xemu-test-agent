#!/usr/bin/env python3
import argparse
import os
import logging
import platform

from xemutestagent import Agent,  ContainerTestingAgent


logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', level=logging.INFO)
log = logging.getLogger(__name__)


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument('--orchestrator', default='https://ci.xemu.app', help='Testing orchestrator URL')
	ap.add_argument('--platform', default=platform.system().lower(), help='Platform tag (e.g. windows)')
	ap.add_argument('--token', help='Agent authorization token')
	ap.add_argument('--private', required=True, help='Path to private data files (e.g. ROMs)')
	ap.add_argument('--docker', action='store_true', help='Use agent test container')
	ap.add_argument('--dont-verify-cert', default=False, action='store_true', help="Don't verify orchestrator SSL cert")
	args = ap.parse_args()

	token = args.token
	if token is None:
		token = os.getenv('AGENT_TOKEN')
	if token is None:
		log.error('Agent authorization token not provided. Specify --token or set AGENT_TOKEN environment variable.')
		exit(1)

	agent_cls = ContainerTestingAgent if args.docker else Agent
	agent = agent_cls(args.orchestrator, token, args.platform, os.path.abspath(args.private), not args.dont_verify_cert)
	agent.run()


if __name__ == '__main__':
	main()
