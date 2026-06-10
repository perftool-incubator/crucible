# How Benchmark Execution Works

This document explains how crucible orchestrates benchmark execution
— from parameter configuration through engine deployment,
synchronized workload execution, data collection, and result
generation. It covers the conceptual model and data flow rather
than how to write a benchmark.

For instructions on creating a new benchmark, see
[implementing-a-new-benchmark.md](implementing-a-new-benchmark.md).
For how tool collection works alongside benchmarks, see
[how-tool-collection-works.md](how-tool-collection-works.md).

## Overview

Benchmarks are the workload generators in crucible. A run consists
of one or more benchmarks, each with parameters that expand into
**iterations**. Each iteration runs one or more **samples**
(repeated executions of the same parameter set). The orchestration
framework handles image building, engine deployment, distributed
synchronization across client and server processes, data collection,
post-processing, and result generation.

The key abstractions:

- **Iteration**: A unique combination of benchmark parameters
  (e.g., 64-byte frame size with TCP protocol)
- **Sample**: A single execution of an iteration's parameters.
  Multiple samples of the same iteration provide statistical
  confidence.
- **Period**: A named time window within a sample
  (e.g., "measurement"). Most benchmarks have a single measurement
  period, but some define warm-up or cool-down phases.
- **Primary metric**: The headline result for the benchmark
  (e.g., "ops-sec" for uperf, "latency-usec" for cyclictest)

## From run file to iterations

### Parameter specification

The run file specifies benchmark parameters using the multiplex
system. Parameters are organized into two sections:
**global-options** (reusable named groups) and **sets** (the
actual test configurations that reference global-options and add
set-specific parameters).

```json
{
    "benchmarks": [{
        "name": "uperf",
        "mv-params": {
            "global-options": [
                {
                    "name": "common",
                    "params": [
                        { "arg": "duration", "vals": ["60"] },
                        { "arg": "nthreads", "vals": ["1"] }
                    ]
                }
            ],
            "sets": [
                {
                    "include": "common",
                    "params": [
                        { "arg": "protocol", "vals": ["tcp"] },
                        { "arg": "wsize", "vals": ["64", "1024"] }
                    ]
                },
                {
                    "include": "common",
                    "params": [
                        { "arg": "protocol", "vals": ["udp"] },
                        { "arg": "wsize", "vals": ["256"] }
                    ]
                }
            ]
        }
    }]
}
```

Run files are validated against `rickshaw/schema/run-file.json`
before execution begins. Each benchmark's `rickshaw.json` is
validated against `rickshaw/schema/benchmark.json`. These
schemas enforce valid parameter structures, endpoint
configurations, and benchmark integration fields.

### Structure and fields

**global-options** (required): An array of named parameter groups.
Each group has:

- `name` (required): A label used by sets to reference this group
- `params` (required): Array of parameter objects

**sets** (required): An array of test configurations. Each set
has:

- `include` (optional): Name of a global-options group to inherit
  parameters from
- `params` (required): Array of parameter objects specific to this
  set
- `enabled` (optional): `"yes"` or `"no"` — disabled sets are
  skipped entirely, useful for temporarily excluding a
  configuration without deleting it

**Parameter objects** have these fields:

- `arg` (required): The parameter name passed to the benchmark
  script as `--arg`
- `vals` (required): Array of values. Multiple values cause
  cartesian expansion.
- `role` (optional): Restricts this parameter to a specific
  engine role (`"client"` or `"server"`). Parameters without a
  role apply to all engines.
- `enabled` (optional): `"yes"` or `"no"` — disabled parameters
  are excluded

### Cartesian expansion

Each set independently expands its parameters (inherited from
global-options plus its own) via cartesian product. In the example
above:

**Set 1** (TCP): `protocol` has 1 value, `wsize` has 2 values →
2 iterations:

| Iteration | protocol | wsize | duration | nthreads |
|-----------|----------|-------|----------|----------|
| 1 | tcp | 64 | 60 | 1 |
| 2 | tcp | 1024 | 60 | 1 |

**Set 2** (UDP): `protocol` has 1 value, `wsize` has 1 value →
1 iteration:

| Iteration | protocol | wsize | duration | nthreads |
|-----------|----------|-------|----------|----------|
| 3 | udp | 256 | 60 | 1 |

The total run has 3 iterations. Parameters inherited from the
`common` global-options group (`duration`, `nthreads`) apply to
all iterations from both sets.

### Role-specific parameters

For client-server benchmarks, some parameters only apply to one
role. The `role` field controls this:

```json
{ "arg": "trex-devices", "vals": ["0000:2d:00.0,0000:2d:00.1"], "role": "client" }
{ "arg": "ifname", "vals": ["default-route"], "role": "server" }
```

Parameters with `"role": "client"` are only passed to client
engines; `"role": "server"` only to server engines. Parameters
without a role field are passed to all engines.

### Samples and test order

Each iteration executes `num-samples` times (default 1). The
`test-order` setting controls the execution sequence:

- **`sample`** (default): All samples of iteration 1, then all
  samples of iteration 2, etc.
- **`iteration`**: Sample 1 of all iterations, then sample 2 of
  all iterations, etc.
- **`random`**: Randomized execution order

Random test order is useful for reducing systematic bias from
thermal effects, caching, or other time-dependent factors.

## Engine images and deployment

### Container image building

Before any benchmark can run, crucible builds container images
containing the benchmark software and its dependencies. Each
benchmark has a `workshop.json` file declaring what needs to be
installed (packages, source builds, pip modules, etc.).

The source-images-service builds these images incrementally using
a multi-stage approach. Each stage adds one layer of requirements,
and stages are cached independently. If only the benchmark-specific
requirements change, earlier stages (toolbox, roadblock, rickshaw
engine) are reused from cache.

### Split workshop files

Benchmarks with fundamentally different client and server
requirements (like trafficgen) can use separate
`client-workshop.json` and `server-workshop.json` files instead
of a single `workshop.json`. This produces different container
images for each role, reducing image size and allowing different
base images (userenvs) per role. The `--image` format includes
a role field (`bench::role::userenv::arch::image`) so each engine
gets the correct image for its role.

### Userenvs

A userenv (user environment) defines the base container image for
the engine. Userenvs are JSON files that specify the OS
distribution and version (e.g., `rhubi9`, `fedora-latest`,
`alma8`). Each benchmark's workshop requirements are layered on
top of the userenv base image.

### Engine deployment

Engines are the containers that run benchmark workloads. Depending
on the endpoint type:

- **Kubernetes**: Each engine becomes a pod, created via the K8s
  API. Client and server pods may run on different nodes, with
  optional node selectors for architecture or topology control.
- **Remotehosts**: Each engine becomes a podman container on a
  remote host, started via SSH.
- **OSP (OpenStack)**: Each engine becomes a VM instance in an
  OpenStack cluster.

Engines persist for the entire run — they are created once and
execute all iterations and samples sequentially. This avoids the
overhead of container creation per sample.

## Per-sample execution flow

Each sample follows a precisely synchronized sequence coordinated
by roadblock, a distributed barrier synchronization system backed
by valkey. Every phase has a begin and end barrier that all
participating engines must reach before execution proceeds.

### Execution sequence

For each sample, the following phases execute in order:

1. **infra-start** — Optional infrastructure setup. Used by
   benchmarks that need supporting services before the main
   workload starts (e.g., trafficgen starts a TRex traffic
   generation service).

2. **server-start** — Server engines launch their server
   processes (e.g., iperf3 in server mode, uperf master). Servers
   publish service information (IP address, ports) via the
   messaging system for clients to discover.

3. **endpoint-start** — Endpoint-specific service setup. On
   Kubernetes, this may create Service resources (ClusterIP,
   NodePort, or LoadBalancer) to expose server pods. The endpoint
   can transform the server's service information (e.g., replacing
   a pod IP with a LoadBalancer VIP).

4. **client-start** — The main benchmark execution phase:
   - Client engine 1 runs the `get-runtime` script to discover
     the expected workload duration
   - The runtime value adjusts the roadblock timeout for this
     phase (with padding for startup/teardown overhead)
   - All client engines execute the benchmark workload
   - Clients run until completion or timeout

5. **client-stop** — Clients have finished. Data is preserved.

6. **endpoint-stop** — Endpoint-specific cleanup (e.g., deleting
   Kubernetes Services).

7. **server-stop** — Server processes are shut down cleanly.

8. **infra-stop** — Infrastructure teardown.

Each phase's begin/end barriers ensure tight synchronization
across all engines, even when distributed across multiple hosts
or clusters.

## Client-server communication

Client-server benchmarks need clients to discover where servers
are running. Crucible provides a message-passing system built on
roadblock synchronization.

### How it works

1. During **server-start**, the server script writes a JSON
   message to `msgs/tx/svc` containing its IP and port(s):
   ```json
   {
       "recipient": {"type": "all", "id": "all"},
       "user-object": {
           "svc": {"ip": "10.244.1.5", "ports": [30000]}
       }
   }
   ```

2. During the roadblock synchronization between server-start-end
   and client-start-begin, messages are collected and delivered
   to all participants.

3. **Endpoints may modify the service information.** On
   Kubernetes, the endpoint creates a Service resource and
   replaces the pod IP with the Service ClusterIP, NodePort
   address, or LoadBalancer VIP. This is written as an
   `endpoint-start-end` message.

4. During **client-start**, the client reads service information
   from `msgs/rx/`, preferring `endpoint-start-end` messages
   (which have endpoint-transformed addresses) over
   `server-start-end` messages (which have raw pod IPs).

This indirection allows the same benchmark code to work across
different endpoint types without modification — the client always
reads from `msgs/rx/` and gets the appropriate address for its
deployment context.

## Controller scripts

### Pre-script

The controller pre-script runs once before any iteration begins.
It executes on the controller host (not inside engine containers)
and is used to generate shared configuration that all iterations
need.

**Example**: fio's pre-script generates job files from the run
parameters. These files are then copied to client engines via the
`files-from-controller` mechanism.

### Post-script (post-processing)

The controller post-script runs after all iterations and samples
have completed and the data has been transferred back from engines.
It executes once per iteration/sample/role combination, in the
directory containing that engine's raw output for that sample.

The post-script's job is to:
1. Read raw benchmark output (text files, JSON, etc.)
2. Parse the results into time-series metric data
3. Log each data point via the toolbox metrics API
   (`log_sample()` / `finish_samples()`)
4. Write `postprocess/post-process-data.json` declaring the
   metrics, periods, and primary metric

### Runtime script delivery

Benchmark scripts run inside engine containers but are not baked
into the container image. Instead, they are copied from the
controller to each engine at runtime via the
`files-from-controller` mechanism defined in `rickshaw.json`.
This means scripts can be modified on the controller between runs
without rebuilding images — a core design property that enables
rapid iteration during development, debugging, and deep-dive
investigations that often require modifying script behavior to
capture additional data or alter execution parameters.

## Data flow and result structure

### On the engine

During execution, each engine writes benchmark output to
per-iteration, per-sample directories:

```
iteration-1/sample-1/
├── benchmark-stderrout.txt
├── benchmark-result.json
└── msgs/
    ├── tx/svc          # outgoing messages
    └── rx/             # received messages
```

### Transfer to controller

After all iterations complete, the engine archives its entire
working directory into a compressed tarball and transfers it to
the controller via SSH. The controller extracts and reorganizes
the data:

```
run/iterations/
├── iteration-1/
│   └── sample-1/
│       ├── client/
│       │   ├── 1/              # client engine 1's output
│       │   │   ├── benchmark-stderrout.txt
│       │   │   └── postprocess/
│       │   │       ├── metric-data-0.csv.xz
│       │   │       ├── metric-data-0.json.xz
│       │   │       └── post-process-data.json
│       │   └── 2/              # client engine 2's output
│       └── server/
│           └── 1/              # server engine 1's output
└── iteration-2/
    └── sample-1/
        └── ...
```

### Post-processing

The `rickshaw-post-process-bench.py` script iterates over each
iteration/sample/role/engine-id combination. For each, it creates
the `postprocess/` subdirectory, then executes the benchmark's
post-script in that directory. On re-processing, the orchestrator
deletes and recreates `postprocess/`, providing clean artifact
isolation.

## Periods and the primary metric

### Period definition

Each sample's post-processing output declares one or more
**periods** — named time windows within the sample. The most
common pattern is a single "measurement" period covering the
benchmark's steady-state execution.

```json
{
    "primary-period": "measurement",
    "primary-metric": "ops-sec",
    "periods": [{
        "name": "measurement",
        "metric-files": ["metric-data-0"]
    }]
}
```

Some benchmarks define additional periods like "warm-up" or
"ramp-up" to distinguish transient startup behavior from
steady-state measurement.

### Period timestamps

Period begin and end timestamps are derived from the actual
metric data — the earliest `begin` and latest `end` across all
data points in that period's metric files. When multiple client
or server engines contribute to the same period, rickshaw
consolidates the timestamps to cover the full participation
window.

### Primary metric and result summary

The `primary-metric` field identifies the headline metric for
the benchmark (e.g., "Gbps" for iperf, "ops-sec" for uperf,
"wakeup-latency-usec" for cyclictest). Combined with
`primary-period`, this tells the result system which metric
value to extract as the official result for each iteration.

Crucible generates a result summary showing the primary metric
value for each iteration, providing a quick overview of
benchmark performance across parameter combinations.

## Sample pass/fail and retry

### Failure detection

A sample can fail for several reasons:

- **Roadblock timeout**: An engine didn't reach a synchronization
  barrier within the allowed time (the benchmark hung or crashed)
- **Script failure**: The benchmark start script returned a
  non-zero exit code
- **Abort signal**: An engine sent a `follower-ready-abort`
  message during a roadblock (typically because the benchmark
  detected an error condition)

### Retry behavior

The `max-sample-failures` setting (default 1) controls how many
failed attempts are allowed before the sample is considered
permanently failed. With the default of 1, a failed sample gets
no retries. Setting it to 2 allows one retry, and so on.

Failed samples are preserved with a modified directory name:
`sample-1-fail-1`, `sample-1-fail-2`, etc. Only a sample with
no failures is considered passing.

An iteration is considered failed if it doesn't have enough
passing samples to meet the `num-samples` requirement.

## Re-processing

### crucible postprocess

Re-runs the post-processing scripts on existing run data without
re-executing the benchmarks. This is useful when:

- A post-processor bug was fixed
- The metrics extraction logic was improved
- You want to re-generate metrics with updated toolbox code

The orchestrator cleans the `postprocess/` directory and any
stale root-level artifacts before re-running, ensuring a clean
slate.

### crucible index

Re-indexes the post-processed results into OpenSearch. This reads
`postprocess/post-process-data.json` and the associated metric
files, generates CDM documents, and pushes them to the configured
OpenSearch instance. Useful after re-processing or when switching
to a different OpenSearch instance.

## Multi-benchmark runs

Crucible supports running multiple benchmarks in a single run.
Each benchmark occupies its own set of engine IDs, and the
orchestration framework manages them together.

### Configuration

Multiple benchmarks are listed as separate entries in the run
file's `benchmarks` array. Each benchmark specifies its own
`ids` field to assign engine IDs, and its own `mv-params` for
parameter configuration:

```json
{
    "benchmarks": [
        {
            "name": "iperf",
            "ids": "1",
            "mv-params": {
                "global-options": [{ "name": "common", "params": [...] }],
                "sets": [{ "include": "common", "params": [...] }]
            }
        },
        {
            "name": "uperf",
            "ids": "2",
            "mv-params": {
                "global-options": [{ "name": "common", "params": [...] }],
                "sets": [{ "include": "common", "params": [...] }]
            }
        }
    ]
}
```

The `ids` field assigns engine IDs to each benchmark — engine 1
runs iperf and engine 2 runs uperf. The endpoint configuration
references these same IDs to determine which engines run on which
hosts and with what settings.

### Shared execution

All benchmarks share the same iteration/sample execution loop,
roadblock synchronization, and tool collection. Benchmark-specific
parameters are filtered per-engine so each engine only receives
parameters for its assigned benchmark. Tools collect data
continuously across all benchmarks, providing a unified system
view.

This enables comparative testing — running two different
benchmarks under identical conditions (same system, same tools,
same time window) to compare their behavior.

### Aligning workloads

One complexity of multi-benchmark runs is ensuring the workloads
are aligned with each other. Because all benchmarks in a run
share the same roadblock synchronization, a sample doesn't
complete until all clients finish. If one benchmark runs for 60
seconds and another for 300 seconds, the shorter benchmark's
engines sit idle waiting for the longer one to finish. This means
their tool data windows and measurement periods won't align
cleanly.

For meaningful comparative testing, configure all benchmarks to
use the same duration or runtime so their measurement periods
overlap. This ensures tool data captured during the sample
reflects all workloads running simultaneously rather than one
workload running while the other has already stopped.

## Result directory structure

The complete result directory for a benchmark run:

```
<run-dir>/
├── config/
│   ├── rickshaw-run.json          # full run configuration
│   ├── run-file.json              # original user run file
│   ├── bench-params/              # expanded parameter sets
│   ├── engine/
│   │   └── bench-cmds/            # per-engine benchmark commands
│   │       ├── client/1/start     # client 1 start commands
│   │       ├── client/1/runtime   # client 1 runtime script
│   │       ├── server/1/start     # server 1 start commands
│   │       └── server/1/stop      # server 1 stop commands
│   └── image-source-*.json.xz     # image sourcing records
├── run/
│   ├── iterations/
│   │   ├── iteration-1/
│   │   │   ├── sample-1/
│   │   │   │   ├── client/
│   │   │   │   │   └── 1/
│   │   │   │   │       ├── uperf-stderrout.txt    # raw output
│   │   │   │   │       └── postprocess/           # CDM metrics
│   │   │   │   │           ├── metric-data-0.csv.xz
│   │   │   │   │           ├── metric-data-0.json.xz
│   │   │   │   │           └── post-process-data.json
│   │   │   │   └── server/
│   │   │   │       └── 1/
│   │   │   └── sample-1-fail-1/   # failed attempt (if any)
│   │   └── iteration-2/
│   │       └── ...
│   ├── tool-data/                  # tool collection output
│   ├── sysinfo/                    # system information
│   └── opensearch/                 # indexed CDM documents
│       └── *-docs.ndjson
└── crucible.log.xz                 # run log
```

Each engine's output for each iteration/sample combination lives
in its own directory. The `postprocess/` subdirectory contains
the CDM-format metrics produced by the benchmark's post-processor.
Failed sample attempts are preserved alongside successful ones
for debugging.
