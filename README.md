# crucible

[![CI Actions Status](https://github.com/perftool-incubator/crucible/workflows/CI/badge.svg)](https://github.com/perftool-incubator/crucible/actions)

## Introduction

Crucible takes [multiple performance tooling projects](config/default_subprojects) and integrates them, with the intention to provide a well rounded, portable, and highly functional end-to-end performance tool harness. If successful, crucible can be used for test automation which can execute, measure, store, visualize, and analyze the performance of various systems-under-test.

## Features

### Zero-touch Installation

Crucible should only need to be installed on a single Linux system (the crucible-controller), and the user should not have to install crucible on any target host, cluster, or cloud that they wish to test. Crucible uses container images to satisfy nearly all software dependencies. The Linux system where you installed crucible-controller only needs to have [podman](https://podman.io) software installed. Endpoints (systems, clusters, clouds that are "under-test") only need the ability to pull container images. Most clusters/clouds have this ability already, and non-clouds (like a remote-host) only need podman.

### Hybrid/Multi-cloud Capable

Crucible is designed to not just support different cloud solutions, but support testing multiple clouds at the same time, and even mixed cloud solutions in the same test. Crucible implements what are called "endpoints", and each endpoint type facilitates benchmark and tool execution on a specific type of system, cluster, or cloud. The most basic endpoint is "localhost" (benchmark/tools on the crucible-controller host), and as of this writing, there is also "remotehost" (benchmark/tools on remote host reachable via ssh) and "k8s" (a kubernetes cluster). These endpoints can be used for the same crucible-run (a set of benchmark tests), for example, a remotehost endpoint can be used with a k8s endpoint, where the remotehost endpoint is a benchmark-client and the benchmark-server is running in a kubernetes cluster. Other endpoint types are planed, like "ovirt" for testing [oVirt](https://www.ovirt.org) managed KVM clusters, "kvmhost" for testing KVM VMs from a single host via [libvirt](https://libvirt.org), and endpoints for clouds services like [ec2](https://aws.amazon.com/ec2/), [cge](https://cloud.google.com/computeGCE), and [Azure](https://azure.microsoft.com).

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

Crucible is designed to not be a large monolithic project. All function is broken out into sub-projects, and each of those sub-projects are designed to be used independently, in case another project would like to use it, or with Crucible. Crucible aggregates these sub-projects in a cohesive experience for the user. For the Crucible developer or maintainer, this add a lot of flexibility in how one develops, tests, and supports Crucible.

Because of the heavy use of containers, Crucible does not need to build any binary packages at all. The only binary product Crucible produces is the container image for the crucible-controller, and that is already provided by the maintainers of this project. Once installed, updates to Crucible can be done with git, which will be transparent to the user. Because each subproject is its own git repository, testing fixes for specific sub-projects becomes much easier to do. Only when a binary package in the crucible-container needs to be updated, does the user have to update their container image. Because Crucible builds no binary packages at all, there is no big "build-a-thon" for a new release. Fixes can be pushed incrementally by committing them to stable branches, or they can be batched together.
