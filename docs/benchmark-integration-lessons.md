# Benchmark integration lessons

This living document records practical lessons from adding benchmarks to
Crucible, beginning with CoreMark.
Use it alongside [Implementing a New Benchmark](implementing-a-new-benchmark.md).
The implementation guide defines the interfaces; these lessons explain what
to inspect, what can go wrong, and what evidence is needed before publication.
Check the current code and schemas when applying them to another benchmark.

Read this document before starting the next benchmark. Extend it when another
integration reveals an important detail: explain the failure or surprise, the
owning interface, and how the next agent can verify the behavior. Distinguish
observed behavior from hypotheses and pending validation. Correct or retire
lessons when the underlying code changes. Keep private infrastructure and
credentials out of examples, and link detailed interface specifications rather
than duplicating them here.

## 1. Establish the workload contract before writing the adapter

Read the source harness, its benchmark wrapper, any shared wrapper helpers,
and the native benchmark. Read comparable Crucible subprojects as well.
Record:

- The required client and server roles, including defaults when roles are
  omitted. A wrapper's name or packaging does not establish its execution model.
- What each parameter means, its default, and its valid range. An option named
  `iterations` may mean repeated benchmark executions in one wrapper and native
  work iterations in another.
- Whether parallelism means threads in one process, independent processes,
  multiple engines, or some combination of these.
- What makes a result valid: native validation, minimum measurement duration,
  units, and the meaning of the headline score.
- Which system configuration is required and which tuning is optional.

Many workloads migrated from Zathras may only need a client role. Confirm this
from the launch path instead of adding a placeholder server. In Rickshaw,
inspect both schema defaults and the code that applies them.

CoreMark's native score already combines the contexts within one process.
Its validation modes and minimum measurement duration must remain intact.
Those facts are more useful than copying a wrapper's command line unchanged.

## 2. Keep build inputs and benchmark parameters separate

Workshop and userenvs supply the software environment. Multiplex and the
benchmark adapter select supported workload behavior at runtime.

| Concern | Where to implement and verify it |
|---|---|
| Packages, source acquisition, compiler and installed binaries | Workshop recipe and the selected userenv |
| A compile-time variant, such as a prepared thread count | Workshop build inputs and installed variant provenance |
| Runtime variant selection, native work count and deadline | Multiplex validation, adapter parsing and run manifest |
| Endpoint deployment and CPU partitioning | Endpoint settings and engine bootstrap |
| Reported metrics and aggregation | Postprocessor, CDM descriptors and query results |

Do not carry over a source harness's package assumptions. A list of DNF package
names is not evidence that each Crucible userenv can acquire those packages.
Use Workshop's supported source-build mechanisms where packages are unavailable.
Build and install during image preparation; the benchmark client should execute
the prepared software without fetching dependencies or compiling it.

For source builds, pin a revision and retain compiler flags, source integrity
checks, binary hashes and build logs. Include the actual port headers and build
files among relevant integrity inputs. CoreMark's pinned upstream integrity
check had a stale header digest: record such a failure honestly and explain any
independent verification rather than claiming the upstream check passed.

## 3. Verify defaults through the real tools

In CoreMark's first adapter, Multiplex `essentials` replaced explicit requested
values. A request for one thread expanded into two thread variants. A JSON
schema check and parser tests did not catch that behavior.

Exercise the installed Multiplex CLI with:

- An empty parameter set.
- Each explicitly supported variant.
- A partial parameter set that relies on runtime defaults.
- Explicit nondefault values for every configurable parameter.
- Invalid and out-of-range values.

Compare the expansion, launched command, selected binary and effective manifest.
Keep fallback defaults consistent where the adapter supplies omitted arguments.
Bound native work counts as well as wall-clock deadlines.

Endpoint settings and benchmark parameters are different interfaces. For
example, disabling tool execution did not prevent default tool image selection
in the initial CoreMark run. An explicit empty `tool-params` list selected the
intended benchmark-only image path. Inspect the actual image request too.

## 4. Test the declared executable entry points

Rickshaw invokes its configured hooks directly. A postprocessor that runs with
`python3 script.py` can still fail in Crucible if its Git mode is not executable.
The first CoreMark run found exactly that defect.

Resolve each path from `rickshaw.json` and verify its shebang, Git executable
mode, required environment and working-directory assumptions. Exercise the
declared entry point itself, including runtime and postprocessing hooks.
Retain the failure artifacts so a corrected postprocessor can be evaluated
without repeating an expensive workload unnecessarily.

## 5. Treat native output as the authority

Keep the raw output, effective parameters, process status, timing and build
provenance together. Require native correctness checks before emitting a valid
performance metric. A zero process exit status alone may not establish native
benchmark validity.

For CoreMark this means checking both standard seed modes, expected CRCs and
context counts, iteration/score consistency, and the native minimum duration.
Only the performance mode supplies its throughput metric.

Record calibration and validation separately from measured work. When native
output provides duration but no absolute timestamps, document any reconstructed
measurement endpoints as estimates, explain the anchoring method and its
uncertainty, and check that they fit within the recorded process bounds.

Test invalid output, manifest/raw-output disagreement, timeouts and interrupted
execution. A failed run must not produce a valid-looking performance score.
Clean up owned processes, including child process groups, on failure and abort.
Distinguish an adapter signal test from a full Crucible abort test.

## 6. Define CDM aggregation before debugging CDM

CoreMark initially declared throughput with `default-aggregation=avg`. That
decision led us to patch CDM before adequately comparing existing benchmark
contracts. The correct default for total throughput across independent clients
is `sum`, as used for fio and trafficgen throughput.

There are separate aggregation questions:

| Question | Example |
|---|---|
| What does the native process report? | CoreMark already totals its own threads; do not multiply the score by thread count again |
| How do concurrent metric series combine? | Independent clients producing 10 and 30 iterations/sec contribute total throughput 40 |
| How does a series summarize time? | A rate is weighted over its measurement interval; `sum` across clients does not sum every time sample into a rate |
| How do repeated benchmark samples combine? | The result summary separately calculates sample means and standard deviations |

Choose the default aggregation for the metric's meaning across breakout
dimensions. Do not choose `avg` merely because the native metric is a rate or
because repeated benchmark results should be averaged. Inspect existing metrics
with matching semantics, not only matching units or output shape.

The investigation also exposed a genuine CDM defect: period-scoped `avg`
queries can count descriptors belonging to other samples in their denominator.
It is tracked separately in
[CommonDataModel #215](https://github.com/perftool-incubator/CommonDataModel/issues/215).
Original, unpatched CDM code returned all four native CoreMark scores correctly
with `sum`. A shared defect and an adapter contract error can coexist.

Before changing shared framework code:

1. Inspect the adapter's emitted descriptors, labels, IDs and intervals.
2. Compare comparable existing benchmarks and the documented metric contract.
3. Reproduce the calculation using the original framework revision.
4. Separate a benchmark-specific correction from a generic framework issue.
5. Validate multiple sequential samples and concurrent clients. A single client
   and single sample cannot reveal every aggregation error.

Independent review should challenge the metric contract as well as verify the
arithmetic. A patch that reproduces an expected number is not sufficient evidence
that changing the shared component was necessary.

## 7. Make CPU partitioning an explicit support decision

CPU partitioning is optional for CoreMark's initial integration. The accepted
development run used it disabled; target TuneD changes and a reboot were not
needed for that scope.

If a future benchmark requires partitioning, inspect actual engine bootstrap,
kernel CPU isolation and the effective cgroup affinity. Rickshaw can pin its
engine to housekeeping CPUs while supplying a separate workload CPU list.
Intersecting the workload list with an inherited housekeeping-only mask can
incorrectly produce an empty set. A mocked affinity test is useful but does not
establish live partitioned support.

Do not enable partitioning or turn it into a readiness requirement by default.
When it is required, prepare and verify system tuning before testing it; an
endpoint option alone does not create isolated CPUs.

## 8. Validate a development installation before publication

Use a local benchmark repository and unofficial registration first. Keep it
separate from unrelated development checkouts. Crucible runs as root; verify
that the controller's container can resolve any symlink to the local repository.
Reuse approved registry configuration and the user's existing SSH identity.
Keep credentials and private infrastructure details out of published artifacts.

Record the framework revisions, benchmark revision or staged tree, run file,
expanded parameters, image identity, raw logs and queries used for acceptance.
A staged tree identifies contents and modes but is not a published commit.

A useful progression is:

1. Local source build, native validation and adapter checks.
2. Installed schema validation and benchmark discovery.
3. Workshop requirements-stage build and complete engine execution.
4. Multiple samples and prepared variants, followed by native/CDM comparison.
5. Default indexed queries and the ordinary Crucible result summary.
6. Failure/abort handling and the intended userenv/architecture matrix.

State which checks actually ran. A fresh benchmark requirements stage on a
cached base does not prove cold acquisition of all userenv dependencies.
Parser fixtures do not replace native execution. One tested userenv does not
establish support for every userenv named in a future CI plan.

Check the first failing lifecycle phase before editing code. For example,
CoreMark execution passed while a missing executable bit broke postprocessing;
later, disk pressure blocked OpenSearch writes. An index command can report
success without proving the data is queryable. Query the run and compare its
values independently instead of trusting the command's exit status alone.

Preserve data and the development environment while investigating. Do not
silently relax disk thresholds or remove unrelated results and images. When
using an orchestration service, explicitly state the intended harness, the
development installation, and any no-update/no-teardown requirements. Confirm
that benchmark discovery recognizes the unpublished subproject.

## 9. Publish and integrate in dependency order

The benchmark repository must exist at its advertised URL and branch before
the official Crucible catalog refers to it.

1. Review the benchmark source, license/third-party notices, executable modes,
   example run file, metadata, validation evidence and stated support limits.
2. Publish `bench-<name>` under the agreed organization and verify a fresh clone
   of the advertised branch. Keep machine-specific test evidence local.
3. Add the official entry to Crucible's `config/repos.json` through a feature
   branch and PR. Validate it against `schema/repos.json` and confirm discovery.
4. Coordinate the real Rickshaw CI scenario, benchmark reusable workflow,
   intended userenv coverage and examples. An inactive workflow example is a
   template, not evidence that organizational CI passed.
5. Verify installation through the published clone source, rather than only a
   development symlink. Keep remaining CI or matrix work explicit in the PR.

For this migration effort, use the agreed implementation/review roles:
`gpt-6-luna` implements and `gpt-6.1-sol` reviews. Give reviewers the exact source
revision, test commands, raw evidence and unexecuted cases. Keep unrelated shared
framework issues in separate issues or PRs so a new benchmark's dependencies
remain clear.
