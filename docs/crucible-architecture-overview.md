# Crucible Architecture Overview

This document provides a high-level overview of crucible's
architecture — how the major components fit together and how
data flows through the system during a benchmark run. It
serves as a starting point for understanding the framework
before diving into the detailed subsystem guides.

## What crucible does

Crucible is a container-based performance testing framework
that orchestrates benchmark execution across distributed
infrastructure, collects system-level metrics alongside
workload results, and provides unified storage and analysis
of the data.

The framework handles everything from building the container
images that benchmarks run in, to deploying engines across
Kubernetes clusters or bare metal hosts, to synchronizing
distributed workloads, to post-processing raw output into
queryable metrics.

## Major components

### The controller

The controller is the central orchestration point. It runs
inside a [container image](how-the-controller-image-works.md)
that contains all the software needed to manage a benchmark
run. Every `crucible` command executes inside this container.

The controller coordinates:
- [Image building](how-image-sourcing-works.md) for engine
  containers
- [Engine deployment](how-endpoints-work.md) across
  infrastructure targets
- [Distributed synchronization](how-roadblock-works.md)
  during execution
- [Post-processing](how-benchmark-execution-works.md) of
  raw results into metrics
- [Result storage](how-cdm-works.md) in OpenSearch

### Engines

[Engines](how-engines-work.md) are the containers, chroots,
or VMs where benchmarks and tools actually execute. Each
engine is bootstrapped from the controller, receives its
scripts at runtime, and sends collected data back when
finished. Engines don't know what deployment target they're
running on — the abstraction is transparent.

### Endpoints

[Endpoints](how-endpoints-work.md) are the deployment targets
where engines run. Crucible supports three types:
- **remotehosts** — deploys engines on bare metal or VMs via
  SSH (podman containers or chroot)
- **kube** — deploys engines as pods in Kubernetes clusters
- **osp** — deploys engines as VMs in OpenStack

### Benchmarks and tools

[Benchmarks](how-benchmark-execution-works.md) generate
workload — they're the thing being measured (fio, uperf,
cyclictest, trafficgen, etc.).

[Tools](how-tool-collection-works.md) passively collect
system data alongside benchmarks — CPU utilization, interrupt
rates, process activity, etc. (sysstat, procstat, forkstat,
etc.).

Both are independently versioned repositories in the
[multi-repo ecosystem](how-the-repo-system-works.md).

### Services

Crucible runs several [supporting services](how-services-work.md)
as containers:
- **Valkey** — backend for [roadblock](how-roadblock-works.md)
  synchronization
- **OpenSearch** — stores indexed results for
  [CDM](how-cdm-works.md) queries
- **CDM server** — query API and web dashboard for result
  analysis
- **httpd** — web UI for browsing run artifacts
- **Image-sourcing service** — builds
  [engine images](how-image-sourcing-works.md)

## Data flow during a run

A benchmark run proceeds through these stages:

### 1. Configuration

The user writes a run file specifying benchmarks, parameters,
endpoints, and tools. Parameters expand into iterations via
cartesian product — multiple parameter values produce
multiple test configurations.

### 2. Image sourcing

The [source-images-service](how-image-sourcing-works.md)
builds container images for each benchmark and tool. Images
are built incrementally using a multi-stage approach where
common layers (toolbox, roadblock, rickshaw engine) are
cached and only the benchmark-specific layer is rebuilt when
scripts change. Images are tagged by content hash for
deterministic caching.

### 3. Engine deployment

[Endpoints](how-endpoints-work.md) create
[engines](how-engines-work.md) (containers, pods, or VMs) on
the target infrastructure. Engines persist for the entire
run — they execute all iterations and samples without being
recreated.

### 4. Synchronized execution

For each iteration × sample combination, engines execute in
lockstep via [roadblock](how-roadblock-works.md)
synchronization:

- Tools start collecting (before any benchmark activity)
- Servers launch and publish service info
- Endpoints create network services (K8s Services, etc.)
- Clients discover servers and execute the workload
- Clients finish, servers stop, tools stop
- Data is archived and transferred back to the controller

### 5. Post-processing

Each benchmark's post-processing script converts raw output
into CDM-format metrics — standardized time-series data with
descriptors that define what each metric measures. Tool
post-processors do the same for collected system data. All
output goes to the `postprocess/` subdirectory within each
sample's data.

### 6. Document generation and indexing

`rickshaw-gen-docs.py` reads the post-processed metrics and
generates OpenSearch documents following the
[CDM](how-cdm-works.md) hierarchy: run → iteration → sample
→ period → metrics. These documents are bulk-indexed into
OpenSearch.

### 7. Analysis

Results are available through:
- The **CDM web dashboard** for interactive exploration
  (search, compare, deep-dive views)
- The **CLI** (`crucible get result`, `crucible get metric`)
  for command-line queries
- The **CDM REST API** for programmatic access

## The multi-repo ecosystem

Crucible is composed of 40+ independently versioned
repositories managed through the
[repo/subproject system](how-the-repo-system-works.md).
Repos are cloned to `repos/` and activated via symlinks in
`subprojects/`. This enables:

- Independent versioning and release cycles per component
- Multiple forks coexisting on the same system
- Switching between sources without re-cloning

Quarterly [releases](how-releases-work.md) create
coordinated snapshots across all repos for stable,
reproducible installations.

## Supporting infrastructure

### CI

The [CI system](how-ci-works.md) validates changes across the
multi-repo ecosystem. It uses capability-based runner matching
to route test scenarios to appropriate infrastructure, with
docs-only filtering to skip full tests for documentation
changes.

### Logging

The [logger](how-the-logger-works.md) captures all crucible
command output to a SQLite database, enabling review of past
commands without manual log file management.

### Schema validation

JSON schemas enforce configuration validity at system
boundaries. Configuration files (repos.json, services.json,
run files, rickshaw.json, workshop.json, etc.) are validated
against their schemas before use, catching errors early
before they cause runtime failures.

## Guide to the documentation

| If you want to understand... | Read... |
|------------------------------|---------|
| How to install crucible | [How the installer works](how-the-installer-works.md) |
| How to write a run file | [How run files work](how-run-files-work.md) |
| How benchmarks run | [How benchmark execution works](how-benchmark-execution-works.md) |
| How tools collect data | [How tool collection works](how-tool-collection-works.md) |
| How engines operate inside | [How engines work](how-engines-work.md) |
| Where engines are deployed | [How endpoints work](how-endpoints-work.md) |
| How engine images are built | [How image sourcing works](how-image-sourcing-works.md) |
| How engines are synchronized | [How roadblock works](how-roadblock-works.md) |
| How results are stored and queried | [How CDM works](how-cdm-works.md) |
| How supporting services run | [How services work](how-services-work.md) |
| What the controller container is | [How the controller image works](how-the-controller-image-works.md) |
| How repos are managed | [How the repo system works](how-the-repo-system-works.md) |
| How releases work | [How releases work](how-releases-work.md) |
| How CI validates changes | [How CI works](how-ci-works.md) |
| How command output is captured | [How the logger works](how-the-logger-works.md) |
| How to write a new benchmark | [Implementing a new benchmark](implementing-a-new-benchmark.md) |
| How to write a new tool | [Implementing a new tool](implementing-a-new-tool.md) |
