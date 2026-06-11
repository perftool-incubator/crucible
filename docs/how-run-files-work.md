# How Run Files Work

This document explains the structure of crucible run files —
the JSON documents that define what benchmarks to run, where
to run them, and how to configure the test.

For how parameters are expanded into iterations, see
[how-benchmark-execution-works.md](how-benchmark-execution-works.md).
For how endpoints deploy engines, see
[how-endpoints-work.md](how-endpoints-work.md).
For how tools are configured, see
[how-tool-collection-works.md](how-tool-collection-works.md).

## Overview

The run file is the primary user interface for crucible. It's
a JSON document that specifies:

- **What** to run (benchmarks and their parameters)
- **Where** to run it (endpoints: hosts, clusters, clouds)
- **How** to configure the test (tools, samples, tags)

A run is executed with:

```bash
crucible run my-run-file.json
```

Run files are validated against
`rickshaw/schema/run-file.json` and endpoint-specific schemas
before execution begins.

## Top-level structure

A run file has five sections:

```json
{
    "benchmarks": [ ... ],
    "endpoints": [ ... ],
    "tool-params": [ ... ],
    "run-params": { ... },
    "tags": { ... }
}
```

## Benchmarks

The benchmarks section is an array of benchmark
configurations. Each entry specifies the benchmark name,
engine IDs, and parameters:

```json
"benchmarks": [
    {
        "name": "uperf",
        "ids": "1-2",
        "mv-params": { ... }
    }
]
```

### Fields

- **`name`** (required): The benchmark name, matching a
  deployed benchmark subproject (e.g., `uperf`, `fio`,
  `cyclictest`)
- **`ids`** (required): Engine IDs assigned to this benchmark.
  Accepts multiple formats:
  - String with ranges: `"1-4"`, `"1+3"`, `"1-3+5-7"`
  - Integer: `1`
  - Array: `[1, 2, 3]`
- **`mv-params`** (required): Parameter configuration (see
  below)

### Engine IDs

Engine IDs link benchmarks to endpoints. The same IDs
referenced in the benchmark's `ids` field must appear in the
endpoint configuration. For example, if a benchmark has
`"ids": "1-2"`, the endpoint must include engines with IDs
1 and 2.

In multi-benchmark runs, each benchmark gets its own set of
IDs:

```json
"benchmarks": [
    { "name": "iperf", "ids": "1", "mv-params": { ... } },
    { "name": "uperf", "ids": "2", "mv-params": { ... } }
]
```

## Parameters (mv-params)

The `mv-params` (multivariate parameters) section defines
benchmark parameters using a two-tier structure:
**global-options** (reusable named groups) and **sets** (test
configurations that reference global-options and add their
own parameters). This section is processed by the
**multiplex** subproject, which performs multivariate
parameter expansion — generating the cartesian product of
all multi-valued parameters to produce the iteration matrix.

```json
"mv-params": {
    "global-options": [
        {
            "name": "common",
            "params": [
                { "arg": "duration", "vals": ["60"] },
                { "arg": "protocol", "vals": ["tcp"] }
            ]
        }
    ],
    "sets": [
        {
            "include": "common",
            "params": [
                { "arg": "wsize", "vals": ["64", "1024"] }
            ]
        }
    ]
}
```

### Parameter fields

| Field | Required | Description |
|-------|----------|-------------|
| `arg` | Yes | Parameter name (passed as `--arg` to the benchmark) |
| `vals` | Yes* | Array of values. Multiple values create iterations via cartesian product. |
| `val` | Yes* | Single value (alternative to `vals`) |
| `role` | No | Restrict to a specific role: `"client"`, `"server"`, or `"all"` (default) |
| `enabled` | No | `"yes"` (default) or `"no"` — disabled params are excluded |

*One of `vals` or `val` is required.

### How parameters become iterations

Each set independently expands its parameters via cartesian
product. Multiple values in `vals` multiply with other
multi-valued parameters to create iterations:

- `protocol: ["tcp"]` × `wsize: ["64", "1024"]` = 2 iterations
- `protocol: ["tcp", "udp"]` × `wsize: ["64", "1024"]` = 4 iterations

Parameters inherited from `global-options` via `include` are
combined with the set's own parameters.

### Simplified format

For simple cases, parameters can be specified directly
without the global-options/sets structure:

```json
"mv-params": {
    "sets": [
        {
            "params": [
                { "arg": "duration", "vals": ["60"] },
                { "arg": "wsize", "vals": ["64", "1024"] }
            ]
        }
    ]
}
```

## Endpoints

The endpoints section defines where engines are deployed.
Each endpoint specifies a type and host-specific
configuration:

### Remotehosts endpoint

```json
{
    "type": "remotehosts",
    "settings": {
        "user": "root"
    },
    "remotes": [
        {
            "engines": [
                { "role": "client", "ids": [1] },
                { "role": "server", "ids": [2] }
            ],
            "config": {
                "host": "testhost.example.com",
                "settings": {
                    "userenv": "rhubi9",
                    "osruntime": "podman",
                    "cpu-partitioning": false,
                    "host-mounts": []
                }
            }
        }
    ]
}
```

### Kubernetes endpoint

```json
{
    "type": "kube",
    "settings": {
        "user": "root"
    },
    "remotes": [
        {
            "engines": [
                { "role": "client", "ids": [1, 2] },
                { "role": "server", "ids": [3, 4] }
            ],
            "config": {
                "host": "k8s-controller.example.com",
                "settings": {
                    "userenv": "rhubi9",
                    "controller-ip-address": "10.0.0.1"
                }
            }
        }
    ]
}
```

### Key endpoint settings

| Setting | Description | Default |
|---------|-------------|---------|
| `userenv` | Base container image name | From rickshaw-settings |
| `osruntime` | Runtime mode: `"podman"` or `"chroot"` | `"podman"` |
| `cpu-partitioning` | Enable CPU isolation | `false` |
| `host-mounts` | Host directories to mount into engines | `[]` |
| `controller-ip-address` | IP for engines to reach the controller | Auto-detected |
| `disable-tools` | Skip tool collection on this endpoint | `false` |

### Settings hierarchy

Endpoint settings can be specified at multiple levels:

1. **Top-level `settings`**: Defaults for all remotes/configs
2. **Per-remote/per-config `settings`**: Override defaults for
   specific hosts or engine groups

Per-remote settings override top-level settings.

### Per-engine configuration

Endpoints support targeting specific engines with different
settings:

```json
"config": [
    {
        "targets": [
            { "role": "client", "ids": "1" }
        ],
        "settings": {
            "osruntime": "chroot",
            "cpu-partitioning": true
        }
    },
    {
        "targets": "default",
        "settings": {
            "osruntime": "podman"
        }
    }
]
```

## Tool parameters

The `tool-params` section configures which tools run and
their settings:

```json
"tool-params": [
    {
        "tool": "sysstat",
        "params": [
            { "arg": "subtools", "val": "mpstat,sar,iostat" },
            { "arg": "interval", "val": "3" }
        ]
    },
    {
        "tool": "procstat"
    },
    {
        "tool": "kernel",
        "enabled": "no"
    }
]
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `tool` | Yes | Tool name (must match a deployed tool subproject) |
| `params` | No | Array of `{arg, val}` parameter objects |
| `enabled` | No | `"yes"` (default) or `"no"` |

### Default tools

When `tool-params` is omitted from the run file, crucible
automatically uses the default tool set (sysstat and
procstat). Specifying `tool-params` replaces the defaults
entirely — only the tools listed will run.

Tool parameters use `val` (single value) rather than `vals`
(array) because tools don't participate in the iteration
matrix.

## Run parameters

The `run-params` section controls execution behavior:

```json
"run-params": {
    "num-samples": 3,
    "max-sample-failures": 3,
    "test-order": "random"
}
```

### Fields

| Field | Description | Default |
|-------|-------------|---------|
| `num-samples` | Executions per iteration (for statistical confidence) | 1 |
| `max-sample-failures` | Failed attempts before a sample is permanently failed | 1 |
| `test-order` | Execution order: `"sample"`, `"iteration"`, or `"random"` | `"sample"` |
| `name` | Override user name (both name and email required if either specified) | From identity |
| `email` | Override user email | From identity |

### Test order

- **`sample`** (or `"s"`): All samples of iteration 1, then
  all samples of iteration 2, etc.
- **`iteration`** (or `"i"`): Sample 1 of all iterations,
  then sample 2 of all iterations, etc.
- **`random`** (or `"r"`): Randomized order to reduce
  systematic bias

## Tags

The `tags` section adds free-form metadata for identifying
and filtering runs:

```json
"tags": {
    "purpose": "regression-test",
    "kernel": "6.8.0-rt",
    "tuned-profile": "cpu-partitioning",
    "hardware": "Dell R760"
}
```

Tags are indexed into OpenSearch and appear in the CDM web
dashboard, enabling filtering and comparison across runs.
They're especially useful for tracking what changed between
runs (kernel version, tuning profile, hardware configuration).

Values are always strings. Any key names can be used.

## Complete annotated example

```json
{
    "tags": {
        "purpose": "network-throughput-comparison",
        "kernel": "6.8.0"
    },

    "run-params": {
        "num-samples": 3,
        "test-order": "random"
    },

    "tool-params": [
        {
            "tool": "sysstat",
            "params": [
                { "arg": "subtools", "val": "mpstat,sar" },
                { "arg": "interval", "val": "1" }
            ]
        },
        { "tool": "procstat" }
    ],

    "endpoints": [
        {
            "type": "remotehosts",
            "settings": { "user": "root" },
            "remotes": [
                {
                    "engines": [
                        { "role": "client", "ids": [1] },
                        { "role": "server", "ids": [2] }
                    ],
                    "config": {
                        "host": "testhost.example.com",
                        "settings": {
                            "userenv": "rhubi9",
                            "osruntime": "podman"
                        }
                    }
                }
            ]
        }
    ],

    "benchmarks": [
        {
            "name": "uperf",
            "ids": "1-2",
            "mv-params": {
                "global-options": [
                    {
                        "name": "common",
                        "params": [
                            { "arg": "duration", "vals": ["60"], "role": "client" },
                            { "arg": "nthreads", "vals": ["1"], "role": "client" }
                        ]
                    }
                ],
                "sets": [
                    {
                        "include": "common",
                        "params": [
                            { "arg": "protocol", "vals": ["tcp", "udp"], "role": "client" },
                            { "arg": "wsize", "vals": ["64", "1024"], "role": "client" }
                        ]
                    }
                ]
            }
        }
    ]
}
```

This run file produces 4 iterations (2 protocols × 2 sizes),
each executed 3 times (num-samples) in random order. Sysstat
collects CPU and system metrics at 1-second intervals, and
procstat captures /proc data with default settings. The run
is tagged for later identification.
