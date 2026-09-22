# MCP Run Planning Design

Status: implemented across Crucible, Rickshaw, and Multiplex.
This document records the ownership boundaries and contract behind the planning
capability; the public MCP tools are `prepare_run` and `estimate_run`.

## Problem

The MCP interface can validate a run document and submit it to `start_run`, but
it cannot explain what the document expands into before execution. A coding
agent can therefore discover that a run is syntactically valid only after it
has already made decisions about benchmark parameters, samples, tools, and
endpoint placement.

Crucible does not have an authoritative runtime estimator that can simply be
wrapped. Rickshaw expands benchmark parameters and constructs the execution
order as part of run startup. Benchmark `*-get-runtime` helpers are executable
scripts, not static metadata, and the CLI still has no dry-run command that
returns a normalized plan. The new MCP planner therefore reports static counts
and explicitly marks runtime as unavailable.

The implementation establishes a reusable, read-only planning layer before the
MCP adapter. The planner describes what can be known from configuration without
pretending that deployment or benchmark execution outcomes are known in
advance.

## Goals

The planning layer should:

* reuse the run-file schema and the same expansion rules used by execution;
* report the benchmark instances, parameter expansion, sample count, and
  execution count that the submitted document implies;
* describe selected tools and endpoint topology when those values are
  statically available;
* distinguish exact, derived, estimated, and unavailable values;
* provide bounded output suitable for an MCP client;
* be deterministic for the same input and installed component versions; and
* remain side-effect free: no deployment, container start, CDM write, index
  mutation, or benchmark execution.

## Non-goals for the first version

The initial planner will not:

* predict benchmark performance or resource utilization;
* execute a benchmark's `*-get-runtime` helper;
* invoke endpoint deployment or engine discovery;
* guarantee wall-clock duration, especially for parallel or remote runs;
* render a run directory containing generated credentials and runtime files; or
* replace `start_run` as the authority for whether a run can execute.

Those operations either require execution, depend on mutable infrastructure, or
would create a second implementation of Crucible's runtime behavior.

## Current execution boundaries

The plan must follow the existing architecture rather than inventing a parallel
run model:

* `validate_run` validates the JSON document against Rickshaw's run-file schema
  and checks that named benchmarks are installed.
* Rickshaw loads benchmark parameter data and uses the `multiplex` subproject to
  apply defaults, conversions, validation, and Cartesian expansion.
* `num-samples` controls repeated executions of each expanded iteration, while
  `test-order` changes ordering, not the number of executions.
* Benchmark `ids` refer to engines described by the endpoint section. Endpoint
  implementations determine how those engines are deployed, and some topology
  details are not knowable without contacting the target.
* The configured tool set is selected separately from benchmark parameters and
  may use its own multiplexed configuration.
* An omitted `tool-params` uses Rickshaw's default tool set, while an explicit
  empty `tool-params` disables default tool collection; the planner must retain
  that distinction.

The planner should share these rules with Rickshaw. A hand-written MCP-only
expander would eventually disagree with the run that `start_run` executes.

In particular, separate benchmark occurrences are not a cross-benchmark
Cartesian product. Each occurrence is expanded independently, then Rickshaw
merges the resulting parameter sets into index-aligned global iterations. The
planner must preserve duplicate occurrences and report both per-occurrence
counts and the resulting global count. It must not calculate the total by
summing or multiplying benchmark occurrence counts.

## Proposed architecture

### 1. Canonical input and validation

Create a Python planning module in the rickshaw subproject, with Crucible
providing the MCP transport and policy adapter. The planner should accept the
same mutually exclusive inputs as `validate_run` and `start_run`:

* an inline JSON object; or
* a run-file path accepted by the existing input policy.

The module should canonicalize the document, validate it first, and compute an
input digest from the canonical JSON and relevant planner contract version. The
digest identifies the planned input without exposing filesystem paths or
credentials. Invalid input returns the existing structured validation errors;
it must not produce a partial plan that looks executable.

### 2. Shared normalized plan model

The internal model should be a plain, serializable object with explicit bounds
and no live process or filesystem handles. At minimum it should contain:

```text
RunPlan
  contract_version
  input_digest
  validation: {valid, errors, warnings}
  benchmarks[]
    name
    occurrence
    engine_ids (bounded, with count and truncation marker)
    parameter_sets (bounded summaries or an omitted/truncated marker)
    iteration_count
    sample_count
    sample_execution_count
  totals
    benchmark_count
    global_iteration_count
    sample_execution_count
  tools[]
    name/id
    selected
    parameter_status
  topology
    endpoint_types
    engine_counts
    confidence: exact | derived | unknown
  runtime
    per_benchmark[]
    serial_seconds
    wall_clock_seconds
    confidence: exact | estimated | unavailable
    unknown_reasons[]
  limits
    truncated
    warnings[]
```

Counts are preferable to returning every expanded parameter value. When a
client requests details, the planner may return a bounded prefix together with
the total count and an explicit `truncated` flag. A response must never imply
that a prefix is the complete expansion.

The first version can report runtime as unavailable. If a future component
provides trusted static runtime metadata, it must identify the source and
confidence rather than silently turning a hint into a guarantee. The existing
runtime helper scripts must remain outside the default planning path because
they are arbitrary executable benchmark code.

### 3. Expansion adapters

The planner should have narrow adapters around existing sources of truth:

* **Run-file adapter:** applies documented defaults such as one sample and the
  configured test order, and resolves benchmark occurrences and engine IDs.
* **Multiplex adapter:** exposes a library-level, side-effect-free expansion
  function from `multiplex` or extracts one from its current implementation.
  It must use the same validation, preset, conversion, and Cartesian-product
  behavior as execution. Calling the multiplex CLI as a shell subprocess is
  not acceptable for an MCP request.
* **Benchmark adapter:** reads trusted installed metadata and multiplex
  requirements under the managed Crucible tree. Descriptive metadata is not a
  substitute for runtime behavior.
* **Tool adapter:** reports selected tools and whether their static parameter
  configuration could be validated. Tool setup that depends on endpoint state
  remains `unknown` rather than being guessed. Tool multiplexing is a separate
  flat-parameter path and must not be treated as benchmark Cartesian
  expansion.
* **Endpoint adapter:** derives only topology present in the run document and
  installed endpoint metadata. Dynamic remote, Kubernetes, or OSP capacity is
  reported as unknown until a separate, explicitly authorized discovery
  operation exists.

## Repository ownership and integration

The implementation must not reproduce rickshaw behavior inside Crucible. The
repository boundaries are:

| Repository | Responsibility |
| --- | --- |
| `multiplex` | Importable parameter expansion, defaults, conversions, validation, and bounded cardinality calculation, while preserving its CLI behavior. |
| `rickshaw` | The canonical `RunPlanner`, run-file normalization, benchmark/tool composition, index-aligned iteration merging, samples, and the versioned plan model. |
| `crucible` | MCP authentication, input policy, tool schemas, response-size limits, redaction, and adaptation to the rickshaw planner. |
| Benchmark/tool repositories | Existing manifests; optional future declarative runtime hints, without requiring changes for the first planner. |
| Endpoint repositories | Existing endpoint behavior; optional future non-mutating topology metadata. |

The preferred call path is:

```text
MCP prepare_run / estimate_run
        -> Crucible policy and input validation
        -> rickshaw RunPlanner
        -> multiplex library
        -> bounded RunPlan
        -> MCP response
```

The planner is implemented and tested in `multiplex` and `rickshaw`, including
bounded expansion tests and adapter tests. Future golden tests can compare
planner output with the
configuration produced by real run preparation. Crucible should then expose
thin MCP adapters instead of importing mutable `RunState` internals or
reimplementing expansion logic.

Changes to rickshaw or multiplex source do not by themselves require a
controller-image rebuild. The image-build workflow should rebuild the image
when the relevant dependency metadata, such as `workshop.json`, changes. The
existing GitHub/CI dependency workflows remain responsible for detecting and
running that rebuild when it is needed.

### 4. Public MCP operations

The stable public adapter exposes two operations:

#### `prepare_run`

`prepare_run` is a deterministic preparation/inspection operation, not a
deployment command. It validates the input and returns a bounded normalized
plan plus safe structural details. It must not create a runnable directory,
generate credentials, start containers, contact an endpoint, or reserve
resources.

Its purpose is to let an agent inspect exactly what it is about to submit. The
name should not imply that execution resources have been prepared.

#### `estimate_run`

`estimate_run` consumes the same input and returns the plan's derived counts and
runtime information. A later version may accept an opaque, immutable plan
handle returned by `prepare_run`, but that persistence model should not be
introduced until its retention, authorization, idempotency, and stale-plan
semantics are specified.
The initial result should clearly say that runtime is unavailable unless a
trusted static source exists. It should not fabricate a duration from a
benchmark name or from a generic default.

Both tools should accept explicit limits such as maximum expanded iterations,
maximum parameter summaries, and maximum response bytes. The server must cap
them to safe configured ceilings; client-supplied larger limits must not turn
planning into an unbounded expansion operation. The Crucible adapter currently
rejects more than 100 benchmark occurrences and rejects a request whose
per-occurrence parameter-set ceiling would permit more than 100,000 aggregate
expansions or 1,000,000 aggregate parameter entries. Crucible intentionally
follows the configured Rickshaw and Multiplex revisions (currently their
primary `master` branches) rather than pinning planner commits in
`config/repos.json`. The planner APIs must therefore be available in those
configured revisions before these tools are enabled; the tools must not
silently depend on incompatible versions installed on a host.

The tools are registered in `crucible_info`, `tools/list`, and the MCP interface
table. `validate_run` remains useful for a quick schema-only check; planning is
the optional next step when a client needs bounded expansion and topology
details before submission.

A separate `render_run` operation is deliberately deferred. If clients need
paginated parameter-level inspection later, it should read an immutable plan
and return redacted structural data—not shell commands, generated credentials,
or a runnable directory. For the first version, that inspection belongs in the
bounded `prepare_run` response.

## Relationship to `start_run`

`start_run` remains the authoritative execution path. Clients that call
`prepare_run` can pass its `input_digest` as `plan_digest`; `start_run` then
re-plans the submitted input immediately before launch and rejects a stale
digest rather than executing a document that no longer matches the inspected
plan. The submission response includes the verified digest, derived counts,
runtime confidence, and planning limits alongside the MCP job.

This is an integrity check, not a promise that remote capacity or mutable
deployment state has remained unchanged. Clients that omit `plan_digest` retain
the normal validation and execution behavior without the additional planning
gate.

The first implementation should not persist plan documents as durable jobs.
Plans are cheap, deterministic views of input; persistence would add lifecycle,
retention, and authorization questions without helping execution recovery. If a
future `prepare_run` creates a plan handle for `start_run`, it must be immutable,
service-owned, bounded, expiring, idempotent, and fingerprinted against the
input plus the relevant installed component versions. `start_run` must
revalidate that fingerprint before execution.

## Security and resource bounds

The planner is an authenticated read operation, but it still processes
attacker-controlled JSON and potentially large parameter matrices. It must:

* use the existing input-root policy for file inputs;
* reject symlinks and paths outside approved managed roots as current input
  operations do;
* impose byte limits on inline documents and installed metadata;
* cap benchmark count, tool count, engine IDs, parameter sets, and expanded
  iterations independently;
* stop expansion before materializing an oversized Cartesian product;
* return `truncated: true` and counts/continuation information when a limit is
  reached;
* never run shell commands, benchmark helpers, or endpoint code;
* avoid returning secrets, credentials, environment files, or arbitrary source
  contents; and
* bound the serialized MCP response, including the duplicated text and
  structured payloads used by the current server response format.

The planner should treat a missing or unreadable optional metadata source as an
explicit unknown/incomplete field. It must not report an empty list as a
complete expansion when the source could not be read.

## Error and confidence model

The response should separate user input errors from planning limitations:

* `invalid_run`: the run document fails schema or installed-component checks;
* `planning_limit`: the plan is valid but expansion was stopped by a configured
  bound, with a partial bounded result and `truncated: true`;
* `metadata_unavailable`: a required installed component or optional metadata
  source could not be read;
* `topology_unknown`: deployment-dependent counts could not be derived before
  execution; and
* `runtime_unavailable`: no trusted static duration source exists.

These conditions should be represented in structured fields as well as the
human-readable messages so an agent can decide whether to ask for approval,
continue with a partial plan, or stop.

## Rollout plan

1. **Expansion reuse:** expose a library-level multiplex expansion API while
   preserving the CLI behavior. Add fixtures covering presets, unit conversion,
   empty sets, duplicate benchmark occurrences, index-aligned composition, and
   Cartesian-product limits.
2. **Planner model:** add the bounded `RunPlan` model in rickshaw, the canonical
   input digest, response schema, and pure unit tests for limits and confidence
   states. Add golden tests against real run preparation.
3. **Adapters:** add benchmark, tool, and endpoint adapters with explicit
   unknown/incomplete results. Do not execute runtime helpers.
4. **MCP operations:** implement and test `prepare_run` and `estimate_run` in
   Crucible as thin planner adapters, including tool schemas, discovery
   metadata, structured errors, and response bounds.
5. **Execution integration:** return the plan digest/counts from `start_run`
   when a client supplies `plan_digest`, and reject stale plans after
   re-planning the submitted input.
6. **Documentation:** update the MCP interface table and agent workflow only
   after the public contract is implemented; add examples that handle valid,
   truncated, unknown, and invalid plans.

## Open decisions before implementation

* Should `prepare_run` return the full normalized document, or only a digest and
  bounded summaries? Returning the full document is useful but increases secret
  exposure and response size.
* Should the planner support a durable server-side plan handle, or should each
  operation re-read and re-plan its input? Re-planning is simpler and avoids
  stale handles.
* Which installed metadata, if any, is authoritative enough for a static
  runtime hint? The default should remain unavailable until that source is
  defined.
* Should endpoint topology expose only declared engine IDs, or also an
  explicit count of unresolved deployment capacity? The latter is clearer for
  agents but needs a stable schema.
* What maximum expansion and response budgets should be configurable, and which
  values are safe defaults for the controller image?

## Related documentation

* [Crucible architecture overview](crucible-architecture-overview.md)
* [How benchmark execution works](how-benchmark-execution-works.md)
* [How engines work](how-engines-work.md)
* [How endpoints work](how-endpoints-work.md)
* [Implementing a new benchmark](implementing-a-new-benchmark.md)
* [Implementing a new tool](implementing-a-new-tool.md)
* [MCP agentic performance workflow](mcp-agentic-perf-workflow.md)
