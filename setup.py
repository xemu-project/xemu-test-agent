#!/usr/bin/env python3
from setuptools import setup


__version__ = '0.0.1'


setup(name='xemutestagent',
	version=__version__,
	description='xemu Automated Test Agent',
	author='Matt Borgerson',
	author_email='contact@mborgerson.com',
	url='https://github.com/mborgerson/xemu-test',
	packages=['xemutestagent'],
	install_requires=['requests'],
	extras_require={'docker': ['docker']},
	python_requires='>=3.6'
	)
