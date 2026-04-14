# Implementing a New Tool

This guide explains how to create a new tool for the crucible
performance testing framework. A tool is a passive data collector
that runs in the background alongside benchmarks, gathering system
metrics (CPU usage, interrupt rates, process activity, etc.) during
test execution.

Tools differ from benchmarks in a fundamental way: benchmarks
actively generate workload; tools passively observe and record system
behavior while the workload runs.

## Repository naming

Tool repos follow the naming convention `tool-<name>` (e.g.,
`tool-sysstat`, `tool-procstat`, `tool-forkstat`). The tool name
used inside configuration files is just `<name>` without the `tool-`
prefix.

## Required files

Every tool must contain at minimum:

| File | Purpose |
|------|---------|
| `rickshaw.json` | Tells rickshaw how to deploy and post-process the tool |
| `workshop.json` | Declares container image build requirements |
| `<name>-start` | Launches the collection process(es) in the background |
| `<name>-stop` | Cleanly terminates collection and compresses output |
| `<name>-post-process` | Converts raw collected data into crucible metrics |

## Commonly included files

| File | When needed |
|------|-------------|
| `<name>-collect` | Separate collection loop (e.g., procstat periodically reads /proc files) |
| `LICENSE` | Apache 2.0 (standard across all tools) |
| `README.md` | Project documentation |

---

## rickshaw.json

This is the primary integration file. It tells rickshaw what scripts
to deploy, where the tool is allowed to run, and how to post-process
results. Rickshaw validates it against `rickshaw/schema/tool.json`.

### Schema version

```json
{
    "rickshaw-tool": {
        "schema": {
            "version": "2020.03.18"
        }
    }
}
```

### Tool name

```json
"tool": "<name>"
```

This must match the directory name used in `subprojects/tools/`.

### Controller section

The controller runs on the orchestrator host. It has one required
hook:

```json
"controller": {
    "post-script": "%tool-dir%<name>-post-process"
}
```

- **`post-script`** (required): Runs on the controller after all
  benchmark iterations have completed and data has been sent back
  from the engines. Converts raw tool output into crucible metrics.

### Collector section

Defines how the tool deploys and runs on collection nodes:

```json
"collector": {
    "files-from-controller": [
        { "src": "%tool-dir%/<name>-start", "dest": "/usr/bin/" },
        { "src": "%tool-dir%/<name>-stop", "dest": "/usr/bin/" }
    ],
    "blacklist": [
        {
            "endpoint": "remotehosts",
            "collector-types": [ "client", "server" ]
        },
        {
            "endpoint": "kube",
            "collector-types": [ "client", "server" ]
        }
    ],
    "whitelist": [
        {
            "endpoint": "remotehosts",
            "collector-types": [ "profiler" ]
        },
        {
            "endpoint": "kube",
            "collector-types": [ "profiler" ]
        }
    ],
    "start": "<name>-start",
    "stop": "<name>-stop"
}
```

- **`files-from-controller`** (required): Files copied from the
  controller to each collection engine at runtime. Each entry has
  `src` (controller path) and `dest` (engine path). Use `%tool-dir%`
  to reference the tool's directory on the controller. By copying
  these files at runtime rather than baking them into the engine
  container image, tool scripts can be modified on the fly without
  rebuilding images.
- **`blacklist`** (recommended): Endpoints and collector types where
  this tool should NOT run. Most tools block `client` and `server`
  types since tools collect system-level data, not workload data.
- **`whitelist`** (recommended): Endpoints and collector types where
  this tool SHOULD run. Common values are `profiler` (for
  remotehosts and kube endpoints) and `compute` (for OSP endpoints).
- **`start`** (required): Script that launches collection in the
  background.
- **`stop`** (required): Script that terminates collection and
  compresses output.

### Collector types

Collector types represent the roles a collection node can play:

| Collector type | Endpoint | Description |
|----------------|----------|-------------|
| `profiler` | remotehosts, kube | Dedicated profiling node |
| `compute` | osp | OpenStack compute node |
| `master` | kube | Kubernetes master node |
| `worker` | kube | Kubernetes worker node |
| `client` | remotehosts, kube | Benchmark client engine (typically blocked for tools) |
| `server` | remotehosts, kube | Benchmark server engine (typically blocked for tools) |

Most tools block `client` and `server` because tool data collection
should happen on the host/node level, not inside individual benchmark
engine containers. The `profiler` type is the most common target for
tools.

### Minimal example

```json
{
    "rickshaw-tool": {
        "schema": { "version": "2020.03.18" }
    },
    "tool": "mytool",
    "controller": {
        "post-script": "%tool-dir%mytool-post-process"
    },
    "collector": {
        "files-from-controller": [
            { "src": "%tool-dir%/mytool-start", "dest": "/usr/bin/" },
            { "src": "%tool-dir%/mytool-stop", "dest": "/usr/bin/" }
        ],
        "blacklist": [
            { "endpoint": "remotehosts", "collector-types": [ "client", "server" ] },
            { "endpoint": "kube", "collector-types": [ "client", "server" ] }
        ],
        "whitelist": [
            { "endpoint": "remotehosts", "collector-types": [ "profiler" ] },
            { "endpoint": "kube", "collector-types": [ "profiler" ] }
        ],
        "start": "mytool-start",
        "stop": "mytool-stop"
    }
}
```

---

## workshop.json

Declares what software must be installed in the engine container
image. Workshop reads this file when building images. Every tool
needs a `workshop.json`, even if it has no dependencies (in which
case `requirements` is an empty array).

For the full `workshop.json` reference see the
[workshop.json documentation](../subprojects/core/workshop/docs/workshop-json.md)
in the workshop subproject.

---

## Tool scripts

All tool scripts run inside engine containers on collection nodes.
They receive tool parameters as command-line arguments in
`--key value` format.

### Script conventions

- Use Bash with standard modelines:
  ```bash
  #!/bin/bash
  # -*- mode: sh; indent-tabs-mode: nil; sh-basic-offset: 4 -*-
  # vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=bash
  ```
- Redirect all output to a log file at the start of every script:
  ```bash
  exec >mytool-start-stderrout.txt
  exec 2>&1
  ```
- Parse arguments with `getopt`:
  ```bash
  longopts="interval:,option:"
  opts=$(getopt -q -o "" --longopts "$longopts" -n "getopt.sh" -- "$@")
  eval set -- "$opts"
  while true; do
      case "$1" in
          --interval) shift; interval=$1; shift ;;
          --) shift; break ;;
          *) shift ;;
      esac
  done
  ```

### <name>-start

Launches collection in the background and records process IDs for
later shutdown. Typical structure:

1. Redirect output: `exec >mytool-start-stderrout.txt 2>&1`
2. Log arguments and environment for debugging
3. Parse `--key value` arguments with defaults
4. Capture any baseline system state needed for post-processing
5. Launch the collection process in the background:
   ```bash
   mytool --some-flag --interval $interval > mytool-output.txt &
   pid=$!
   echo "$pid" > mytool-pids.txt
   ```
6. Exit 0 (the collection process continues in the background)

**Example** (forkstat-start):
```bash
#!/bin/bash
exec >forkstat-start-stderrout.txt
exec 2>&1

events="all"
longopts="events:"
opts=$(getopt -q -o "" --longopts "$longopts" -n "getopt.sh" -- "$@")
eval set -- "$opts"
while true; do
    case "$1" in
        --events) shift; events=$1; shift ;;
        --) shift; break ;;
        *) exit 1 ;;
    esac
done

date -u +%Y-%m-%d > forkstat-date.txt

forkstat -e ${events} -X -S > forkstat-stderrout.txt &
echo "$!" > forkstat-pids.txt
```

### <name>-stop

Reads the PID file, terminates collection, and compresses output:

```bash
#!/bin/bash
exec >mytool-stop-stderrout.txt
exec 2>&1

if [ -e mytool-pids.txt ]; then
    while read pid; do
        kill -s SIGINT $pid
    done < mytool-pids.txt

    xz --verbose --threads=0 mytool-output.txt
else
    echo "Could not find mytool-pids.txt"
    exit 1
fi
```

Key points:
- Use `SIGINT` first (allows graceful shutdown and output flushing)
- Compress output with `xz` to reduce data transfer back to the
  controller
- Exit non-zero if the PID file is missing (indicates a startup
  failure)

---

## Post-processing script

The post-process script runs on the controller after tool data has
been collected from all engines. It converts raw tool output into the
[Common Data Model (CDM)](https://github.com/perftool-incubator/CommonDataModel)
format used by crucible for metric storage and analysis. Post-process
scripts can be written in Python (preferred for new tools) or Perl.

### Toolbox metrics API

Post-process scripts use the toolbox metrics library:

**Python:**
```python
import sys, os, json
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
  - `desc`: Dict with `source` (tool name), `class`
    (`"throughput"` or `"count"`), and `type` (metric name)
  - `names`: Dict of additional name-value pairs for the metric
    (e.g., `{"cpu": "0", "type": "usr"}`)
  - `sample`: Dict with `end` (timestamp in ms), `value` (numeric),
    and optionally `begin` (timestamp in ms)
- **`finish_samples()`**: Finalizes all logged samples, writes
  compressed metric data files, and returns the metric data filename.

### Output format

The post-process script must write `post-process-data.json` to the
current directory:

```json
{
    "rickshaw-bench-metric": {
        "schema": { "version": "2021.04.12" }
    },
    "tool": "mytool",
    "primary-period": "measurement",
    "primary-metric": "some-rate",
    "periods": [
        {
            "name": "measurement",
            "metric-files": ["metric-data-0.json.xz"]
        }
    ]
}
```

### Minimal Python post-process example

```python
#!/usr/bin/env python3

import sys, os, json, lzma
from pathlib import Path

TOOLBOX_HOME = os.environ.get('TOOLBOX_HOME')
if TOOLBOX_HOME is None:
    print("This script requires the toolbox project.")
    exit(1)
sys.path.append(str(Path(TOOLBOX_HOME) / 'python'))
from toolbox.metrics import log_sample, finish_samples

def main():
    # Read compressed raw tool output
    with lzma.open("mytool-output.txt.xz", "rt") as f:
        data = parse_output(f)

    # Log each data point
    desc = {'source': 'mytool', 'class': 'count', 'type': 'events-sec'}
    for point in data:
        names = {'event_type': point['type']}
        sample = {'end': point['timestamp_ms'], 'value': point['rate']}
        log_sample("0", desc, names, sample)

    # Finalize and write output
    metric_file_name = finish_samples()
    output = {
        'rickshaw-bench-metric': {'schema': {'version': '2021.04.12'}},
        'tool': 'mytool',
        'primary-period': 'measurement',
        'primary-metric': 'events-sec',
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

Understanding how tools fit into the overall execution flow helps
when debugging integration issues.

### Tool timing relative to benchmarks

For each benchmark iteration and sample, tools and benchmarks are
coordinated by roadblock synchronization:

1. **Tools start**: All tool `start` scripts run in parallel on
   collection nodes
2. Roadblock synchronization (tools confirm they are running)
3. **Benchmark runs**: Client and server scripts execute the workload
   while tools collect in the background
4. **Benchmark completes**: Client exits, server stops
5. Roadblock synchronization
6. **Tools stop**: All tool `stop` scripts run in parallel
7. Engine archives all output files (tool + benchmark data)

### Post-processing phase

1. After all iterations and samples complete, engines push their
   collected data back to the controller
2. For each iteration/sample/collector combination, the controller
   runs the tool's `post-script` in that sample's output directory
3. Post-process script reads compressed raw output, calls
   `log_sample` / `finish_samples`, writes `post-process-data.json`
4. Rickshaw indexes the metrics into the result store

---

## Tool parameters

Tool parameters are specified in the `tool-params` section of the
run file (not in a separate `multiplex.json` like benchmarks):

```json
"tool-params": [
    {
        "tool": "mytool",
        "params": [
            { "arg": "interval", "val": "3" },
            { "arg": "option", "val": "value" }
        ]
    }
]
```

Each parameter becomes a `--key value` argument pair passed to the
start script. Parameters are optional — tools should define sensible
defaults.

### Enabling and disabling

A tool can be disabled for a specific run:

```json
{
    "tool": "mytool",
    "enabled": "no"
}
```

Including a tool in `tool-params` with no `enabled` field (or
`"enabled": "yes"`) enables it.

---

## Environment variables available to scripts

| Variable | Description |
|----------|-------------|
| `TOOLBOX_HOME` | Path to the toolbox subproject |
| `CRUCIBLE_HOME` | Path to the crucible installation |

Tool scripts typically do not use `RS_CS_LABEL`, `HK_CPUS`, or
`WORKLOAD_CPUS` since tools run on profiler/collector nodes rather
than benchmark engine containers. However, these variables may be
available depending on the deployment context.

---

## Adding the tool to crucible

Once your tool repository is ready:

1. Add an entry to `crucible/config/repos.json`:
   ```json
   {
       "name": "mytool",
       "type": "tool",
       "repository": "https://github.com/perftool-incubator/tool-mytool.git",
       "primary-branch": "main",
       "checkout": { "mode": "follow", "target": "main" }
   }
   ```
2. Run `crucible update mytool` to clone and activate the repo
3. The tool will be available at `subprojects/tools/mytool/`
4. Add a `tool-params` entry to your run file to enable the tool

---

## Checklist for a new tool

- [ ] Create repo named `tool-<name>`
- [ ] `rickshaw.json` with correct schema version and required fields
- [ ] `workshop.json` declaring all build dependencies
- [ ] `<name>-start` script that launches collection in the background
- [ ] `<name>-stop` script that terminates collection and compresses output
- [ ] `<name>-post-process` script that produces `post-process-data.json`
- [ ] Whitelist and blacklist for appropriate collector types
- [ ] `LICENSE` (Apache 2.0)
- [ ] `README.md`
- [ ] Entry in `crucible/config/repos.json`

---

## Reference implementations

| Tool | Complexity | Good example of |
|------|------------|-----------------|
| `forkstat` | Simple | Minimal tool; single process, simple output parsing |
| `procstat` | Medium | Separate collector script, /proc file sampling, delta computation |
| `sysstat` | Complex | Multiple subtools, source build, parallel post-processing |
| `kernel` | Complex | Subtools pattern (turbostat, perf, trace-cmd), many parameters |
