# crucible

[![CI Actions Status](https://github.com/perftool-incubator/crucible/workflows/CI/badge.svg)](https://github.com/perftool-incubator/crucible/actions)

## Introduction

Crucible is a performance tool harness that integrates [multiple performance tooling projects](config/default_subprojects). The project's intention is to provide a well rounded, portable, and highly functional end-to-end performance testing harness. Crucible can be used for test automation to execute, measure, store, visualize, and analyze the performance of various systems-under-test (SUT).

## Features

### Zero-touch Installation

Crucible can be installed on a single Linux system (the crucible-controller), and does not require installation on any target host, cluster, or cloud that needs  testing. Crucible uses container images to satisfy software dependencies. The crucible-controller system only needs to have [git](https://git-scm.com) and [podman](https://podman.io) software installed. Endpoints (systems, clusters, clouds that are "under-test") only need the ability to pull container images. Most clusters/clouds have this ability already, and non-clouds (like a remote-host) only need podman.

### Hybrid/Multi-cloud Capable

Crucible is designed to not just support different cloud solutions, but support testing multiple clouds at the same time, and even mixed cloud solutions in the same test. Crucible implements what are called "endpoints", and each endpoint type facilitates benchmark and tool execution on a specific type of system, cluster, or cloud. As of this writing, there are "remotehost" (benchmark/tools on remote host reachable via ssh), "k8s" (a kubernetes cluster), and "osp" (an OpenStack cluster) endpoints. These endpoints can be used for the same crucible-run (a set of benchmark tests), for example, a remotehost endpoint can be used with a k8s endpoint, where the remotehost endpoint is a benchmark-client and the benchmark-server is running in a kubernetes cluster. Other endpoint types are planned, like "ovirt" for testing [oVirt](https://www.ovirt.org) managed KVM clusters, "kvmhost" for testing KVM VMs from a single host via [libvirt](https://libvirt.org), and endpoints for clouds services like [ec2](https://aws.amazon.com/ec2/), [cge](https://cloud.google.com/computeGCE), and [Azure](https://azure.microsoft.com).

### Automatic Performance-tool Data Collection

A user can specify what tools are used, but if they don't, a base set of tools are automatically used, and because different endpoint types can have specific tool requirements, where these tools run is determined without the user having to specify. Meta-data about how tools are used for specific endpoints are kept in the tool subproject, so even the endpoints don't require intrinsic knowledge about any tool. For example, if a user wants "sar" tool data, the k8s endpoint will check the tool meta data to see where in the k8s cluster this tool needs to run, instead of the user trying to figure out where it should run.

### Universal Benchmark Execution Engine

To support having a benchmark run in many test environments (host/cluster/cloud), crucible builds a benchmark-execution-engine which is designed to support all current and future endpoint types. Regardless of target environment, the benchmark engine is designed to prepare & execute the benchmark in the exact same way. Crucible provides the benchmark-engine a container image to satisfy all software dependencies, and the endpoints determine how to make use of the container image and how to launch the benchmark engine. For example, k8s endpoint creates a pod to run the benchmark-engine, while remotehost endpoint can use podman or chroot to launch the benchmark-engine. Regardless of how the engine was launched, once running, the behavior of the benchmark execution is identical across all endpoint types.

### Flexible Benchmark User Environment

While Crucible uses a container image to satisfy the software dependencies for running the benchmark engine, what the base container image (what Crucible refers to as the user-environment or userenv) is based on is user-configurable. If you change the userenv, you as the user do not need to prepare a new container image. Crucible has the ability to aggregate many software requirements (like a specific benchmark or tool package), combine this with an underlying container base image, then dynamically build a new container image for your crucible-run. In fact, each invocation of an endpoint can use a different userenv on the same test. Crucible manages the container images for you and can share these container images across users by using the same container registry project. As users run different combinations of userenv/benchmark/tools, a rich collection of container images will be re-used among those users.

### A Common Data Model for all Benchmark Output

Crucible converts all output from benchmark execution, performance tool collection, and endpoint environment data into a Common Data Model, so advanced reporting and comparisons are possible. Data like metrics from benchmark and tools share a common format which allows the query engine to find, filter, aggregate, and report this information with the same code regardless of the benchmark or tool source. A query engine is planned that will provide access to this data from REST API calls as well as a command-line utility for CLI-based data-exploration, separating the presentation layer from the query layer.

### Distributed, Cloud-Native Result Repository

Once data is converted to Common Data Model, Crucible plans to store this data in a distributed model, where local data can be either kept local or pushed to a cloud-native infrastructure, and even if the user data for one or more runs stored locally, they can seamlessly compare this data to result data stored remotely. At any point the user can decide to migrate their local data to the remote repository (making it available for comparison by other users with access to the remote repository).

### Easy to Develop and Maintain

Crucible is designed to not be a large, monolithic project. All functionality is broken out into sub-projects, and each of those sub-projects are designed to be loosely coupled, in case another project would like to use one or more of them. Crucible aggregates these sub-projects in a cohesive experience for the user. For the Crucible developer or maintainer, this adds flexibility in how one develops, tests, and supports Crucible.

Through the use of containers built at runtime for software distribution (which can be shared via a cache), Crucible does not need to pre-build any binary packages that must be installed by the user. The only binary product Crucible produces is the container image for the crucible-controller, and that is already provided by the maintainers of this project. Once installed, updates to Crucible are handled via git, which will be transparent to the user. Since each sub-project is its own git repository, testing fixes for specific sub-projects becomes much easier to do because each sub-project can be individually modified and/or replaced. The crucible-controller image may be rebuilt from time to time to upgrade and/or add additional software dependencies; however, obtaining these image updates will be handled transparently when Crucible is updated. As Crucible builds no binary packages, there is no big "build-a-thon" for a new release -- in fact there is currently not a release concept. Crucible's deployment model is similar to that of a rolling release, where the latest "stable" code is always available when an installation and/or update is performed from the upstream repositories. In order to maintain stability in this fast moving development/deployment model, continuous integration testing via Github workflows is used extensively across the many sub-projects and is always being improved upon.

## Subprojects

Type/Project | Description | URL
-------------|-------------|----
**Core** | |
CommonDataModel | Schema definitions and query utilities | https://github.com/perftool-incubator/CommonDataModel
crucible-ci | Continuous Integration testing framework | https://github.com/perftool-incubator/crucible-ci
multiplex | Parameter validation and parameter combination creation | https://github.com/perftool-incubator/multiplex
packrat | System information collector | https://github.com/perftool-incubator/packrat
rickshaw | Primary orchestration component which contains the endpoint and engine code.  Also contains the userenv definitions. | https://github.com/perftool-incubator/rickshaw
roadblock | Synchronization and communication framework | https://github.com/perftool-incubator/roadblock
toolbox | Shared libraries and utility for use by other subprojects | https://github.com/perftool-incubator/toolbox
workshop | Container build utility | https://github.com/perftool-incubator/workshop
**Documentation** | |
examples | Examples of different approaches to running Crucible with the various workloads | https://github.com/perftool-incubator/crucible-examples
**Benchmarks** | |
cyclictest | Traditional RT latency measurement tool | https://github.com/perftool-incubator/bench-cyclictest
fio | Traditional block IO testing | https://github.com/perftool-incubator/bench-fio
hwlatdetect | Baremetal HW latency spike detector | https://github.com/perftool-incubator/bench-hwlatdetect
flexran | Radio Access Network (RAN) edge framework testing | https://github.com/perftool-incubator/bench-flexran
oslat | Latency measurement tool that simulates a continously polling application (ie. DPDK PMD) | https://github.com/perftool-incubator/bench-oslat
iperf | Traditional network communication testing | https://github.com/perftool-incubator/bench-iperf
tracer | Framework for Linux kernel latency tracer/workload tools (ie. osnoise & timerlat) | https://github.com/perftool-incubator/bench-tracer
trafficgen | TRex based high speed packet forwarding througput and loss analysis using binary search logic | https://github.com/perftool-incubator/bench-trafficgen
uperf | Traditional network communication testing | https://github.com/perftool-incubator/bench-uperf
**Tools** | |
forkstat | A tool to capture fork+exec statistics | https://github.com/perftool-incubator/tool-forkstat
ftrace | Linux kernel tracing | https://github.com/perftool-incubator/tool-ftrace
kernel | Various kernel tools (turbostat, perf, sst, tracing) | https://github.com/perftool-incubator/tool-kernel
ovs | Open vSwitch data collection | https://github.com/perftool-incubator/tool-ovs
procstat | Custom utilities for capturing various /proc information | https://github.com/perftool-incubator/tool-procstat
rt-trace-bpf | A eBPF based tool used to indentify preemption causers in latency sensitive workloads | https://github.com/perftool-incubator/tool-rt-trace-bpf
sysstat | Traditional Linux performance tools (sar, mpstat, iostat, etc.) | https://github.com/perftool-incubator/tool-sysstat
