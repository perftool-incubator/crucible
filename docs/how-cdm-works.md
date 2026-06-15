# How CDM Works

This document explains how crucible's Common Data Model (CDM)
stores, indexes, and queries benchmark and tool results. CDM
provides the data model, OpenSearch integration, query API,
and web dashboard for result analysis.

For how benchmark metrics are generated, see
[how-benchmark-execution-works.md](how-benchmark-execution-works.md).
For how tool metrics relate to benchmark periods, see
[how-tool-collection-works.md](how-tool-collection-works.md).

## Overview

CDM solves a fundamental problem: different benchmarks produce
results in different formats with different metric names,
units, and structures. CDM defines a standardized schema that
all benchmarks and tools conform to, enabling unified storage,
querying, and comparison across workloads.

All result data is stored in OpenSearch using a hierarchical
document model. The CDM server provides a REST API for
querying, and a web dashboard for interactive analysis.

## The document hierarchy

Result data is organized hierarchically:

```
run
├── tag (metadata key-value pairs)
├── iteration
│   ├── param (benchmark parameters for this iteration)
│   ├── sample
│   │   ├── period (named time window: measurement, warm-up, etc.)
│   │   │   ├── metric_desc (what the metric is)
│   │   │   └── metric_data (time-series values)
```

### Document types

| Type | Purpose | Key fields |
|------|---------|-----------|
| **run** | A single benchmark execution | run UUID, begin/end times, benchmark name, user, host |
| **iteration** | One parameter combination | iteration UUID, primary metric, primary period, status |
| **sample** | One execution of an iteration | sample UUID, status (pass/fail) |
| **period** | A time window within a sample | period UUID, name, begin/end timestamps |
| **param** | A benchmark parameter | arg name, value, role (client/server) |
| **tag** | Run metadata | name, value (e.g., kernel version, test purpose) |
| **metric_desc** | What a metric IS | source, type, class, breakout dimensions |
| **metric_data** | Actual values | begin, end, value, duration |

Each level adds context. A metric_data document inherits its
run, iteration, sample, and period context through the
document hierarchy, so queries can filter at any level.

## Metric descriptors and metric data

The core of CDM is the separation between metric definitions
(what) and metric values (data).

### metric_desc (descriptor)

Defines what a metric measures:

- **source**: Which benchmark or tool produced it (e.g.,
  `uperf`, `mpstat`, `procstat`)
- **type**: The specific metric name (e.g., `Gbps`,
  `Busy-CPU`, `interrupts-sec`)
- **class**: The metric category — `throughput` (rate-based)
  or `count` (accumulative)
- **names**: Breakout dimensions that identify specific
  instances of this metric (e.g., `hostname=worker-01`,
  `cpu=3`, `device=sda`, `direction=rx`)

The names/dimensions enable drill-down analysis. A single
metric type like `Busy-CPU` may have hundreds of metric_desc
documents — one per CPU per host.

### metric_data (values)

Contains the actual time-series data:

- **begin**: Start timestamp (milliseconds since epoch)
- **end**: End timestamp (milliseconds since epoch)
- **value**: The metric value for this time window
- **duration**: `end - begin + 1`

Each metric_data document is linked to a metric_desc via
UUID. The toolbox metrics library consolidates adjacent
samples with the same value to reduce data volume.

### Benchmark metrics vs tool metrics

- **Benchmark metrics** are attributed to a specific period
  (e.g., "measurement"). They exist only within the time
  window of that period.
- **Tool metrics** have no period attribution. They are
  collected continuously and exist as a flat time series.
  When querying tool metrics for a specific period, CDM
  uses the period's begin/end timestamps as a time range
  filter rather than a document relationship.

## How results get into OpenSearch

### The indexing pipeline

```
Post-processing
  ↓ (creates metric-data files + post-process-data.json)
rickshaw-gen-docs.py
  ↓ (generates NDJSON documents)
add-run.sh
  ↓ (bulk-indexes into OpenSearch)
OpenSearch indices
```

### Document generation

`rickshaw-gen-docs.py` reads the post-processing output
(validated against `rickshaw/schema/bench-metric.json`)
and generates OpenSearch documents:

1. Creates **run**, **tag**, and **param** documents from
   the run configuration
2. Processes **tool metrics** in parallel — reads
   metric-data files from each tool's postprocess/ directory
3. Processes **benchmark metrics** sequentially — for each
   iteration/sample/period, reads post-process-data.json
   and the associated metric files
4. Creates **period**, **sample**, and **iteration**
   documents with consolidated timestamps

Output is NDJSON (newline-delimited JSON) — pairs of index
commands and document bodies, written to the
`run/opensearch/` directory.

### UUID persistence

Document UUIDs are generated once and stored in persistent
ID files within the result directory. This means re-indexing
the same run produces the same UUIDs, making re-indexing
idempotent rather than creating duplicate documents. It also
means that any saved queries, dashboard URLs, or scripts
that reference specific UUIDs (run, iteration, period, etc.)
continue to work after re-indexing — users don't have to
update their queries when results are re-processed and
re-indexed.

## How results are queried

### The CDM server

The CDM server is a Node.js/Express application that
provides a REST API for querying results in OpenSearch.
It runs as a crucible service on a configurable port
(default 3000).

### Key query patterns

**Run discovery**: Search for runs by benchmark name,
user, tags, date range:

```
GET /api/v1/runs?benchmark=uperf&name=John
```

**Result summary**: Get the primary metric value for each
iteration in a run, providing a quick overview of
performance:

```
crucible get result --run <run-id>
```

**Metric data retrieval**: Get specific metric values
with optional breakouts and time filtering:

```
POST /api/v1/metric-data
{
    "run": "<run-uuid>",
    "period": "<period-uuid>",
    "source": "mpstat",
    "type": "Busy-CPU",
    "breakouts": ["hostname", "cpu"]
}
```

### Period-based time filtering

When you query for metrics within a specific period, CDM:

1. Looks up the period document to get its begin and end
   timestamps
2. **Removes the period ID from the query** — because
   tool metrics don't have period attribution
3. Uses the timestamps as a range filter on metric_data

This approach works uniformly for both benchmark metrics
(which have period attribution) and tool metrics (which
don't). The result is the same: you get metric data from
within that time window.

### Breakout dimensions

Metrics can be broken down by their dimension values.  For
example, `Busy-CPU` from mpstat has dimensions like
`hostname` and `cpu`.  Querying with `breakouts: ["hostname"]`
returns separate time series per host.  Adding `"cpu"` splits
further by individual CPU.

#### Response format

The metric-data API response includes three fields that
describe breakout state:

```json
{
    "values": {
        "": [{ "begin": 1648111546729, "end": 1648111635267, "value": 45.2 }]
    },
    "usedBreakouts": [],
    "remainingBreakouts": ["hostname", "cpu", "type"]
}
```

- **`values`** — metric data keyed by breakout labels.
  Without breakouts, the key is an empty string (aggregated
  across all dimensions).  With breakouts, each key encodes
  the dimension values in angle brackets:
  `"<host1>-<0>"`, `"<host1>-<1>"`, `"<host2>-<0>"`.
- **`usedBreakouts`** — dimensions currently applied to
  produce the value keys.
- **`remainingBreakouts`** — dimensions still available for
  further disaggregation.  These are the dimensions you can
  add to the `breakouts` array to split the data further.

With breakouts applied:

```json
{
    "values": {
        "<host1>-<0>": [{ "begin": 1648111546729, "end": 1648111635267, "value": 78.5 }],
        "<host1>-<1>": [{ "begin": 1648111546729, "end": 1648111635267, "value": 65.2 }],
        "<host2>-<0>": [{ "begin": 1648111546729, "end": 1648111635267, "value": 82.1 }]
    },
    "usedBreakouts": ["hostname", "cpu"],
    "remainingBreakouts": ["type"]
}
```

#### Breakout query syntax

The `breakouts` array supports three forms:

- **Basic**: `["hostname", "cpu"]` — split by all values
  of each dimension
- **Value-filtered**: `["hostname=host1+host2"]` — return
  separate series only for the specified values
- **Regex**: `["hostname=r/^worker-.*/"]` — return series
  for values matching the pattern

#### Available dimensions

The dimensions available for breakout depend on the metric
source.  Each metric's `metric_desc` document in OpenSearch
records which dimensions are present.  Common dimensions
include:

| Dimension | Description |
|-----------|-------------|
| `hostname` | Host where the metric was collected |
| `cpu` | CPU number |
| `csid` | Client-server ID — identifies which engine pair produced the metric |
| `cstype` | Client-server type (e.g., `client`, `server`, `profiler`) |
| `engine-id` | Numeric engine ID |
| `type` | Sub-metric type (e.g., CPU mode for mpstat: `usr`, `sys`, `irq`) |
| `dev` | Device name (for storage or network metrics) |
| `cmd` | Process command name |

The `remainingBreakouts` field in the API response is the
authoritative source for what dimensions are available on a
given metric.

#### Per-pair analysis

Runs with multiple parallel engine pairs (`"ids": "1+2"`)
produce combined metric values by default.  To separate
results per pair, add a breakout on `csid` or `engine-id`.

Note that benchmark metrics and tool metrics use different
`csid` label formats.  Benchmark `csid` values are numeric
(e.g., `1`, `2`), while tool `csid` values include the
endpoint and tool name (e.g., `remotehosts-1-sysstat-1`).
Use the `remainingBreakouts` response to discover which
dimensions are available, then query breakout values with
`POST /api/v1/iterations/breakout-values` to see the actual
values before filtering.

## The web dashboard

CDM includes a React-based web dashboard for interactive
result analysis, served by the CDM server.

### Search view

Filter and select iterations for comparison:

- Filter by benchmark, tags, parameters, user, date range
- Select multiple iterations across different runs
- Persistent selection state — selections survive navigation

### Compare view

Side-by-side comparison of selected iterations:

- Bar charts showing primary metric values
- Error bars from multiple samples (min/max/standard
  deviation)
- Group iterations by parameter value, run, or tag
- Overlay supplemental metrics (tool metrics like CPU
  utilization alongside benchmark throughput)

### Deep-dive view

Time-series analysis of individual metrics:

- Line charts with time on the X-axis
- Configurable resolution (single aggregate value or N
  time-series points)
- Overlay multiple iterations aligned by relative time
- Per-sample or averaged across samples
- Interactive breakout exploration:
  - Right-click to add/remove/filter dimensions
  - Isolate individual lines
  - Zoom and pan
- Legend toggle for showing/hiding specific series

### Shareable URLs

Dashboard state (filters, selections, view, breakouts) is
encoded in the URL hash. Sharing the URL reproduces the
exact same view, enabling collaboration and issue tracking
with direct links to specific analyses.

## Crucible commands for CDM

### Indexing results

```bash
crucible index <run-dir>
```

Generates CDM documents and indexes them into OpenSearch.
This runs automatically after a benchmark completes, but
can be run manually to re-index or index imported results.

### Viewing results

```bash
crucible get result --run <run-id>
crucible get metric --run <run-id> --source mpstat --type Busy-CPU
```

Query results from the command line. `get result` shows
the primary metric summary; `get metric` retrieves specific
metric data.

### Managing results

```bash
crucible ls                    # list all runs
crucible ls --type tags        # list runs with tag info
crucible rm --run <run-id>     # remove a run from OpenSearch
```

### Re-processing

```bash
crucible postprocess <run-dir>
```

Re-runs post-processing on existing run data without
re-executing benchmarks. Useful when post-processor logic
has been updated. Follow with `crucible index` to update
OpenSearch.

## Versioning

CDM has evolved through several versions:

| Version | Key changes |
|---------|------------|
| v7dev | Original version, generic `id` fields |
| v8dev | Document-specific UUID fields (`iteration-uuid`, `period-uuid`, etc.) |
| v9dev | Per-month index naming (`cdm-v9dev-metric_data@2026.06`), additional document types |

Multiple CDM versions can coexist in the same OpenSearch
instance. The `services.json` OpenSearch configuration
specifies which CDM version each instance uses, and the
`index-to` / `query-from` settings control where new results
go and which versions are searchable.

This enables gradual migration — new results can be indexed
in v9dev while older v8dev results remain queryable.
