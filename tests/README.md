# Test Design
Tests should be individually added to the tests/ dir as a bash executable
file, so users can individually run each test script as a standalone test
on their llocal environments without any special requirement.

Scripts should be named with the $GITLAB_JOB_NAME in order to keep the jobs
dry and clean. In this design the script blocks for each job may only call
an external bash script to execute tests and inline commands should be
avoided.

Example:
```
# run installer tests
./test-installer

# run trafficgen integration tests
./test-integration-trafficgen
```
