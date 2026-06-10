# How Tool Collection Works

This document explains how crucible's tool collection system operates
— from configuration through data collection, post-processing, and
querying. It covers the orchestration model, timing relationships,
data flow, and how the CDM (Common Data Model) system extracts
relevant subsets of tool data for analysis.

For instructions on creating a new tool, see
[implementing-a-new-tool.md](implementing-a-new-tool.md).

## Overview

Tools are passive data collectors that run in the background alongside
benchmarks. While benchmarks actively generate workload, tools observe
and record system behavior — CPU utilization, interrupt rates, process
activity, power consumption, and more.

The key design principle: **tools start before any benchmark activity
and stop after all benchmark iterations complete.** This produces a
continuous stream of data that spans the entire run. The CDM query
system then uses benchmark period timestamps to extract the relevant
subset of tool data for any given measurement window.

This design means tools don't need to know about benchmark timing,
iterations, or samples. They simply collect continuously, and the
analysis layer handles the time-based filtering.

## Collection lifecycle

A tool's lifecycle within a crucible run proceeds through these
stages:

### 1. Configuration

Crucible includes a default tool set that runs automatically if
the run file does not specify `tool-params`. The defaults are
defined in `rickshaw/config/tool-params.json`:

```json
[
    { "tool": "sysstat" },
    { "tool": "procstat" }
]
```

This means every run automatically collects CPU, memory, I/O,
and process metrics via sysstat and procstat unless the user
overrides or disables them.

Users can customize the tool set by specifying `tool-params` in
the run file. This replaces the defaults entirely — only the
tools listed in the run file will run:

```json
"tool-params": [
    {
        "tool": "sysstat",
        "params": [
            { "arg": "subtools", "val": "mpstat,sar,iostat,pidstat" },
            { "arg": "interval", "val": "3" }
        ]
    },
    {
        "tool": "procstat",
        "params": [
            { "arg": "interval", "val": "1" }
        ]
    }
]
```

Each tool entry specifies the tool name and optional parameters.
Parameters become `--key value` arguments passed to the tool's
start script. When no parameters are specified (as in the
defaults), the tool uses its own built-in defaults.

Tools can be disabled for a specific run by adding
`"enabled": "no"`.

### 2. Deployment

Rickshaw determines which nodes each tool runs on based on the
tool's `rickshaw.json` whitelist and blacklist configuration (see
[Tool deployment](#tool-deployment-where-tools-run) below). It then
generates start and stop command files for each collector type and
distributes them to the appropriate engine containers.

### 3. Startup

Tool startup is coordinated by roadblock synchronization. The
sequence within each engine is:

1. **System information collection** (packrat)
2. **`start-tools-begin` / `start-tools-end`** — all tools launch
3. **Benchmark iterations begin** — tools are already running

The `start_tools()` function in the engine creates a
`tool-data/<tool-name>/` directory for each tool, changes into it,
and executes the tool's start script. The start script typically
launches one or more background processes and records their PIDs
for later shutdown.

Tools start **before** any benchmark iteration runs. This ensures
the first measurement samples are captured even during initial
benchmark activity.

### 4. Continuous collection

While benchmarks execute their iterations and samples, tools
continue collecting in the background. The benchmark execution
loop — which may include multiple iterations, multiple samples per
iteration, server startup, client execution, and server shutdown —
runs entirely within the window that tools are active.

### 5. Shutdown

After all benchmark iterations complete:

1. **`stop-tools-begin` / `stop-tools-end`** — all tools shut down
2. Data archival and transfer

The `stop_tools()` function executes each tool's stop script, which
typically sends SIGINT to the collection processes, waits for
graceful shutdown, and compresses output files.

### 6. Data transfer

Each engine archives its entire working directory (benchmark
outputs, tool data, system info) into a compressed tarball and
transfers it back to the controller via SSH. The controller extracts
and reorganizes the data into the result directory structure (see
[Data flow](#data-flow-and-directory-structure) below).

### 7. Post-processing

The `rickshaw-post-process-tools.py` script iterates over all tool
data directories. For each collector/engine/tool combination, it
creates a `postprocess/` subdirectory, then executes the tool's
post-process script. The post-processor reads raw collection output,
converts it to CDM-format metrics using the toolbox metrics library,
and writes the results to `postprocess/`.

### 8. Document generation and indexing

`rickshaw-gen-docs.py` scans tool data directories for
`metric-data-*.json` files (in `postprocess/` or the directory
root for older runs). It generates OpenSearch NDJSON documents that
link each metric to the run's metadata (run ID, iteration, sample,
collector type, engine ID). These documents are then indexed into
OpenSearch by the CDM `add-run` process.

## Collection models

Tools use different collection strategies depending on what they
measure. There are four primary models:

### Periodic sampling

The tool runs a process that outputs data at fixed intervals.

**Example: sysstat** — launches `mpstat`, `sar`, `iostat`, and
`pidstat` as background processes, each sampling system metrics
every N seconds (configurable via `--interval`). Each subtool writes
timestamped records to its own output file until killed by SIGINT.

This model works well for metrics that change gradually (CPU
utilization, memory usage, I/O throughput) where the sampling
interval is much shorter than the phenomena being measured.

### Periodic snapshot

The tool periodically reads system state files and records
snapshots. Delta computation happens during post-processing.

**Example: procstat** — reads files from `/proc` (interrupts,
vmstat, meminfo, softnet_stat, etc.) at fixed intervals. Each
read captures a point-in-time snapshot. The post-processor computes
rates and deltas between consecutive snapshots.

This model is used when the data source provides cumulative counters
(like `/proc/interrupts`) rather than instantaneous rates.

### Event-driven

The tool captures events as they occur, with no fixed sampling
interval.

**Example: forkstat** — monitors process fork, exec, and exit
events via the kernel's process event connector. Events are logged
with timestamps as they happen, producing variable-density data
that reflects actual system activity rather than a fixed sampling
cadence.

This model captures transient events that might be missed by
periodic sampling.

### Benchmark-controlled tracing

The tool sets up tracing infrastructure but delegates activation
timing to the benchmark itself. This is used for high-volume
tracing where continuous collection would produce unmanageable
data volumes.

**Example: kernel tool (sysfs-trace subtool) + oslat** — the kernel
tool configures the ftrace subsystem (tracers, events, buffer
sizes, CPU masks) via sysfs but explicitly leaves tracing disabled
(`echo 0 > tracing_on`). The oslat benchmark binary, compiled with
a `--trace-control` patch, calls `tracing_on()` just before its
measurement loop and `tracing_off()` immediately after. The kernel
tool's stop script then collects the trace buffer contents.

This cooperative model gives highly focused precision: trace data
is collected only during the actual measurement window, avoiding
the massive data volumes that continuous tracing would produce
during warm-up, cool-down, and between iterations. The benchmark
controls exactly when the high-resolution capture occurs.

Similarly, `--trace-markers` allows benchmarks to write markers
into the ftrace buffer at key points (like latency threshold
violations), enabling precise correlation between benchmark events
and kernel trace data.

## Tool deployment: where tools run

Tools deploy to specific node types based on their `rickshaw.json`
collector configuration. Each tool defines a whitelist (where it
should run) and a blacklist (where it should not run).

### Collector types

| Type | Endpoint | Description |
|------|----------|-------------|
| `profiler` | remotehosts, kube | Dedicated profiling node |
| `compute` | osp | OpenStack compute node |
| `worker` | kube | Kubernetes worker node |
| `master` | kube | Kubernetes master node |
| `client` | remotehosts, kube, osp | Benchmark client engine |
| `server` | remotehosts, kube, osp | Benchmark server engine |

### Why tools run on profiler nodes

Most tools block `client` and `server` collector types and
whitelist only `profiler` (and sometimes `compute` for OpenStack).
This is by design:

- **Resource isolation**: Tool data collection on benchmark engine
  nodes would compete for CPU, memory, and I/O with the workload
  under test, distorting both the benchmark results and the tool
  measurements.
- **System-level perspective**: Tools like sysstat and procstat
  measure host-level metrics (CPU utilization across all cores,
  system-wide interrupt rates). Running them on a dedicated
  profiler node gives an undistorted view of the system under test.
- **Separation of concerns**: The profiler node observes the
  system; the client and server nodes run the workload.

### Multi-instance tools

A tool can be deployed multiple times with different parameters.
Each instance gets a unique tool ID and runs as a separate
collector. For example, you might run sysstat with a 1-second
interval on one profiler and a 10-second interval on another.

## Data flow and directory structure

### On the engine (during collection)

Each tool writes to its own directory within the engine's working
space:

```
tool-data/
├── sysstat/
│   ├── sysstat-start-stderrout.txt
│   ├── mpstat.json.xz
│   ├── sar-stdout.txt.xz
│   ├── iostat.json.xz
│   └── pidstat-stdout.txt.xz
├── procstat/
│   ├── procstat-start-stderrout.txt
│   └── procstat-collect-output.txt.xz
└── forkstat/
    ├── forkstat-date.txt
    └── forkstat-stderrout.txt.xz
```

### Transfer and reorganization

After collection, the engine archives everything and sends it to
the controller. The controller extracts and reorganizes by
collector type and engine ID:

```
run/tool-data/
├── profiler/
│   ├── kube-1-sysstat-1/
│   │   └── sysstat/
│   │       ├── mpstat.json.xz
│   │       ├── sar-stdout.txt.xz
│   │       └── postprocess/
│   │           ├── metric-data-sar-mem.csv.xz
│   │           ├── metric-data-sar-mem.json.xz
│   │           ├── metric-data-mpstat-0.csv.xz
│   │           ├── metric-data-mpstat-0.json.xz
│   │           └── post-process-output.txt
│   ├── kube-1-procstat-1/
│   │   └── procstat/
│   │       └── ...
│   └── kube-1-sysstat-2/
│       └── sysstat/
│           └── ...
└── client/
    └── 1/
        └── (typically empty — tools blocked on client nodes)
```

### Post-processed output

The `postprocess/` subdirectory inside each tool directory contains
the CDM-format metric files produced by the tool's post-processor.
On re-processing, the orchestrator deletes and recreates this
directory, providing clean artifact isolation without
filename-dependent cleanup.

## The relationship between tool data and benchmark periods

This is the most important conceptual piece for understanding how
tool data is used in analysis.

### Tools collect continuously; benchmarks define time windows

Tools run for the entire duration of the benchmark execution and
produce a continuous stream of timestamped data. They have no
concept of iterations, samples, or measurement periods.

Benchmarks, on the other hand, define **periods** — named time
windows like "measurement" with explicit begin and end timestamps.
These periods are determined during post-processing from the actual
benchmark output data.

### CDM queries bridge the gap

When you query the CDM for tool metrics during a specific benchmark
period, the system:

1. Looks up the period document to get its `begin` and `end`
   timestamps
2. **Removes the period from the metric query** — tool metrics are
   not attributed to any period in the database
3. Uses the period's timestamps as a time range filter on the tool
   metric data
4. Returns only the tool data points that fall within that time
   window

This means asking for "sysstat CPU utilization during the
measurement period" is really asking: "give me sysstat CPU data
between timestamp X and timestamp Y, where X and Y are the
measurement period's boundaries."

### Partial overlap handling

When a tool data point spans a period boundary (its begin-to-end
range partially overlaps the period), the CDM query system
interpolates the value proportionally based on how much of the data
point falls within the period.

### Custom time ranges

Because tool data exists as a continuous time series independent of
benchmark periods, you are not limited to querying by iteration,
sample, or period boundaries. If you know the timestamps you care
about, you can query tool data for any arbitrary time range —
including ranges that span multiple samples, cover the entire run,
or focus on a specific event you identified in the data.

For example, if you noticed a CPU utilization spike at a particular
timestamp in the sysstat data, you could query a narrow window
around that spike regardless of which benchmark period it fell in.
Or you could query the entire tool collection window to see system
behavior from before the first iteration through after the last,
including any activity during benchmark startup and teardown.

The iteration/sample/period segmentation is a convenience for the
common case — correlating tool observations with benchmark results.
But the underlying data is a flat time series, and custom timestamp
queries give you full access to it.

### Exception: benchmark-controlled collection

The "collect continuously, filter later" model has one notable
exception. For high-volume tracing tools (like the kernel tool's
sysfs-trace subtool), continuous collection would produce
unmanageable data volumes. Instead, the tool sets up the collection
infrastructure in advance but leaves it inactive. The benchmark
binary itself activates collection only during its measurement
window. In this model, the tool data inherently covers only the
measurement period because the benchmark controlled when collection
was active.

## Tool parameters

Parameters flow from the run file to the tool start script through
this path:

1. **Run file**: User specifies `tool-params` with `arg`/`val`
   pairs
2. **rickshaw-run.py**: Builds a Bash command with a declared
   `ARGS` array:
   ```bash
   declare -a ARGS=('--interval' '3' '--subtools' 'mpstat,sar')
   && sysstat-start "${ARGS[@]}"
   ```
3. **Serialization**: Commands are written to compressed JSON files
   (`tool-cmds/<collector-type>/start.json.xz`) in the run config
   directory
4. **Engine execution**: The engine deserializes the JSON and
   evaluates the command string

This indirection allows the controller to build tool commands once
and distribute them to multiple engines, with each engine's start
script receiving the same parameters.

## Result directory structure

The complete result directory for a run with tool data:

```
<run-dir>/
├── config/
│   ├── tool-cmds/
│   │   ├── profiler/
│   │   │   ├── start.json.xz    # tool start commands
│   │   │   └── stop.json.xz     # tool stop commands
│   │   ├── client/1/
│   │   │   ├── start.json.xz
│   │   │   └── stop.json.xz
│   │   └── server/1/
│   │       ├── start.json.xz
│   │       └── stop.json.xz
│   └── rickshaw-run.json         # full run configuration
├── run/
│   ├── iterations/
│   │   └── iteration-1/
│   │       └── sample-1/
│   │           ├── client/1/     # benchmark output
│   │           └── server/1/     # benchmark output
│   ├── tool-data/
│   │   └── profiler/
│   │       ├── kube-1-sysstat-1/
│   │       │   └── sysstat/
│   │       │       ├── mpstat.json.xz        # raw collection
│   │       │       ├── sar-stdout.txt.xz     # raw collection
│   │       │       └── postprocess/          # CDM metrics
│   │       │           ├── metric-data-*.csv.xz
│   │       │           └── metric-data-*.json.xz
│   │       └── kube-1-procstat-1/
│   │           └── procstat/
│   │               ├── interrupts.xz         # raw snapshots
│   │               └── postprocess/
│   │                   └── metric-data-*.json.xz
│   └── opensearch/
│       └── *-docs.ndjson         # indexed CDM documents
└── crucible.log.xz              # run log
```

The `tool-data/` subtree is organized by collector type, then
engine ID, then tool name. Each tool directory contains both the
raw collection output and the `postprocess/` subdirectory with
CDM-format metrics. The `opensearch/` directory contains the
generated NDJSON documents ready for indexing.
