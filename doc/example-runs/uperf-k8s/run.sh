#!/bin/bash
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=bash
# -*- mode: sh; indent-tabs-mode: nil; sh-basic-offset: 4 -*-

# user provides vars
host_pairs=2 # For scale-out, use a number >1
             # For internode (OCP) total workers = 2 * $host_pairs
             # For ingress (OCP) total workers = 1 * $host_pairs,
             # and total external client hosts = 1 * $host_pairs
num_cpus=61  # Number of *available* cpus on each of the workers.  TODO: this should be
             # automatically calculated.
ocphost=myk8sadminhost # The host which oc/kni commands are run
                       # Requires passwordless ssh with user kni
# The hosts below *must* have the same subnet as 'baremetal' network from OCP
# cluster if you want to do a test with 'ingress' topology
bmlhosta=hosta 
bmlhostb=hostb

userenv=centos8 # can be centos7, centos8, rhubi7, rhubi8, debian, opensuse (last two need testing)
topo="internode" # internode = between nodes in ocp/k8s cluster
         # ingress = client outside, server inside ocp/k8s cluster
         # interhost = between two BML hosts
                 # TODO: support ocp/k8s cluster "ingress", between a worker node an external host
pod_sizes="1" # space separated list of number of pod-pairs to test, like "1 16 32 64"
irq="bal" # bal by default or rrHost or <something-else> depending on what manual mods made
          # This is completely manual and needs to be confirmed by the user!
samples=1 # Ideally use at least 3 samples for each benchmark iteration.


# check for dependencies
bins="jq bc ssh sed crucible"
missing_bins=""
for bin in $bins; do
    which $bin >/dev/null 2>&1 || missing_bins="$missing_bins $bin"
done
if [ ! -z "$missing_bins" ]; then
    echo "ERROR: these required bins are needed; please install before running this script:"
    echo $missing_bins
    exit 1
fi
    

for num_pods in $pod_sizes; do

    if [ "$topo" == "internode" ]; then #between to OCP nodes
    min_worker_nodes=`echo "$host_pairs * 2" | bc`
        sed -e s/thisIfname/eth0/ bench-params.template >bench-params.json
        # Get info about the cluster first
        ssh kni@$ocphost "oc get nodes -o json" >nodes.json
        workers=(`jq -r '.items[] | .metadata.labels | select(."node-role.kubernetes.io/worker" != null) | ."kubernetes.io/hostname"' nodes.json | tr '\n' ' '`)
        echo \$workers: $workers
        echo '${workers[@]}:' ${workers[@]}
        echo '${#workers[@]}:' ${#workers[@]}
        if [ ${#workers[@]} -lt $min_worker_nodes ]; then
            echo "Need at least $min_worker_nodes to run tests, and this cluster only has ${#workers[@]}"
            exit 1
        fi
        # base config on first worker
        first_worker=`echo ${workers[0]}`
        if [ -z "$first_worker" ]; then
            "First worker not defined, exiting"
            exit 1
        fi
        ssh kni@$ocphost "oc get nodes/$first_worker -o json" >worker.json
        kernel=`jq -r .status.nodeInfo.kernelVersion worker.json`
        rcos=`jq -r .status.nodeInfo.osImage  worker.json | awk -F"CoreOS " '{print $2}' | awk '{print $1}'`
        ssh kni@$ocphost "oc get network -o json" >network.json
        network_type=`jq -r .items[0].status.networkType network.json`
        network_mtu=`jq -r .items[0].status.clusterNetworkMTU network.json`
        per_pod_cpu=`echo "1000 * $num_cpus / $num_pods" | bc`m
    
        # Create a resource JSON to size the pods
        resource_file="`/bin/pwd`/resource_$per_pod_cpu.json"
        echo '"resources": {'     >$resource_file
        echo '    "requests": {' >>$resource_file
        echo '        "cpu": "'$per_pod_cpu'"' >>$resource_file
        echo '    }' >>$resource_file
        echo '}' >>$resource_file

        ns_file[0]=""
        ns_file[1]=""
        # Create a nodeSelector JSON to place pods
        for i in `seq 1 $host_pairs`; do
            for j in 0 1; do
                idx=`echo "($i-1)*2+$j" | bc`
                this_worker=${workers[$idx]}
                ns_file[$j]=`/bin/pwd`/nodeSelector-$this_worker.json
                echo '"nodeSelector": {' >${ns_file[$j]}
                    echo '    "kubernetes.io/hostname": "'$this_worker'"' >>${ns_file[$j]}
                echo '}' >>${ns_file[$j]}
            done
            # Populate the node_selector option
            for k in `seq 1 $num_pods`; do
                cs_num=`echo "$num_pods * ($i-1) + $k" | bc`
                node_selector="$node_selector`printf "nodeSelector:client-$cs_num:\${ns_file[0]},nodeSelector:server-$cs_num:\${ns_file[1]},"`"
            done
        done
        num_clients=`echo "$num_pods * $host_pairs" | bc`
        num_servers=$num_clients
        endpoint_opt="--endpoint k8s,user:kni,host:$ocphost,client:1-$num_clients,server:1-$num_servers,${node_selector}userenv:$userenv,resources:$resource_file"
        echo $endpoint_opt

    elif [ "$topo" == "interhost" ]; then #between two baremetal hosts
        sed -e s/thisIfname/ens2f0/ bench-params.template >bench-params.json
        network_type=flat
        network_mtu=8900 #fixme
        rcos=na
        kernel=`ssh $bmlhosta uname -r`
        # TODO: this only works for $host_pairs = 1
        endpoint_opt="--endpoint remotehost,user:root,host:$bmlhosta,client:1-$num_pods,userenv:$userenv "
        endpoint_opt+="--endpoint remotehost,user:root,host:$bmlhostb,server:1-$num_pods,userenv:$userenv "

    elif [ "$topo" == "ingress" ]; then #between two baremetal hosts
    #TODO: Some of this should be consolidated with section for "internode"
    min_worker_nodes=$host_pairs
        sed -e s/thisIfname/eth0/ bench-params.template >bench-params.json
        # Get info about the cluster first
        ssh kni@$ocphost "oc get nodes -o json" >nodes.json
        workers=(`jq -r '.items[] | .metadata.labels | select(."node-role.kubernetes.io/worker" != null) | ."kubernetes.io/hostname"' nodes.json | tr '\n' ' '`)
        echo \$workers: $workers
        echo '${workers[@]}:' ${workers[@]}
        echo '${#workers[@]}:' ${#workers[@]}
        if [ ${#workers[@]} -lt $min_worker_nodes ]; then
            echo "Need at least $min_worker_nodes to run tests, and this cluster only has ${#workers[@]}"
            exit 1
        fi
        # base config on first worker
        first_worker=`echo ${workers[0]}`
        if [ -z "$first_worker" ]; then
            "First worker not defined, exiting"
            exit 1
        fi
        ssh kni@$ocphost "oc get nodes/$first_worker -o json" >worker.json
        kernel=`jq -r .status.nodeInfo.kernelVersion worker.json`
        rcos=`jq -r .status.nodeInfo.osImage  worker.json | awk -F"CoreOS " '{print $2}' | awk '{print $1}'`
        ssh kni@$ocphost "oc get network -o json" >network.json
        network_type=`jq -r .items[0].status.networkType network.json`
        network_mtu=`jq -r .items[0].status.clusterNetworkMTU network.json`
        per_pod_cpu=`echo "1000 * $num_cpus / $num_pods" | bc`m
    
        # Create a resource JSON to size the pods
        resource_file="`/bin/pwd`/resource_$per_pod_cpu.json"
        echo '"resources": {'     >$resource_file
        echo '    "requests": {' >>$resource_file
        echo '        "cpu": "'$per_pod_cpu'"' >>$resource_file
        echo '    }' >>$resource_file
        echo '}' >>$resource_file

        # Create a nodeSelector JSON to place pods
        ns_file=""
        for i in `seq 1 $min_worker_nodes`; do
            idx=`echo "$i-1" | bc`
            this_worker=${workers[$idx]}
            ns_file=`/bin/pwd`/nodeSelector-$this_worker.json
            echo '"nodeSelector": {' >$ns_file
                echo '    "kubernetes.io/hostname": "'$this_worker'"' >>$ns_file
            echo '}' >>$ns_file
            # Populate the node_selector option
            for k in `seq 1 $num_pods`; do
                cs_num=`echo "$num_pods * ($i-1) + $k" | bc`
                node_selector="$node_selector`printf "nodeSelector:server-$cs_num:\$ns_file,"`"
            done
        done
        num_clients=$num_pods
        num_servers=$num_clients
        endpoint_opt="--endpoint k8s,user:kni,host:$ocphost,server:1-$num_servers,${node_selector}userenv:$userenv,resources:$resource_file "
        endpoint_opt+="--endpoint remotehost,user:root,host:$bmlhosta,client:1-$num_pods,userenv:$userenv "
    fi

    tags="sdn:$network_type,mtu:$network_mtu,rcos:$rcos,kernel:$kernel,irq:$irq"
    tags="$tags,topo:$topo,pods-per-worker:$num_pods,worker_pairs:$worker_pairs"
    crucible run uperf --tags $tags --num-samples=$samples $endpoint_opt
done
