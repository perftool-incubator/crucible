# Implementing a New Benchmark

This guide explains how to create a new benchmark for the crucible
performance testing framework. A benchmark is a standalone git
repository that integrates with rickshaw (the orchestrator) via a set
of JSON configuration files and executable scripts.

## Repository naming

Benchmark repos follow the naming convention `bench-<name>` (e.g.,
`bench-fio`, `bench-uperf`, `bench-sleep`). The benchmark name used
inside configuration files is just `<name>` without the `bench-`
prefix.

## Required files

Every benchmark must contain at minimum:

| File | Purpose |
|------|---------|
| `rickshaw.json` | Tells rickshaw how to run and post-process the benchmark |
| `workshop.json` | Declares container image build requirements (packages, source builds) |
| `<name>-client` | The script that executes the benchmark workload |
| `<name>-post-process` | Converts raw benchmark output into crucible metrics |
| `<name>-get-runtime` | Returns the expected duration (seconds) of a benchmark run |

## Commonly included files

| File | When needed |
|------|-------------|
| `<name>-base` | Shared initialization; sources toolbox's `bench-base` library |
| `<name>-server-start` | Client-server benchmarks: starts the server process |
| `<name>-server-stop` | Client-server benchmarks: stops the server process |
| `multiplex.json` | Parameter defaults, validation rules, and unit conversions |
| `LICENSE` | Apache 2.0 (standard across all benchmarks) |
| `README.md` | Project documentation |

---

## rickshaw.json

This is the primary integration file. It tells rickshaw what scripts
to run, what files to copy to engines, and how to post-process
results. Rickshaw validates it against `rickshaw/schema/benchmark.json`.

### Schema version

```json
{
    "rickshaw-benchmark": {
        "schema": {
            "version": "2020.05.18"
        }
    }
}
```

### Benchmark name

```json
"benchmark": "<name>"
```

This must match the directory name used in `subprojects/benchmarks/`.

### Controller section

The controller runs on the orchestrator host. It has two optional
hooks:

```json
"controller": {
    "pre-script": "%bench-dir%<name>-prepare",
    "post-script": "%bench-dir%<name>-post-process"
}
```

- **`post-script`** (required): Runs on the controller after all
  iterations and samples have completed and the data has been sent
  back to the controller. Converts raw benchmark output into crucible
  metrics.
- **`pre-script`** (optional): Runs once before any tests start.
  Useful for generating configuration files that all iterations share
  (e.g., fio uses this to prepare job files).

### Client section

Defines how the benchmark runs on client engines:

```json
"client": {
    "files-from-controller": [
        { "src": "%bench-dir%/<name>-base", "dest": "/usr/bin/" },
        { "src": "%bench-dir%/<name>-get-runtime", "dest": "/usr/bin/" },
        { "src": "%bench-dir%/<name>-client", "dest": "/usr/bin/" }
    ],
    "runtime": "<name>-get-runtime",
    "start": "<name>-client"
}
```

- **`files-from-controller`** (required): Files copied from the
  controller to each client engine at runtime, before execution
  begins. Each entry has `src` (controller path) and `dest` (engine
  path). Use `%bench-dir%` to reference the benchmark's directory on
  the controller. Use `%run-dir%` for the run directory. By copying
  these files at runtime rather than baking them into the engine
  container image, benchmark scripts can be modified on the fly
  without rebuilding images. This enables ad-hoc customization of
  scripts for testing and investigation and is a core design property
  of how crucible/rickshaw orchestrates tests.
- **`runtime`** (required): Script that outputs the expected
  benchmark duration in seconds. Used to set roadblock timeouts.
  If the workload has unbounded duration (i.e., how long it will
  run is not known in advance), the script should echo `-1` to
  signal that the workload is unbounded time-wise.
- **`start`** (required): Script that actually runs the benchmark.
- **`param_regex`** (optional): Array of sed-style regex
  transformations applied to parameter strings before passing them to
  the start script. Used when the benchmark binary has a different
  argument format than crucible's `--key=value` convention (see fio
  for an example).

### Server section (client-server benchmarks only)

```json
"server": {
    "files-from-controller": [
        { "src": "%bench-dir%/<name>-base", "dest": "/usr/bin/" },
        { "src": "%bench-dir%/<name>-server-start", "dest": "/usr/bin/" },
        { "src": "%bench-dir%/<name>-server-stop", "dest": "/usr/bin/" }
    ],
    "start": "<name>-server-start",
    "stop": "<name>-server-stop"
}
```

- **`files-from-controller`** (required): Same mechanism as
  described in the client section above. Lists the server-side
  scripts to copy from the controller at runtime.
- **`start`** (required): Starts the server process. Must
  background the server and exit 0 on success.
- **`stop`** (required): Cleanly shuts down the server process.

### Minimal example (client-only)

```json
{
    "rickshaw-benchmark": {
        "schema": { "version": "2020.05.18" }
    },
    "benchmark": "mybench",
    "controller": {
        "post-script": "%bench-dir%mybench-post-process"
    },
    "client": {
        "files-from-controller": [
            { "src": "%bench-dir%/mybench-get-runtime", "dest": "/usr/bin/" },
            { "src": "%bench-dir%/mybench-client", "dest": "/usr/bin/" }
        ],
        "runtime": "mybench-get-runtime",
        "start": "mybench-client"
    }
}
```

### Client-server example

```json
{
    "rickshaw-benchmark": {
        "schema": { "version": "2020.05.18" }
    },
    "benchmark": "mybench",
    "controller": {
        "post-script": "%bench-dir%mybench-post-process"
    },
    "client": {
        "files-from-controller": [
            { "src": "%bench-dir%/mybench-base", "dest": "/usr/bin/" },
            { "src": "%bench-dir%/mybench-get-runtime", "dest": "/usr/bin/" },
            { "src": "%bench-dir%/mybench-client", "dest": "/usr/bin/" }
        ],
        "runtime": "mybench-get-runtime",
        "start": "mybench-client"
    },
    "server": {
        "files-from-controller": [
            { "src": "%bench-dir%/mybench-base", "dest": "/usr/bin/" },
            { "src": "%bench-dir%/mybench-server-start", "dest": "/usr/bin/" },
            { "src": "%bench-dir%/mybench-server-stop", "dest": "/usr/bin/" }
        ],
        "start": "mybench-server-start",
        "stop": "mybench-server-stop"
    }
}
```

---

## workshop.json

Declares what software must be installed in the engine container
image. Workshop reads this file when building images. Every benchmark
needs a `workshop.json`, even if it has no dependencies (in which
case `requirements` is an empty array).

For the full `workshop.json` reference see the
[workshop.json documentation](../subprojects/core/workshop/docs/workshop-json.md)
in the workshop subproject.

---

## multiplex.json

Defines parameter defaults, validation rules, and optional unit
conversions. This file is consumed by the multiplex subproject during
run setup to expand and validate user-provided benchmark parameters.

### Structure

```json
{
    "presets": { ... },
    "validations": { ... },
    "units": { ... }
}
```

### Presets

Named groups of default parameter values. The `"defaults"` preset is
applied automatically. Other presets (like `"essentials"`) can be
referenced by users.

```json
"presets": {
    "defaults": [
        { "arg": "duration", "vals": ["60"] },
        { "arg": "protocol", "vals": ["tcp"] },
        { "arg": "nthreads", "vals": ["1"] }
    ]
}
```

Each preset entry specifies an `arg` name and one or more `vals`.
When multiple values are given (e.g., `"vals": ["read", "randread"]`),
multiplex generates separate test iterations for each value
(cartesian product across all multi-valued args).

### Validations

Regex-based rules that validate user-provided parameter values:

```json
"validations": {
    "positive_integer": {
        "description": "a whole number greater than 0",
        "args": ["duration", "nthreads", "wsize"],
        "vals": "[1-9][0-9]*"
    },
    "test-types": {
        "description": "all possible test types",
        "args": ["test-type"],
        "vals": "^stream$|^rr$|^crr$"
    },
    "host-or-ip": {
        "description": "a hostname or IP address",
        "args": ["remotehost"],
        "vals": ".+"
    }
}
```

Each validation names the `args` it applies to and a regex pattern
(`vals`) that valid values must match. The `description` is used in
error messages.

### Units (optional)

Conversion tables for parameters with units. Users can specify
values in whatever unit is convenient (e.g., `bs=4K`, `bs=4096B`,
`bs=0.004M`), and multiplex will normalize them to a single
canonical unit. This ensures consistency across all invocations and
satisfies benchmarks that require a specific unit format:

```json
"units": {
    "size_BKMG": {
        "K": { "B": "1/1024", "K": "1", "M": "1024", "G": "1024*1024" }
    }
}
```

---

## Benchmark scripts

All benchmark scripts run inside engine containers. They receive
benchmark parameters as command-line arguments in `--key=value` format
(unless transformed by `param_regex` in `rickshaw.json`).

### Script conventions

- Use Bash with standard modelines:
  ```bash
  #!/bin/bash
  # -*- mode: sh; indent-tabs-mode: nil; sh-basic-offset: 4 -*-
  # vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=bash
  ```
- Source `bench-base` from toolbox for shared functions:
  ```bash
  . /usr/bin/<name>-base || exit 1
  ```
- Parse arguments with `getopt`:
  ```bash
  longopts="duration:,protocol:,nthreads:"
  opts=$(getopt -q -o "" --longoptions "$longopts" -n "getopt.sh" -- "$@")
  eval set -- "$opts"
  while true; do
      case "$1" in
          --duration) shift; duration=$1; shift ;;
          --) shift; break ;;
          *) shift ;;
      esac
  done
  ```
- Use `exit_error "message"` (from bench-base) for fatal errors. This
  sends an error message through the roadblock messaging system.
- Use `validate_label` (from bench-base) to extract the role ID
  (`$id`) from the `RS_CS_LABEL` environment variable.
- Use `validate_sw_prereqs` to check that required binaries exist.
- Use `dump_runtime` at script start for debugging output.

### <name>-base

A shared library sourced by the benchmark's other scripts. This is
where common functions and initialization logic live, providing a
single place to define functionality used across the benchmark's
client, server, and other scripts. It can pull in functionality
from toolbox (the most common pattern), define completely custom
functions, or both.

A typical implementation sources toolbox's `bench-base` library:

```bash
#!/bin/bash
if ! source ${TOOLBOX_HOME}/bash/library/bench-base; then
    echo "ERROR: Could not source bench-base from \$TOOLBOX_HOME [${TOOLBOX_HOME}]"
    exit 1
fi
```

This gives benchmark scripts access to `exit_error`,
`validate_label`, `validate_sw_prereqs`, `dump_runtime`, and other
shared functions from toolbox.

### <name>-get-runtime

Extracts the expected benchmark runtime (in seconds) from the
provided parameters and echoes it to stdout. Rickshaw uses this
value (plus padding) to set roadblock timeouts. The complexity of
this script varies widely between benchmarks since each benchmark
may express its duration differently. In the simplest case, there
is a dedicated parameter whose value can be echoed directly:

```bash
#!/bin/bash
duration=60
opts=$(getopt -q -o "" --longoptions "duration:" -n "getopt.sh" -- "$@")
eval set -- "$opts"
while true; do
    case "$1" in
        --duration) shift; echo $1; shift ;;
        --) shift; exit ;;
        *) shift ;;
    esac
done
```

Other benchmarks may need to compute the runtime from multiple
parameters or parse more complex formats (e.g., fio strips the
unit suffix from values like `30s`). Output `-1` for unbounded
workloads (no timeout).

### <name>-client

The main benchmark execution script. This is where the actual
workload runs. Typical structure:

1. Redirect output: `exec >mybench-stderrout.txt 2>&1`
2. Source base library: `. /usr/bin/mybench-base`
3. Call `dump_runtime` and `validate_label`
4. Check prerequisites: `validate_sw_prereqs mybench jq getopt`
5. Parse all `--key=value` arguments
6. For client-server benchmarks, read server IP/port from messages
   (see "Messaging" below)
7. Run the benchmark, capturing output to a results file
8. Exit 0 on success, `exit_error` on failure

### <name>-server-start (client-server only)

Starts the server process, publishes service information, and exits:

1. Redirect output: `exec >mybench-server-stderrout.txt 2>&1`
2. Source base library and validate
3. Parse arguments (only server-relevant ones)
4. Determine IP address and port numbers:
   ```bash
   let "port = 2 * $id + 30000"
   ```
5. Publish service info for clients via messaging:
   ```bash
   echo '{"recipient":{"type":"all","id":"all"},"user-object":{"svc":{"ip":"'$ip'","ports":['$port']}}}' >msgs/tx/svc
   ```
6. Start the server in the background:
   ```bash
   mybench --server --port $port &
   pid=$!
   echo $pid >mybench-server.pid
   ```
7. Wait briefly and check for startup errors
8. Exit 0

### <name>-server-stop (client-server only)

Reads the PID file and kills the server:

```bash
#!/bin/bash
exec >mybench-server-stop-stderrout.txt 2>&1

if [ -e mybench-server.pid ]; then
    pid=$(cat mybench-server.pid)
    kill -15 $pid
    sleep 3
    if [ -e /proc/$pid ]; then
        kill -9 $pid
    fi
else
    echo "mybench-server.pid not found"
    exit 1
fi
```

---

## Messaging between client and server

Rickshaw provides a message-passing system built on roadblock
synchronization. Benchmark scripts do not communicate directly;
instead they write and read JSON files in `msgs/` directories.

### How it works

1. **Server writes** a JSON message to `msgs/tx/svc` containing its
   IP and port(s) during the server-start phase.
2. **Roadblock** collects messages from `msgs/tx/` and delivers them
   to other participants during synchronization.
3. **Endpoints** (Kubernetes, remote hosts) may modify the service
   information (e.g., creating a LoadBalancer or NodePort) and write
   an adjusted message.
4. **Client reads** service information from `msgs/rx/` before
   connecting to the server.

### Message format

```json
{
    "recipient": { "type": "all", "id": "all" },
    "user-object": {
        "svc": {
            "ip": "192.168.1.100",
            "ports": [30000, 30001]
        }
    }
}
```

### Reading messages on the client side

The client should check for messages in this order of preference:

```bash
file="msgs/rx/endpoint-start-end:1"
if [ ! -e "${file}" ]; then
    file="msgs/rx/server-start-end:1"
fi
if [ -e "$file" ]; then
    remotehost=$(jq -r '.svc.ip' $file)
    port=$(jq -r '.svc.ports[0]' $file)
fi
```

The `endpoint-start-end` message takes priority because the endpoint
may have transformed the server's IP/port (e.g., through a Kubernetes
Service). The `server-start-end` message is the fallback when no
endpoint transformation occurred.

---

## Post-processing script

The post-process script runs on the controller after benchmark data
has been collected. It converts raw benchmark output into the
[Common Data Model (CDM)](https://github.com/perftool-incubator/CommonDataModel)
format used by crucible for metric storage and analysis. Post-process
scripts can be written in Python (preferred for new benchmarks) or
Perl.

### Toolbox metrics API

Post-process scripts use the toolbox metrics library:

**Python:**
```python
import sys, os, json, math
from pathlib import Path

TOOLBOX_HOME = os.environ.get('TOOLBOX_HOME')
p = Path(TOOLBOX_HOME) / 'python'
sys.path.append(str(p))
from toolbox.metrics import log_sample, finish_samples
```

**Perl:**
```perl
use lib "$ENV{'TOOLBOX_HOME'}/perl";
use toolbox::metrics;
```

### Key functions

- **`log_sample(file_id, desc, names, sample)`**: Records a single
  metric data point.
  - `file_id`: Groups samples into output files (typically `"0"`)
  - `desc`: Dict with `source` (benchmark name), `class`
    (`"throughput"` or `"count"`), and `type` (metric name)
  - `names`: Dict of additional name-value pairs for the metric
    (e.g., `{"cmd": "read"}`)
  - `sample`: Dict with `end` (timestamp in ms), `value` (numeric),
    and optionally `begin` (timestamp in ms)
- **`finish_samples()`**: Finalizes all logged samples, writes
  compressed metric data files, and returns the metric data filename.

### Output format

The post-process script must write `post-process-data.json` to the
current directory. This file tells rickshaw where to find the metrics
and what the primary metric is:

```json
{
    "rickshaw-bench-metric": {
        "schema": { "version": "2021.04.12" }
    },
    "benchmark": "mybench",
    "primary-period": "measurement",
    "primary-metric": "ops-sec",
    "periods": [
        {
            "name": "measurement",
            "metric-files": ["metric-data-0.json.xz"]
        }
    ]
}
```

- **`primary-period`**: The period used for the official result
  (typically `"measurement"`).
- **`primary-metric`**: The headline metric for this benchmark.
- **`periods`**: Array of benchmark phases. Most benchmarks have a
  single `"measurement"` period, but you could also define
  `"warm-up"`, `"prep"`, etc.
- **`metric-files`**: The filenames returned by `finish_samples()`.

### Minimal Python post-process example

```python
#!/usr/bin/env python3

import sys, os, json, math
from pathlib import Path

TOOLBOX_HOME = os.environ.get('TOOLBOX_HOME')
if TOOLBOX_HOME is None:
    print("This script requires the toolbox project.")
    exit(1)
sys.path.append(str(Path(TOOLBOX_HOME) / 'python'))
from toolbox.metrics import log_sample, finish_samples

def main():
    # Read raw benchmark output
    with open("mybench-result.txt") as f:
        results = parse_results(f)

    # Log each data point
    desc = {'source': 'mybench', 'class': 'throughput', 'type': 'ops-sec'}
    names = {}
    for r in results:
        sample = {'end': r['timestamp_ms'], 'value': r['ops_per_sec']}
        log_sample("0", desc, names, sample)

    # Finalize and write output
    metric_file_name = finish_samples()
    output = {
        'rickshaw-bench-metric': {'schema': {'version': '2021.04.12'}},
        'benchmark': 'mybench',
        'primary-period': 'measurement',
        'primary-metric': 'ops-sec',
        'periods': [{
            'name': 'measurement',
            'metric-files': [metric_file_name]
        }]
    }
    with open('post-process-data.json', 'w') as f:
        json.dump(output, f)

if __name__ == "__main__":
    exit(main())
```

---

## Execution lifecycle

Understanding the full execution flow helps when debugging benchmark
integration issues.

### Setup phase

1. Rickshaw reads `rickshaw.json` from each `--bench-dir`
2. If `multiplex.json` exists, multiplex validates and expands
   user-provided parameters into iteration sets
3. If a controller `pre-script` is defined, it runs once with the
   first iteration's parameters (e.g., to generate shared config files)
4. Rickshaw creates file lists and benchmark command files for each
   engine
5. Engines copy `files-from-controller` into place

### Per-sample execution (on engines)

For each iteration and sample, the following happens in order,
synchronized by roadblock:

1. **Server engines**: run `server-start` script
2. Server publishes service info via `msgs/tx/`
3. Roadblock synchronization delivers messages
4. **Client engines**: query `runtime` script for timeout value
5. **Client engines**: run `client` (start) script
6. Benchmark runs for the specified duration
7. Client script exits
8. **Server engines**: run `server-stop` script
9. Engine archives all output files

### Post-processing phase

1. After all iterations and samples complete, engines push their
   collected data back to the controller (rickshaw coordinates
   this process but never directly pushes or pulls data to or
   from running engines)
2. For each iteration/sample/role combination, runs the controller
   `post-script` in that sample's output directory
3. Post-process script reads raw output, calls `log_sample` /
   `finish_samples`, writes `post-process-data.json`
4. Rickshaw indexes the metrics into the result store

---

## Environment variables available to scripts

| Variable | Description |
|----------|-------------|
| `RS_CS_LABEL` | Role label (e.g., `client-1`, `server-2`). Parse with `validate_label` to get `$id` |
| `TOOLBOX_HOME` | Path to the toolbox subproject |
| `CRUCIBLE_HOME` | Path to the crucible installation |
| `HK_CPUS` | Housekeeping CPU list (only set when cpu-partitioning is enabled) |
| `WORKLOAD_CPUS` | Workload CPU list (only set when cpu-partitioning is enabled) |

---

## CPU partitioning (optional)

Crucible and rickshaw provide built-in support for cpu-partitioning,
a technique used by high-performance and latency-sensitive workloads
(such as real-time applications) to isolate CPUs for the benchmark
workload and separate them from system housekeeping activity. This
reduces measurement noise and timing jitter caused by kernel and
system tasks sharing CPUs with the workload under test.

The cpu-partitioning environment is set up automatically by the
rickshaw engine bootstrap code (`rickshaw/engine/bootstrap`) and the
endpoint implementations. When a user enables cpu-partitioning in
their run configuration, the engine bootstrap:

1. Runs `discover-cpu-partitioning.py` to identify which CPUs are
   isolated (via kernel boot parameters like `isolcpus`, `nohz_full`,
   or `rcu_nocbs`) and which are available for housekeeping
2. Runs `partition-cpus.py` to divide isolated CPUs among multiple
   engines when more than one engine shares a host
3. Exports two environment variables:
   - **`HK_CPUS`**: CPU list for housekeeping/management threads
   - **`WORKLOAD_CPUS`**: CPU list for benchmark workload threads
4. Pins the engine's own processes to `HK_CPUS` so that workload
   CPUs remain undisturbed

### Supporting cpu-partitioning in a benchmark

Benchmarks that want to take advantage of cpu-partitioning should:

1. **Check for the environment variables**: When cpu-partitioning is
   enabled, `HK_CPUS` and `WORKLOAD_CPUS` will be set. The benchmark
   should validate that both are present and non-empty:
   ```bash
   if [ -z "$WORKLOAD_CPUS" ]; then
       exit_error "WORKLOAD_CPUS is not defined"
   fi
   if [ -z "$HK_CPUS" ]; then
       exit_error "HK_CPUS is not defined"
   fi
   ```

2. **Pin workload threads to `WORKLOAD_CPUS`**: Use `taskset`,
   tool-specific affinity options, or both to ensure workload threads
   run only on isolated CPUs.

3. **Pin management threads to `HK_CPUS`**: If the benchmark has a
   main/control thread separate from the workload threads, pin it to
   the housekeeping CPUs. For example, cyclictest uses
   `--affinity ${WORKLOAD_CPUS}` for measurement threads and
   `--mainaffinity ${HK_CPUS}` for its main thread.

4. **Use `taskset` to cover the full CPU set**: A common pattern is
   to run the benchmark under `taskset -c ${HK_CPUS},${WORKLOAD_CPUS}`
   so that the process has access to all assigned CPUs, then use
   tool-specific options to direct individual threads to the
   appropriate group.

### Existing benchmarks with cpu-partitioning support

For reference implementations, see: `cyclictest`, `oslat`,
`timerlat`, `hwnoise`, and `trafficgen`. These benchmarks all
require cpu-partitioning and demonstrate the patterns described
above.

Not all benchmarks need cpu-partitioning support. It is only relevant
for workloads where CPU isolation materially affects measurement
quality. Most throughput-oriented benchmarks (e.g., fio, uperf) do
not use it.

---

## Adding the benchmark to crucible

Once your benchmark repository is ready:

1. Add an entry to `crucible/config/repos.json`:
   ```json
   {
       "name": "mybench",
       "type": "benchmark",
       "repository": "https://github.com/perftool-incubator/bench-mybench.git",
       "primary-branch": "main",
       "checkout": { "type": "branch", "target": "main" }
   }
   ```
2. Run `crucible update mybench` to clone and activate the repo
3. The benchmark will be available at
   `subprojects/benchmarks/mybench/`

---

## Checklist for a new benchmark

- [ ] Create repo named `bench-<name>`
- [ ] `rickshaw.json` with correct schema version and required fields
- [ ] `workshop.json` declaring all build dependencies
- [ ] `<name>-client` script that runs the workload
- [ ] `<name>-get-runtime` script that outputs duration in seconds
- [ ] `<name>-post-process` script that produces `post-process-data.json`
- [ ] `multiplex.json` with parameter defaults and validations (recommended)
- [ ] `<name>-base` script sourcing toolbox bench-base (recommended)
- [ ] For client-server: `<name>-server-start` and `<name>-server-stop`
- [ ] For client-server: messaging via `msgs/tx/svc` and `msgs/rx/`
- [ ] `LICENSE` (Apache 2.0)
- [ ] `README.md`
- [ ] Entry in `crucible/config/repos.json`

---

## Reference implementations

| Benchmark | Complexity | Good example of |
|-----------|------------|-----------------|
| `sleep` | Minimal | Simplest possible benchmark; start here |
| `iperf` | Medium | Client-server with messaging |
| `uperf` | Medium-high | Multiple test types, CPU pinning, server IP resolution |
| `fio` | Medium-high | Controller pre-script, param_regex, complex multiplex.json |
| `cyclictest` | Medium | Source builds with patches, multiple userenvs |
