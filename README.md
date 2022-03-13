xemu Automated Testing Agent
============================

This is the agent component of the automated xemu testing system. An agent connects to the xemu testing orchestrator and waits for a package for testing. The test suite that the agents run against a package are in the [xemu-test](https://github.com/mborgerson/xemu-test) repository.

Installation
------------

First:
* Understand that **arbitrary code** may be executed on your system and take reasonable precautions.[^1]
* Coordinate with me to get an agent code to join the pool.

Then:
* Install Python 3.9+ and have it available on your `PATH`
* Install FFMPEG and have it available on your `PATH`
* Install this package via `python -m pip install https://github.com/mborgerson/xemu-test-agent/archive/refs/heads/master.zip`
* Create a directory `private` that holds your:
  * mcpx.bin
  * bios.bin

Finally the agent can be run with: `python -m xemutestagent --token abcdef --private ./private`

The agent will connect to the orchestrator and wait for work to do. When it gets a job, it will fetch the tests it needs to run, screen-record xemu as it runs, then package up the results and send it back to the orchestrator to be published.

[^1]: Packages are tested on a green-light only policy for now, so it is unlikely that malicious software will be run on your system, but it's still important to be aware of this. Don't run this on a system you care about, or in a network with sensitive targets accessible.
