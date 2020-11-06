
The following example is used to run the uperf benchmark. primarily between pods on different worker nodes in a k8s cluster.  In run.sh, several variables need to be defined to execute this test; please see the run.sh script for details.  To run this test (once the variable assignments are complete), simply execute ./run.sh.  Output should look something like:

<pre>
[root@dhcp31-123 uperf-Npods]# ./run.sh
$workers: worker000
${workers[@]}: worker000 worker001 worker002 worker003
${#workers[@]}: 4
--endpoint k8s,user:kni,host:e23-h24-b01-fc640.rdu2.scalelab.redhat.com,client:1-2,server:1-2,nodeSelector:client-1:/var/lib/crucible/uperf-Npods/nodeSelector-worker000.json,nodeSelector:server-1:/var/lib/crucible/uperf-Npods/nodeSelector-worker001.json,nodeSelector:client-2:/var/lib/crucible/uperf-Npods/nodeSelector-worker002.json,nodeSelector:server-2:/var/lib/crucible/uperf-Npods/nodeSelector-worker003.json,userenv:centos8,resources:/var/lib/crucible/uperf-Npods/resource_61000m.json
arg 0: --tags
Checking for redis
...appears to be running
Checking for httpd
...appears to be running
Checking for elasticsearch
...appears to be running
podman run --pull always --name crucible-run -t --rm -e PS1=\[\033[01;31m\]crucible-container\[\033[0m\][\u@\h:\W]$ -e CRUCIBLE_HOME=/home/atheurer/repos/crucible --mount=type=bind,source=/var/lib/containers,destination=/var/lib/containers --mount=type=bind,source=/root,destination=/root --mount=type=bind,source=/home,destination=/home --mount=type=bind,source=/var/lib/crucible,destination=/var/lib/crucible --mount=type=bind,source=/home/atheurer/repos/crucible,destination=/home/atheurer/repos/crucible --privileged --ipc=host --pid=host --net=host --security-opt=label=disable --workdir=/var/lib/crucible/uperf-Npods -i -e RS_REG_REPO=quay.io/crucible/client-server -e RS_REG_AUTH=/root/.docker/crucible-client-server.json -e RS_EMAIL=my@name.net -e RS_NAME="My Name" quay.io/crucible/controller:latest /home/atheurer/repos/crucible/subprojects/core/rickshaw/rickshaw-run      --tool-params tool-params.json      --bench-params bench-params.json      --bench-dir /home/atheurer/repos/crucible/subprojects/benchmarks/uperf      --roadblock-dir=/home/atheurer/repos/crucible/subprojects/core/roadblock      --workshop-dir=/home/atheurer/repos/crucible/subprojects/core/workshop      --tools-dir=/home/atheurer/repos/crucible/subprojects/tools      --base-run-dir=/var/lib/crucible/run/uperf-2020-11-06_15:26:39--sdn:OVNKubernetes,mtu:8900,rcos:45.82.202009181447-0,kernel:4.18.0-193.23.1.el8_2.x86_64,irq:bal,topo:internode,pods-per-worker:1,worker_pairs:2      --tags sdn:OVNKubernetes,mtu:8900,rcos:45.82.202009181447-0,kernel:4.18.0-193.23.1.el8_2.x86_64,irq:bal,topo:internode,pods-per-worker:1,worker_pairs:2 --num-samples=1 --endpoint k8s,user:kni,host:e23-h24-b01-fc640.rdu2.scalelab.redhat.com,client:1-2,server:1-2,nodeSelector:client-1:/var/lib/crucible/uperf-Npods/nodeSelector-worker000.json,nodeSelector:server-1:/var/lib/crucible/uperf-Npods/nodeSelector-worker001.json,nodeSelector:client-2:/var/lib/crucible/uperf-Npods/nodeSelector-worker002.json,nodeSelector:server-2:/var/lib/crucible/uperf-Npods/nodeSelector-worker003.json,userenv:centos8,resources:/var/lib/crucible/uperf-Npods/resource_61000m.json
Trying to pull quay.io/crucible/controller:latest...
Getting image source signatures
Copying blob 5ddc8ac96272 skipped: already exists
Copying blob f0eaf3569a5e skipped: already exists
Copying blob a3ed95caeb02 done
Copying blob a3ed95caeb02 done
Writing manifest to image destination
Storing signatures
Found envornment variable: RS_NAME, assigning ""My Name"" to name
Found envornment variable: RS_EMAIL, assigning "my@name.net" to email
Found envornment variable: RS_REG_AUTH, assigning "/root/.docker/crucible-client-server.json" to reg-auth
Found envornment variable: RS_REG_REPO, assigning "quay.io/crucible/client-server" to reg-repo
Preparing to run uperf
Base run directory: [/var/lib/crucible/run/uperf-2020-11-06_15:26:39--sdn:OVNKubernetes,mtu:8900,rcos:45.82.202009181447-0,kernel:4.18.0-193.23.1.el8_2.x86_64,irq:bal,topo:internode,pods-per-worker:1,worker_pairs:2]
Bench helper subproject directory: [/home/atheurer/repos/crucible/repos/git@github.com:atheurer/bench-uperf]
Confirming the endpoints will satisfy the benchmark-client and benchmark-server requirements
Clients/servers for endpoint k8s-1 will have userenv centos8
There will be 2 client(s) and server(s)
Building test execution order
Image was found at quay.io/crucible/client-server:0fb91aed07d976ae96001b635b0762e7:
Deploying endpoints
Going to run endpoint command:
./k8s --endpoint-opts=user:kni,host:e23-h24-b01-fc640.rdu2.scalelab.redhat.com,client:1-2,server:1-2,nodeSelector:client-1:/var/lib/crucible/uperf-Npods/nodeSelector-worker000.json,nodeSelector:server-1:/var/lib/crucible/uperf-Npods/nodeSelector-worker001.json,nodeSelector:client-2:/var/lib/crucible/uperf-Npods/nodeSelector-worker002.json,nodeSelector:server-2:/var/lib/crucible/uperf-Npods/nodeSelector-worker003.json,userenv:centos8,resources:/var/lib/crucible/uperf-Npods/resource_61000m.json --endpoint-label=k8s-1 --run-id=60F64F88-206E-11EB-A440-D367F645F82D --base-run-dir=/var/lib/crucible/run/uperf-2020-11-06_15:26:39--sdn:OVNKubernetes,mtu:8900,rcos:45.82.202009181447-0,kernel:4.18.0-193.23.1.el8_2.x86_64,irq:bal,topo:internode,pods-per-worker:1,worker_pairs:2 --image=quay.io/crucible/client-server:0fb91aed07d976ae96001b635b0762e7 --roadblock-server=dhcp31-123.perf.lab.eng.bos.redhat.com --roadblock-id=60F64F88-206E-11EB-A440-D367F645F82D --roadblock-passwd=flubber >/var/lib/crucible/run/uperf-2020-11-06_15:26:39--sdn:OVNKubernetes,mtu:8900,rcos:45.82.202009181447-0,kernel:4.18.0-193.23.1.el8_2.x86_64,irq:bal,topo:internode,pods-per-worker:1,worker_pairs:2/run/endpoint//k8s-1/endpoint-stdout.txt 2>/var/lib/crucible/run/uperf-2020-11-06_15:26:39--sdn:OVNKubernetes,mtu:8900,rcos:45.82.202009181447-0,kernel:4.18.0-193.23.1.el8_2.x86_64,irq:bal,topo:internode,pods-per-worker:1,worker_pairs:2/run/endpoint//k8s-1/endpoint-stderr.txt

Roadblock: Fri Nov  6 20:26:44 UTC 2020 role: leader attempt number: 1 uuid: 1:60F64F88-206E-11EB-A440-D367F645F82D:endpoint-deploy
Roadblock: Fri Nov  6 20:27:42 UTC 2020 role: leader attempt number: 1 uuid: 1:60F64F88-206E-11EB-A440-D367F645F82D:client-server-script-start
Roadblock: Fri Nov  6 20:27:46 UTC 2020 role: leader attempt number: 1 uuid: 1:60F64F88-206E-11EB-A440-D367F645F82D:client-server-start-tools
Iteration 1 sample 1 attempt number 1
Roadblock: Fri Nov  6 20:27:56 UTC 2020 role: leader attempt number: 1 uuid: 1:60F64F88-206E-11EB-A440-D367F645F82D:1-1-1:server-start
Roadblock: Fri Nov  6 20:27:58 UTC 2020 role: leader attempt number: 1 uuid: 1:60F64F88-206E-11EB-A440-D367F645F82D:1-1-1:endpoint-start
Roadblock: Fri Nov  6 20:28:09 UTC 2020 role: leader attempt number: 1 uuid: 1:60F64F88-206E-11EB-A440-D367F645F82D:1-1-1:client-start
found new timeout value: 240
Assigning new timeout with padding for next roadblock: 240
Roadblock: Fri Nov  6 20:28:32 UTC 2020 role: leader attempt number: 1 uuid: 1:60F64F88-206E-11EB-A440-D367F645F82D:1-1-1:client-stop
Roadblock: Fri Nov  6 20:30:36 UTC 2020 role: leader attempt number: 1 uuid: 1:60F64F88-206E-11EB-A440-D367F645F82D:1-1-1:endpoint-stop
Roadblock: Fri Nov  6 20:30:38 UTC 2020 role: leader attempt number: 1 uuid: 1:60F64F88-206E-11EB-A440-D367F645F82D:1-1-1:server-stop
Sample 1 completed successfully with 0 failed attempts (0 total sample failures for this iteration)
Roadblock: Fri Nov  6 20:30:45 UTC 2020 role: leader attempt number: 1 uuid: 1:60F64F88-206E-11EB-A440-D367F645F82D:client-server-stop-tools
Roadblock: Fri Nov  6 20:30:49 UTC 2020 role: leader attempt number: 1 uuid: 1:60F64F88-206E-11EB-A440-D367F645F82D:client-server-send-data
Roadblock: Fri Nov  6 20:32:16 UTC 2020 role: leader attempt number: 1 uuid: 1:60F64F88-206E-11EB-A440-D367F645F82D:client-server-script-finish
Roadblock: Fri Nov  6 20:33:11 UTC 2020 role: leader attempt number: 1 uuid: 1:60F64F88-206E-11EB-A440-D367F645F82D:endpoint-move-data
Roadblock: Fri Nov  6 20:33:32 UTC 2020 role: leader attempt number: 1 uuid: 1:60F64F88-206E-11EB-A440-D367F645F82D:endpoint-finish
Roadblock: Fri Nov  6 20:33:34 UTC 2020 role: leader attempt number: 1 uuid: 1:60F64F88-206E-11EB-A440-D367F645F82D:endpoint-really-finish
Moving per-client/server/tool data into common iterations and tool-data directories
Trying to pull quay.io/crucible/controller:latest...
Getting image source signatures
Copying blob 5ddc8ac96272 skipped: already exists
Copying blob f0eaf3569a5e skipped: already exists
Copying blob a3ed95caeb02 done
Copying blob a3ed95caeb02 done
Writing manifest to image destination
Storing signatures
opening /var/lib/crucible/run/uperf-2020-11-06_15:26:39--sdn:OVNKubernetes,mtu:8900,rcos:45.82.202009181447-0,kernel:4.18.0-193.23.1.el8_2.x86_64,irq:bal,topo:internode,pods-per-worker:1,worker_pairs:2/run/rickshaw-run.json
Launching a post-process job for each iteration x sample x [client|server] for uperf
Waiting for 4 post-processing jobs to complete
Post-processing complete
Trying to pull quay.io/crucible/controller:latest...
Getting image source signatures
Copying blob 5ddc8ac96272 skipped: already exists
Copying blob f0eaf3569a5e skipped: already exists
Copying blob a3ed95caeb02 done
Copying blob a3ed95caeb02 done
Writing manifest to image destination
Storing signatures
opening /var/lib/crucible/run/uperf-2020-11-06_15:26:39--sdn:OVNKubernetes,mtu:8900,rcos:45.82.202009181447-0,kernel:4.18.0-193.23.1.el8_2.x86_64,irq:bal,topo:internode,pods-per-worker:1,worker_pairs:2/run/rickshaw-run.json
Launching a post-process job for each tool * each collector
Waiting for 28 post-processing jobs to complete
Post-processing complete
Trying to pull quay.io/crucible/controller:latest...
Getting image source signatures
Copying blob 5ddc8ac96272 skipped: already exists
Copying blob f0eaf3569a5e skipped: already exists
Copying blob a3ed95caeb02 done
Copying blob a3ed95caeb02 done
Writing manifest to image destination
Storing signatures
opening /var/lib/crucible/run/uperf-2020-11-06_15:26:39--sdn:OVNKubernetes,mtu:8900,rcos:45.82.202009181447-0,kernel:4.18.0-193.23.1.el8_2.x86_64,irq:bal,topo:internode,pods-per-worker:1,worker_pairs:2/run/rickshaw-run.json
Waiting for consolidation of per-client/server data into sample data to complete
Consolidation complete
Benchmark result is in /var/lib/crucible/run/uperf-2020-11-06_15:26:39--sdn:OVNKubernetes,mtu:8900,rcos:45.82.202009181447-0,kernel:4.18.0-193.23.1.el8_2.x86_64,irq:bal,topo:internode,pods-per-worker:1,worker_pairs:2
Trying to pull quay.io/crucible/controller:latest...
Getting image source signatures
Copying blob 5ddc8ac96272 skipped: already exists
Copying blob f0eaf3569a5e skipped: already exists
Copying blob a3ed95caeb02 done
Copying blob a3ed95caeb02 done
Writing manifest to image destination
Storing signatures
/home/atheurer/repos/crucible/subprojects/core/CommonDataModel/templates /var/lib/crucible/uperf-Npods
/var/lib/crucible/uperf-Npods
Trying to pull quay.io/crucible/controller:latest...
Getting image source signatures
Copying blob 5ddc8ac96272 skipped: already exists
Copying blob f0eaf3569a5e skipped: already exists
Copying blob a3ed95caeb02 done
Copying blob a3ed95caeb02 done
Writing manifest to image destination
Storing signatures
Opening /var/lib/crucible/run/uperf-2020-11-06_15:26:39--sdn:OVNKubernetes,mtu:8900,rcos:45.82.202009181447-0,kernel:4.18.0-193.23.1.el8_2.x86_64,irq:bal,topo:internode,pods-per-worker:1,worker_pairs:2/run/rickshaw-result.json
Exporting from /var/lib/crucible/run/uperf-2020-11-06_15:26:39--sdn:OVNKubernetes,mtu:8900,rcos:45.82.202009181447-0,kernel:4.18.0-193.23.1.el8_2.x86_64,irq:bal,topo:internode,pods-per-worker:1,worker_pairs:2/run/rickshaw-result.json to elasticsearch documents and POSTing to localhost:9200
Run ID: 60F64F88-206E-11EB-A440-D367F645F82D
Working on tool data
Working on iteration 1
Export to CDM complete
Benchmark result now in elastic, localhost:9200
[root@dhcp31-123 uperf-Npods]#
</pre>

At this point, if the run completed sucessfully, you can query for the results.  The results query is not yet available via the crucible command, so you must use the subproject, CommonDataModel, direclty.  You will need the Run ID from the above output as well.  To use the subproject directly, start a crucible-controller container with /bin/bash, change directory to that subproject, and run this command:

<pre>
[root@dhcp31-123 uperf-Npods]# crucible wrapper /bin/bash
Trying to pull quay.io/crucible/controller:latest...
Getting image source signatures
Copying blob 5ddc8ac96272 skipped: already exists
Copying blob f0eaf3569a5e skipped: already exists
Copying blob a3ed95caeb02 done
Copying blob a3ed95caeb02 done
Writing manifest to image destination
Storing signatures
crucible-container[root@dhcp31-123:~]$cd $CRUCIBLE_HOME
crucible-container[root@dhcp31-123:crucible]$cd subprojects/core/CommonDataModel/
crucible-container[root@dhcp31-123:CommonDataModel]$cd queries/cdmq/
crucible-container[root@dhcp31-123:cdmq]$./get-result-summary.sh --run-id 60F64F88-206E-11EB-A440-D367F645F82D

run-id: 60F64F88-206E-11EB-A440-D367F645F82D
  benchmark: uperf
  tags: sdn:OVNKubernetes,mtu:8900,rcos:45.82.202009181447-0,kernel:4.18.0-193.23.1.el8_2.x86_64,irq:bal,topo:internode,pods-per-worker:1,worker_pairs:2
  metrics:
    mpstat:  Busy-CPU  NonBusy-CPU
    sar:  L2-Gbps
    uperf:  Gbps  round-trip-usec  transactions-sec
  iteration: 7A03E3CC-206F-11EB-8451-437BF645F82D
    parameters:  duration=120  nthreads=64  protocol=tcp  rsize=1024  server-ifname=eth0  test-type=rr  wsize=64
    benchmark samples:  1063000.000000  mean: 1063000.0000 transactions-sec
  Elapsed time to generate this summary: 9.25
crucible-container[root@dhcp31-123:cdmq]$
</pre>

From the above you can see that there was 1 iteration, with 1 sample, and that result was 1063000.0000 transactions-sec

