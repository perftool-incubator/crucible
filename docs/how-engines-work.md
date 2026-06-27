# How Engines Work

This document explains how crucible's engines operate — the
runtime environments where benchmark and tool scripts actually
execute. It covers the bootstrap process, execution phases,
script delivery, CPU partitioning, and data archival.

For how endpoints deploy engines, see
[how-endpoints-work.md](how-endpoints-work.md).
For the benchmark execution flow within engines, see
[how-benchmark-execution-works.md](how-benchmark-execution-works.md).
For tool collection within engines, see
[how-tool-collection-works.md](how-tool-collection-works.md).

## Overview

An engine is a container, chroot, or VM where benchmark and
tool scripts run. Engines are deployed by endpoints, bootstrapped
from the controller, synchronized via roadblock, and persist
for the entire run (all iterations and samples).

A key design property: **engines don't know what endpoint type
deployed them.** Whether running inside a podman container, a
Kubernetes pod, or an OpenStack VM, the engine sees the same
environment and follows the same execution flow. This
abstraction is what allows the same benchmark to run identically
across different deployment targets.

Each engine has an identity defined by its **cs-label** — a
string like `client-1`, `server-2`, or
`profiler-kube-1-sysstat-1` that identifies its role and ID
within the run.

## Bootstrap

When an engine starts, the first thing it runs is the bootstrap
script. Bootstrap is **the one script that is baked into the
engine container image** — it has to be, because nothing else
is available yet. The engine has no connection to the controller
and no other scripts at this point. Bootstrap's sole purpose is
to establish that connection and fetch everything else.

This is a deliberate minimal footprint: only the bootstrap
logic is embedded in the image, keeping the image stable and
cacheable. All other scripts (engine main script, benchmark
scripts, tool scripts, roadblock client) are fetched from the
controller at runtime, enabling modification without image
rebuilds.

### What bootstrap does

1. **Receives identity**: The engine's cs-label, role, and ID
   are passed via environment variables set by the endpoint.

2. **CPU partitioning discovery**: If cpu-partitioning is
   enabled, runs `discover-cpu-partitioning.py` to detect which
   CPUs are isolated for workload use and which are available
   for housekeeping (see [CPU partitioning](#cpu-partitioning)
   below).

3. **Sets up SSH keys**: Creates an SSH private key from an
   injected environment variable. This key enables secure file
   transfer from the controller throughout the run.

4. **Fetches engine scripts**: Uses paramiko SFTP to retrieve
   the main execution scripts from the controller:
   - `engine.py` — the main execution driver
   - `engine_lib.py` — the engine library
   - `roadblocker.py` — the roadblock synchronization client
   - `rickshaw-settings.json.xz` — run configuration

5. **Hands off to engine.py**: Once initialization is complete,
   bootstrap execs `python3 engine.py` which drives the rest
   of the engine's lifecycle.

## Execution phases

After bootstrap, the engine proceeds through a series of
roadblock-synchronized phases. Each phase has begin and end
barriers that all engines in the run must reach before
execution continues. This ensures tight coordination across
distributed engines.

### Phase sequence

1. **engine-init** — The engine signals readiness and receives
   any environment variables injected by the controller. The
   full environment is captured to `engine-env.txt` for later
   use during post-processing.

2. **get-data** — The engine fetches everything it needs to
   execute:
   - Benchmark and tool scripts via the files-from-controller
     mechanism
   - Benchmark command files (start, stop, runtime, infra)
   - Tool command files (start and stop JSON)

3. **collect-sysinfo** — System profiling via packrat. Captures
   hardware and software inventory (CPU topology, memory,
   kernel version, installed packages, etc.) to the `sysinfo/`
   directory.

4. **start-tools** — Tool collectors launch in the background
   (see [Tool command execution](#tool-command-execution)
   below). Tools start before any benchmark activity.

5. **process_bench_roadblocks** — The main benchmark execution
   loop. For each iteration and sample, the engine participates
   in the synchronized sequence: infra-start, server-start,
   endpoint-start, client-start, client-stop, endpoint-stop,
   server-stop, infra-stop. This is where the actual workload
   runs.

6. **stop-tools** — Tool collectors are shut down after all
   benchmark iterations complete.

7. **send-data** — The engine archives its entire working
   directory and transfers it back to the controller via SSH.

## Files-from-controller

Benchmark and tool scripts are **not baked into the container
image**. Instead, they are copied from the controller to each
engine at runtime during the get-data phase. This is the
files-from-controller mechanism defined in each benchmark and
tool's `rickshaw.json`.

### How it works

Each benchmark's `rickshaw.json` declares which files to copy:

```json
"files-from-controller": [
    { "src": "%bench-dir%/mybench-client", "dest": "/usr/bin/" },
    { "src": "%bench-dir%/mybench-base", "dest": "/usr/bin/" },
    { "src": "%run-dir%/config.yaml", "dest": "." }
]
```

The `%bench-dir%`, `%run-dir%`, and `%tool-dir%` placeholders
are resolved by rickshaw to actual paths on the controller.
During get-data, the engine fetches each file via SCP.

### Why this matters

This design enables rapid iteration without rebuilding container
images. You can modify a benchmark script on the controller,
re-run, and the engines pick up the changes immediately. This
is invaluable for development, debugging, and deep-dive
investigations that require modifying script behavior to capture
additional data or alter execution parameters.

## Working directory structure

Each engine operates in a temporary working directory (`cs_dir`)
that accumulates all data during the run:

```
cs_dir/
├── engine-env.txt              # environment snapshot
├── tool-start.json.xz          # tool start commands
├── tool-stop.json.xz           # tool stop commands
├── roadblock-msgs/             # roadblock sync logs
│   ├── engine-init-begin.json
│   ├── engine-init-end.json
│   └── ...
├── sysinfo/                    # packrat system info
├── tool-data/                  # tool collection output
│   ├── sysstat/
│   │   ├── mpstat.json.xz
│   │   ├── sar-stdout.txt.xz
│   │   └── engine-env.txt      # copy for post-processing
│   └── procstat/
│       └── ...
├── iteration-1/
│   ├── sample-1/
│   │   ├── mybench-stderrout.txt
│   │   ├── mybench-result.json
│   │   └── msgs/
│   │       ├── tx/             # outgoing messages
│   │       └── rx/             # received messages
│   └── sample-2/
│       └── ...
└── iteration-2/
    └── ...
```

Benchmark output goes into per-iteration/per-sample
directories. Tool data goes into `tool-data/` with per-tool
subdirectories. The messaging directories (`msgs/tx/` and
`msgs/rx/`) are used for client-server service discovery and
runtime timeout communication.

## Benchmark command execution

### Command format

Benchmark commands are generated by rickshaw-run.py and stored
in command files that the engine fetches during get-data. Each
command includes the iteration and sample context plus the
benchmark script with its parameters.

Parameters are passed using a Bash array mechanism:

```bash
declare -a ARGS=(
    '--duration' '60'
    '--protocol' 'tcp'
    '--nthreads' '4'
) && mybench-client "${ARGS[@]}"
```

The array preserves quoting and handles values with spaces
correctly. The engine executes the command via `invoke.run()`,
which spawns a subprocess that processes the array declaration
and runs the benchmark script. Benchmark scripts remain Bash
regardless of the engine runtime.

### Runtime discovery

Before the client benchmark runs, client engine 1 executes the
benchmark's `get-runtime` script to discover how long the
workload will take. This value is communicated via roadblock
messaging to adjust the timeout for the client-start phase.
Benchmarks that run for an indeterminate duration return `-1` to
signal unbounded execution.

### Output capture

Benchmark scripts redirect their stdout and stderr to a
`-stderrout.txt` file in the sample directory. This provides a
complete log of the benchmark's execution for debugging.

## Tool command execution

### Start/stop JSON format

Tool commands are stored as compressed JSON files:

```json
{
    "tools": [
        {
            "name": "sysstat",
            "command": "declare -a ARGS=('--interval' '3') && sysstat-start \"${ARGS[@]}\""
        }
    ]
}
```

### Execution

For each tool in the JSON:

1. The engine creates a `tool-data/<tool-name>/` subdirectory
2. Changes into that directory
3. Executes the command via `invoke.run()`
4. The tool's start script launches background processes and
   records PIDs

At stop time, the engine reads the stop JSON and executes each
tool's stop command in the same tool directory. Stop commands
run via roadblock's wait-for mechanism so tool shutdown happens
concurrently with barrier synchronization. After stopping,
`engine-env.txt` is copied into each tool's directory so the
post-processor has access to the engine's environment context.

### Profiler engines

For profiler engines (dedicated tool collection nodes), each
engine runs exactly one tool. The engine's cs-label encodes
which tool it runs (e.g., `profiler-kube-1-sysstat-1`), and
the start logic only executes the matching tool.

## CPU partitioning

CPU partitioning isolates specific CPUs for the benchmark
workload, keeping system housekeeping activity on separate
CPUs. This reduces measurement noise and timing jitter for
latency-sensitive workloads.

### How isolation is detected

The `discover-cpu-partitioning.py` script operates in two modes
depending on the runtime environment:

**Process mode** (used with chroot runtime):

The engine runs directly on the host, so it has visibility into
the kernel command line. The script reads `/proc/cmdline` and
looks for CPU isolation parameters in priority order:

1. `isolcpus=` — highest priority, explicitly isolates CPUs
   from the scheduler
2. `nohz_full=` — disables timer ticks on specified CPUs
3. `rcu_nocbs=` — moves RCU callbacks off specified CPUs

The isolated CPUs become workload CPUs; all remaining online
CPUs become housekeeping CPUs. This mode gives the most
accurate picture of the host's isolation configuration.

**Container mode** (used with podman and Kubernetes):

Inside a container, the kernel command line reflects the host
but the container may only have access to a subset of CPUs
(via cgroup cpuset). The script reads
`/proc/self/status` → `Cpus_allowed_list` to discover which
CPUs the container is permitted to use. It then designates the
first allowed CPU (and its SMT siblings) as housekeeping and
all remaining allowed CPUs as isolated workload CPUs.

This approach works regardless of the host's actual isolation
configuration — the container's cpuset is the authoritative
source of which CPUs are available to this engine.

### Multi-engine partitioning

When multiple engines share the same host, `partition-cpus.py`
divides the isolated CPUs among them. It groups CPUs into
physical cores (accounting for SMT/hyperthreading siblings) and
assigns each engine a proportional partition. Each engine
receives its partition index, ensuring no two engines compete
for the same CPUs.

### Environment variables

Two environment variables are exported for use by benchmark
scripts:

- **`HK_CPUS`**: Comma-separated list of housekeeping CPUs.
  Management threads and system activity should be pinned here.
- **`WORKLOAD_CPUS`**: Comma-separated list of workload CPUs.
  Benchmark measurement threads should be pinned here.

Benchmarks that support cpu-partitioning (cyclictest, oslat,
trafficgen) use these variables with `taskset` or tool-specific
affinity options to pin threads to the appropriate CPU sets.

## Data archival and transfer

After all phases complete, the engine archives its entire
working directory and transfers it to the controller.

### Archive process

1. The entire `cs_dir` is packaged into a compressed tarball
2. The tarball is transferred to the controller via Fabric's
   SFTP `put()` (Python engine) or piped through SSH (bash
   fallback)
3. The controller writes it to
   `run/engine/archives/<cs_label>-data.tgz`
4. Transfer retries up to 10 times with backoff on failure
5. Data transfer runs via roadblock's wait-for mechanism so
   archival happens concurrently with barrier synchronization

### What gets archived

Everything in the engine's working directory:

- Benchmark output (per-iteration/sample directories)
- Tool collection data (tool-data/ subdirectories)
- System information (sysinfo/)
- Roadblock message logs
- Environment snapshot (engine-env.txt)

The controller later extracts and reorganizes this data into
the final result directory structure.

## Engine environment

Engines receive configuration through three different
mechanisms, each suited to a different purpose:

**Bootstrap parameters** provide the engine's identity and
controller connection info — the minimum needed to get started.
How these are delivered depends on the runtime environment:

- **Chroot mode** (remotehosts): Parameters are passed as
  command-line arguments directly to the bootstrap script
  (e.g., `bootstrap --cs-label=client-1 --rickshaw-host=...`).
  A chroot doesn't have a container environment, so CLI is the
  only option.
- **Podman mode** (remotehosts): Parameters are written to an
  env file that podman loads as container environment variables.
  Bootstrap reads them from its environment instead of CLI
  arguments.
- **Kubernetes**: Parameters are set as environment variables in
  the pod spec. The kubelet injects them when the container
  starts.

Bootstrap handles both delivery methods transparently — it
checks for CLI arguments first, then falls back to environment
variables.

**Roadblock environment injection** happens during the
engine-init phase, after the engine is running and connected.
The endpoint sends additional environment variables via
roadblock messages — things like `endpoint_label`,
`hosted_by` (which node the engine landed on), `userenv`, and
`osruntime`. These are deployment context that the endpoint
only knows after the engine is created (e.g., which Kubernetes
node the pod was scheduled to) and that scripts need during
execution or post-processing.

**Command files** (the ARGS array mechanism) deliver
benchmark and tool parameters. These are fetched from the
controller during the get-data phase and contain the actual
workload configuration — what to run, with what parameters,
for each iteration and sample.

This separation keeps each mechanism focused: bootstrap
parameters for identity and connectivity, roadblock injection
for deployment context, command files for workload parameters.

### Key environment variables

Scripts running inside engines have access to these key
environment variables:

| Variable | Description |
|----------|-------------|
| `RS_CS_LABEL` | Engine identity (e.g., `client-1`, `server-2`) |
| `TOOLBOX_HOME` | Path to the toolbox shared library |
| `RICKSHAW_HOME` | Path to rickshaw scripts |
| `CRUCIBLE_HOME` | Path to the crucible installation |
| `HK_CPUS` | Housekeeping CPUs (when cpu-partitioning enabled) |
| `WORKLOAD_CPUS` | Workload CPUs (when cpu-partitioning enabled) |

The `engine-env.txt` file captures the full environment after
initialization. This file is included in each tool's data
directory and in the archive, ensuring post-processors have
access to the engine's runtime context even though they run on
the controller after the engine is gone.

## Error handling

### Roadblock abort

When an engine detects a fatal error (benchmark script returns
non-zero, prerequisite check fails), it sends an abort signal
through roadblock. All engines in the run receive the abort,
the current sample is marked as failed, and the run proceeds to
the next sample or iteration.

### Roadblock timeout

If an engine doesn't reach a roadblock barrier within the
allowed time, the roadblock times out. This is treated as a
more severe failure — the entire run is terminated because an
unresponsive engine indicates a fundamental problem.

### Sample retry

Failed samples increment a failure counter. If the counter
reaches `max-sample-failures`, the sample is permanently
failed. Failed sample directories are renamed to
`sample-N-fail-M` (where M is the attempt number) and preserved
in the result for debugging. Only samples that complete without
failures contribute to the benchmark results.
