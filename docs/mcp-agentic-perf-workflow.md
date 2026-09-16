# Agentic-perf MCP Workflow

This guide describes how an [agentic-perf](https://github.com/atheurer/agentic-perf)
agent can use Crucible through MCP. It is written for coding agents that need
to discover Crucible, construct a run document, execute it asynchronously, and
review the indexed result without reading Crucible's local files directly.

The workflow uses the current Crucible MCP contract (`mcp_contract_version: 2`)
and the tool names returned by `tools/list`. SDK method names vary by MCP
client; the examples below use the language-neutral form
`call_tool(name, arguments)`.

## Recommended agent boundaries

Agentic-perf has separate agents for benchmark execution and result review.
Keep those responsibilities separate when configuring MCP access:

| Agent role | Crucible MCP access | Purpose |
| --- | --- | --- |
| Benchmark | Documentation resources, discovery, `validate_run`, `start_run`, `get_run_status`, `get_run_logs`, `get_run_summary`, and processing tools | Understand Crucible, construct a run, execute it, and monitor progress. |
| Review | `get_run_status`, `get_run_summary`, `list_indexed_periods`, `get_indexed_metric`, and indexed-result reads | Determine result readiness and analyze measurements. |
| Operator/admin | Explicitly approved maintenance tools only | Perform local archive, tag, or indexed-result maintenance when required. |

The MCP server does not provide arbitrary shell access, unrestricted file
reads, or remote archive-backend operations. It does provide local archive
operations—`list_local_archives`, `archive_local_run`, and
`unarchive_local_run`—for the configured local archive directory. A client
should use the curated documentation resources rather than bypassing MCP to
inspect the Crucible installation.

## 1. Discover Crucible and read its documentation

Start by discovering the contract and the user-facing context:

```text
info = call_tool("crucible_info", {})
resources = resources/list()

architecture = resources/read("crucible://docs/architecture-overview")
run_files = resources/read("crucible://docs/run-files")
execution = resources/read("crucible://docs/benchmark-execution")
endpoints = resources/read("crucible://docs/endpoints")
```

If the client supports `resources/read` but not resource listing, use the
search tool to identify a resource and then read the returned URI:

```text
matches = call_tool("search_documentation", {
    "query": "run files benchmark execution endpoints CDM",
    "limit": 5
})
```

`search_documentation` returns resource metadata, not document contents.
Clients without `resources/read` must receive this context through their
host-provided agent instructions or another documentation channel.

The documentation is context, not a substitute for validation. The run
document remains subject to the installed Crucible and endpoint schemas.

## 2. Discover installed benchmarks and tools

The benchmark agent should not assume that a benchmark or tool is installed.
Select a benchmark from the returned list before requesting its details:

```text
benchmarks = call_tool("list_benchmarks", {})
if not benchmarks["benchmarks"]:
    stop and report "no installed benchmarks"

benchmark_name = choose_benchmark(benchmarks["benchmarks"], requested_benchmark)
benchmark = call_tool("describe_benchmark", {"name": benchmark_name})
tools = call_tool("list_tools", {})
```

Use the returned metadata to choose a benchmark and construct its parameters.
The endpoint, engine IDs, user environment, and tool configuration are
deployment-specific; the generic run-file skeleton in
`how-run-files-work.md` is not necessarily executable without those details.

## 3. Construct and validate the run document

Build the run document from the discovered benchmark metadata, endpoint
configuration, and the ticket's requested workload. Prefer inline JSON for a
small document:

```text
run_document = construct_run_from_ticket_and_crucible_docs(ticket, resources)

validation = call_tool("validate_run", {
    "document": run_document
})
if not validation["valid"]:
    stop and report validation["errors"]
```

`validate_run` validates Crucible run documents only. It does not replace the
CLI's development and administration validation modes for multiplex,
workshop, tool metadata, repository, service, or registry configuration.

For a larger document, stage it under the configured MCP `input-root` and use
an approved path instead:

```text
validation = call_tool("validate_run", {"path": approved_input_path})
```

Do not pass a path from an arbitrary workspace. The MCP policy canonicalizes
the path, rejects symlink escapes and unsafe permissions, and limits the
retained input size.

## 4. Submit an idempotent run

Use a stable idempotency key derived from the agentic-perf ticket and logical
benchmark attempt. Do not generate a new key for every retry:

```text
idempotency_key = ticket_id + ":benchmark:" + attempt_number

submission = call_tool("start_run", {
    "idempotency_key": idempotency_key,
    "document": run_document
})

mcp_job_id = submission["job"]["mcp_job_id"]
persist_to_ticket({
    "idempotency_key": idempotency_key,
    "mcp_job_id": mcp_job_id,
    "state": submission["job"]["state"]
})
```

The job ID is the MCP handle. Crucible's logger session ID, Rickshaw run ID,
CDM run ID, and local run directory are separate identifiers and may not be
available immediately. Preserve every identifier returned by status polling;
do not treat one as an alias for another.

If the request is lost and submitted again with the same key and equivalent
document, Crucible returns the original job with `created: false`. Reusing the
key for a different document is a conflict and must be reported to the
ticket, not retried with increasingly modified input.

## 5. Poll lifecycle and bounded logs

Poll the authoritative status tool using the stored MCP job ID:

```text
while true:
    status = call_tool("get_run_status", {"mcp_job_id": mcp_job_id})
    persist_to_ticket(status)

    if status["state"] == "failed":
        report the structured failure and stop

    if status["state"] == "completed" and status["result_status"] == "available":
        candidate_summary = call_tool("get_run_summary", {
            "mcp_job_id": mcp_job_id
        })
        partial_runs = [
            run for run in candidate_summary["summary"].get("runs", [])
            if run.get("partial") is True
        ]
        if partial_runs:
            report the partial status and each run's dropped-engines
            stop
        break

    if status["state"] == "completed" and status["result_status"] == "unavailable":
        report that the result is unavailable and stop

    if status["state"] == "completed" and status["result_status"] == "partial":
        report that the result is partial and stop

    if status["state"] == "completed" and status["result_status"] == "pending":
        report that the result is still pending and continue within the readiness budget

    if status["state"] in ["recovery_required", "unknown_after_crash"]:
        report that manual recovery is required and stop

    if status["state"] in ["queued", "starting", "running",
                            "postprocessing", "indexing"]:
        logs = call_tool("get_run_logs", {
            "mcp_job_id": mcp_job_id,
            "offset": log_offset,
            "limit": 65536
        })
        append_to_ticket_log(logs["text"])
        log_offset = logs["next_offset"]

    sleep(poll_interval)
```

The lifecycle state and result status are separate. A completed workload can
still have `result_status: "pending"` while CDM indexing or consistency work
finishes, so keep polling within the configured readiness budget. Do not enter
result review for `unavailable` results. The runner's result status may still
be `available` for a partial run, so inspect the summary's `partial` and
`dropped-engines` fields before treating the measurement as complete.

If the service restarts, continue polling the same MCP job ID. The managed
runner reconciles the durable job record and does not launch a duplicate
workload for a retry.

## 6. Optionally reprocess or index the run

The normal `start_run` command already runs Crucible's postprocessing and
indexing phases before it reports completion. Do not submit the processing
tools for every successful run. Use them only to repair or reprocess an
approved local run, or to process a run that was created outside `start_run`.

When reprocessing is required, submit the phases as separate asynchronous
jobs. Use the source MCP job ID so the server resolves the approved run
directory without asking the agent to access local paths:

```text
postprocess = call_tool("postprocess_local_run", {
    "idempotency_key": ticket_id + ":postprocess:" + attempt_number,
    "mcp_job_id": mcp_job_id
})
wait_until_terminal(postprocess["job"]["mcp_job_id"])

index = call_tool("index_local_run", {
    "idempotency_key": ticket_id + ":index:" + attempt_number,
    "mcp_job_id": mcp_job_id
})
wait_until_terminal(index["job"]["mcp_job_id"])
```

Both processing calls are asynchronous and idempotent. Persist their job IDs
separately from the source run job ID. If indexing completes while CDM is
eventually consistent, continue polling the source status and retry the
result-read operation within the configured readiness budget.

## 7. Retrieve a summary and query metrics by period

Once the source job reports that results are available, retrieve its summary:

```text
status = call_tool("get_run_status", {"mcp_job_id": mcp_job_id})
summary = call_tool("get_run_summary", {"mcp_job_id": mcp_job_id})
summary_payload = summary.get("summary", {})
summary_runs = (
    summary_payload.get("runs", [])
    if isinstance(summary_payload, dict)
    else []
)
summary_run = choose_summary_run(summary_runs) if summary_runs else {}
cdm_run_id = (
    summary_run.get("run-id")
    if isinstance(summary_run, dict)
    else None
)
if not isinstance(cdm_run_id, str) or not cdm_run_id:
    cdm_run_id = status.get("cdm_run_id")
if not isinstance(cdm_run_id, str) or not cdm_run_id:
    report that no verified CDM run ID is available and stop
```

The CDM run identifier is normally stored in the summary's
summary["summary"]["runs"][*]["run-id"] field. The status
cdm_run_id field is an acceptable fallback; rickshaw_run_id is not a CDM
query identifier.

Runs can contain multiple iterations and primary measurement periods. Always
discover and retain the period identity before querying metrics:

```text
periods = call_tool("list_indexed_periods", {"run": cdm_run_id})
period = choose_period_from_ticket_or_iteration(periods["periods"])

metric = call_tool("get_indexed_metric", {
    "run": cdm_run_id,
    "period": period["primary_period_id"],
    "source": "mpstat",
    "type": "Busy-CPU",
    "aggregation": "avg"
})
```

Do not query a metric with only a run ID when the run has multiple primary
periods. Use `primary_period_id`, or explicitly provide both `begin` and `end`,
so the review is scoped to the intended measurement.

## Failure and retry rules

| Situation | Agent action |
| --- | --- |
| Validation reports errors | Fix the generated run document and validate again; do not submit it. |
| `queued`, `starting`, `running`, `postprocessing`, or `indexing` | Poll status and bounded logs. Do not submit another run. |
| `recovery_required` or `unknown_after_crash` | Report an ambiguous execution to the ticket and stop for manual recovery; never relaunch automatically. |
| `completed` with `pending` results | Poll within the CDM readiness budget, then retry summary/period reads. |
| `partial` results | Report which data is partial and preserve the result identifiers. |
| `failed` or `unavailable` | Preserve the structured error and decide whether a new, explicitly numbered attempt is warranted. |
| Lost response to any asynchronous call | Retry with the same idempotency key and equivalent arguments. |

The review agent should treat the MCP response as the source of truth for
state and identifiers, while the ticket remains the source of truth for the
agentic-perf investigation and user-facing conclusions.
