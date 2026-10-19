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

Combining related workloads into one subproject does not establish that they
use the same native implementation. Zathras's STREAM wrapper contains an OpenMP
STREAM source, while its NUMA STREAM wrapper launches AMD STREAM Dynamic and
installs jemalloc. Installing an allocator does not establish its use: this
wrapper omits the AMD build script's jemalloc flag, which defaults to disabled.
Inspect the actual compile/link command and preload environment before recording
the effective allocator. Inspect both launch paths before choosing a shared
adapter. Preserve material differences through explicit workload identity and
documented parameters rather than assuming NUMA placement is the only difference.

## 2. Keep build inputs and benchmark parameters separate

For database workloads, verify native connection selection as well as readiness.
HammerDB's MariaDB driver selects a Unix socket for a local host if its socket
setting is non-null, even when the host is `127.0.0.1`. A successful external
TCP login does not establish that this different authentication path works.
The reviewed adapter forces the native TCP path with the literal `null` socket
setting and validates the same generated account.

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

Check actual binary storage before rejecting prepared variants as too large.
During STREAM design review, two OpenMP builds with array lengths 1,048,576 and
67,108,864 produced identical 22,160-byte executable sizes: their different array
allocations were in a `NOBITS` BSS section. This does not reduce runtime memory
needs. Evaluate image size, build cost and runtime allocation separately, and
keep the native workload identity intact when choosing a variant strategy.
Compile each declared size: in the reviewed x86_64 GCC build, a 1 GiB-per-array
variant failed to link under the default code model, while a 2 GiB-per-array
variant linked with `-mcmodel=medium`. Keep such target-specific build flags in
Workshop provenance and verify other architectures independently.

## 3. Verify defaults through the real tools

Keep dependency checks separate when software generates executable scripts.
HammerDB v6 `loadscript` successfully generated database workloads on a host
where explicit `package require mariatcl` and `package require Pgtcl` failed
because shared client libraries were absent. Startup and `ldd` of the CLI did
not establish those dependencies. Record script generation, actual package
loading, database connectivity and native execution as distinct checks. The
first Workshop-built Alma9 trial subsequently passed both database workloads;
this does not qualify other userenvs or architectures.

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

Inspect `presets.defaults` as well as `essentials`; fio demonstrates their
different purposes. In the Multiplex implementation inspected for STREAM,
`override_presets()` applies `defaults` only to an entirely empty parameter set,
not to each missing field of a partial set. Start-script defaults therefore
matter when a user supplies only some parameters. Match the intended defaults
in both interfaces, and verify partial requests through the CLI and adapter.
Do not use `essentials` to fill ordinary optional values when that would
replace an explicit user selection.

Endpoint settings and benchmark parameters are different interfaces. For
example, disabling tool execution did not prevent default tool image selection
in the initial CoreMark run. An explicit empty `tool-params` list selected the
intended benchmark-only image path. Inspect the actual image request too.

## 4. Test the declared executable entry points

A service UID must traverse every storage ancestor. Rickshaw's engine sample
parent is created with `mkdtemp()` and mode 0700; making a benchmark child
directory traversable cannot grant `mysql` or `postgres` access through that
parent. Check permissions under the actual service UID. Use adapter-owned
traversable parents or explicit writable mounts, keep database leaves private,
and leave framework directories and caller-owned mount roots unchanged.
HammerDB's mechanics default uses an owned `/tmp` root and records its backing
filesystem; this does not establish parity with a dedicated storage device.
Its `log-dir` places native redo/WAL, while diagnostics are archived separately.
Verify native variables, symlinks and files before shutdown, then owned-tree
removal and absence of live processes afterward.

A mounted sequential MariaDB repeat exposed a TCP-port reuse edge case. The
first sample succeeded and removed its database daemon, process group and
owned data/log children; the next sample rejected engine port 40000 before
starting a daemon. A target-side live probe found no owned process or directory
left from the first sample, but `/proc/net/tcp` showed that port only in state
`06` (`TIME_WAIT`). A bind/listen probe with `SO_REUSEADDR=0` returned
`EADDRINUSE`; enabling reuse allowed the completed port to be bound again.
For engine-local listener checks, enable `SO_REUSEADDR` and perform both
`bind` and `listen`: bind alone can share a port with another reuse-enabled
socket, while `listen` still rejects a live overlapping listener. Cover
listeners bound to both `127.0.0.1` and `0.0.0.0`, with reuse enabled and
disabled. Keep daemon startup as the final owner of the port and fail closed if
another process binds between preflight and startup; never fall back to
connecting to an existing database. Local regression tests exercised a real
loopback `TIME_WAIT` state and all four listener combinations. The mounted
repeat subsequently passed all eight samples with the corrected adapter,
including both MariaDB repeats. Independent review matched all native/API
values and verified owned process/storage cleanup.

Rickshaw invokes its configured hooks directly. A postprocessor that runs with
`python3 script.py` can still fail in Crucible if its Git mode is not executable.
The first CoreMark run found exactly that defect.

Resolve each path from `rickshaw.json` and verify its shebang, Git executable
mode, required environment and working-directory assumptions. Exercise the
declared entry point itself, including runtime and postprocessing hooks.
Retain the failure artifacts so a corrected postprocessor can be evaluated
without repeating an expensive workload unnecessarily.

## 5. Treat native output as the authority

Build fixtures from the pinned native implementation and then captured output.
HammerDB schema operations catch some errors internally; exit status zero and
an adapter completion sentinel are insufficient. Require native build/check
success, successful schema VU status and the expected SQL warehouse count.
Its serial schema build has one VU, while the timed run adds a monitor. Native
schema labels differ by backend and worker messages include `Vuser 1:`.
Invented fixture wording passed unit tests but rejected real native forms;
source review corrected this before the first database trial, whose captured
output confirmed the parser. Postprocessing must revalidate retained setup
logs as well as timed output, iteration arguments and engine identity.

Keep the raw output, effective parameters, process status, timing and build
provenance together. Require native correctness checks before emitting a valid
performance metric. A zero process exit status alone may not establish native
benchmark validity.

Bind postprocessing to the iteration arguments as well as the retained output
and manifest. STREAM review found that a hook ignored iteration arguments and
could accept a valid Triad artifact for a Copy request. Internal
agreement between two files cannot detect a valid artifact from the wrong
iteration. Compare effective parameters, including start-script fallbacks,
before publishing a score.

For CoreMark this means checking both standard seed modes, expected CRCs and
context counts, iteration/score consistency, and the native minimum duration.
Only the performance mode supplies its throughput metric.

Record calibration and validation separately from measured work. When native
output provides duration but no absolute timestamps, document any reconstructed
measurement endpoints as estimates, explain the anchoring method and its
uncertainty, and check that they fit within the recorded process bounds.

If a process envelope is represented by nanoseconds but the metric interface
requires integer milliseconds, round its start down and completion up. Flooring
both endpoints can trim up to a millisecond from the completion bound. State
this quantization explicitly; an outward-rounded process envelope still does
not identify the native kernel's measured interval.

Test invalid output, manifest/raw-output disagreement, timeouts and interrupted
execution. A failed run must not produce a valid-looking performance score.
Clean up owned processes, including child process groups, on failure and abort.
Distinguish an adapter signal test from a full Crucible abort test.

Check process-group members even after the directly owned leader exits. A
zombie leader can make `killpg(group, 0)` appear live; reap it while checking
actual member state. Conversely, a reaped leader can leave live descendants.
Gate deletion of owned database and transaction-log files on confirmed group
shutdown, and keep deadlines active after stdout EOF. Owned helper tests
exercised zombie reaping, exited-leader descendants and stdout EOF supervision
during HammerDB review. Dedicated MariaDB and PostgreSQL adapter SIGTERM
trials inside Crucible subsequently verified cleanup and client-start-end
roadblock failure propagation. Both samples were invalid, with no completed
measurement timestamps, score, or indexed metric/measurement-period records.
Direct controller SIGINT or immediate cancellation remains untested.

A native counter-window start marker and running-worker logs are diagnostic
evidence for an interrupted sample, not a completed measurement. CLI process
age at signal receipt is not measured workload duration. Failed-run parameter,
tag and run metadata may still be indexed while metric queries return structured
`RUN_NOT_FOUND`; verify absence of accepted metric and measurement-period
records rather than claiming the entire run is absent from the index.

## 6. Define CDM aggregation before debugging CDM

Validate generated metric descriptors against stock CDM `indexDefs`, including
`metric_desc.names`. `CDMMetrics` accepts a names dictionary, but arbitrary keys
are not necessarily indexable: STREAM completed 16 native samples and document
generation before indexing rejected its unknown `result-variant` field. The
supported `desc` name can carry the `STREAM-derived` annotation. Require
successful indexing and ordinary period-scoped queries before claiming
integration acceptance. Valid native samples can be reprocessed for a
postprocessor-only correction; record the original native adapter and revised
postprocessor identities separately.

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

Inspect the generated period bounds before predicting a concurrent-client
summary. In the Rickshaw revision inspected for STREAM, same-name client
periods are combined using their intersection, not their union. Derive the
expected value from the actual query window and stock CDM weighting, including
its inclusive millisecond bounds and treatment of partially overlapping data.
Source inspection and synthetic endpoint-weighting checks do not establish
live concurrency acceptance. Summing native best-rate summaries also does not
establish instantaneous combined bandwidth when the kernels execute at
different times within their process envelopes.

Check single-sample summary semantics too. The inspected stock summary uses
sample standard deviation, which is undefined for one observation and appears
as JSON `null`. Do not replace that with zero in an independent acceptance
calculation; compare mean/min/max and require the expected undefined dispersion.

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

NUMA topology discovery and workload placement are separate interfaces.
Uperf's `cpu-pin=numa` locates the NIC's NUMA node and applies CPU affinity;
it does not bind memory. Toolbox's sysfs topology helpers describe CPUs and
nodes, but do not establish what a container is permitted to use. Verify
effective CPU affinity and allowed memory nodes before selecting CPU and memory
policies. Inspect allocation and first-touch behavior in the actual native
workload before describing a placement mode as local or remote memory.

Check inherited runtime environment variables as well as the process mask.
During STREAM review, a small GNU OpenMP helper showed that inherited
`OMP_PLACES={1}` caused two threads to use CPU 1 despite `GOMP_CPU_AFFINITY=0,1`;
clearing `OMP_PLACES` restored the requested split across CPUs 0 and 1.
Normalize or explicitly account for conflicting OpenMP placement controls, and
verify effective worker placement rather than inferring it from the launch
command alone. This helper check does not establish live STREAM placement.

Verify each topology helper's return contract. Toolbox's
`get_thread_siblings(cpu)` excludes the requested CPU itself. STREAM's first
grouping attempt lost SMT relationships and could select duplicate CPUs on
wider SMT systems. Form the group from the CPU plus its siblings, intersect
with permitted CPUs, and assert that the selected CPU list is unique. Use a
fixture matching the actual helper rather than an assumed inclusive API.

Memory availability also depends on cgroup hierarchy. A leaf with an unlimited
`memory.max` can still be constrained by a finite parent. Walk visible cgroup
ancestors and account for their current usage and applicable limits, along
with host availability and explicit node placement. Fixture validation of a
finite parent limit is separate from a live constrained-container check.

For restricted allowed memory nodes, host-wide `MemAvailable` can include
memory the workload cannot allocate. STREAM review reproduced this gap with
ample global memory and insufficient free memory on the permitted node.
Account for observable permitted-node capacity, and fail clearly if the
topology needed for that check is incomplete. This remains a snapshot guard;
inherited memory policies and concurrent allocations need separate evidence.

## 8. Validate a development installation before publication

Use a local benchmark repository and unofficial registration first. Keep it
separate from unrelated development checkouts. Crucible runs as root; verify
that the controller's container can resolve any symlink to the local repository.
Reuse approved registry configuration and the user's existing SSH identity.
Keep credentials and private infrastructure details out of published artifacts.

Record the framework revisions, benchmark revision or staged tree, run file,
expanded parameters, image identity, raw logs and queries used for acceptance.
A staged tree identifies contents and modes but is not a published commit.

Verify that every Workshop file input survives source-image-service bundling.
STREAM's first full trial failed before native execution because its required
`NOTICE.md` existed in the local checkout but was omitted from the image
request. The inspected client and service exclude `.md` files, along with
documentation directories. Keep build-required notices in an included format
and location, and check the materialized bundle as well as the local recipe.
A direct cached-image build with local mounts does not exercise this transfer.

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

Verify CI selection in the generator and reusable workflow, not just the
schema or scenario. At the Rickshaw revision inspected for STREAM, the schema
permits a per-benchmark userenv allowlist, but the job generator does not apply
it. The reusable workflow selects a global userenv matrix, and run-file
creation overwrites an explicit scenario userenv. Its Rickshaw checkout also
uses `master`; a benchmark development branch alone cannot supply a new CI
benchmark enum and scenario. Coordinate those dependencies or use an explicit
development workflow to test them. Treat unqualified matrix lanes as coverage
work to complete, and back support claims with actual builds and runs.

Check the first failing lifecycle phase before editing code. For example,
CoreMark execution passed while a missing executable bit broke postprocessing;
later, disk pressure blocked OpenSearch writes. An index command can report
success without proving the data is queryable. Query the run and compare its
values independently instead of trusting the command's exit status alone.

Compare the expected iteration/sample matrix with the retained harness status
and accepted indexed results. In HammerDB's mounted baseline trial, two repeated
MariaDB samples failed while six native samples succeeded. The controller exited
zero and the ordinary summary reported `partial=false`, but omitted both failed
MariaDB iterations, including their successful first samples. The PostgreSQL
metric/period records alone were indexed, and only its iterations were
summarized. A clean exit or a nonpartial
summary therefore did not establish that the requested matrix passed. Retain
failed-attempt diagnostics and check missing iterations explicitly.

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

## 10. Port suite-selected payloads as native benchmarks

A suite profile may select a patched or repackaged workload rather than the
pristine upstream release. Compare the profile payload with the upstream source,
pin and document both inputs, review the differences, and apply any required
changes as a checked-in patch. Preserve the source license and attribution. The
benchmark subproject should invoke the native workload directly; do not add the
suite harness as a runtime dependency when the goal is to support that workload
in Crucible.

PHPBench 0.8.1 demonstrated why native output needs row-level validation. Its
fixed search paths can discover the same tests twice when installed under
`/opt/phpbench`; it still prints `Tests: 56`. Install outside those search paths
and validate the full ordered set of 56 timed rows, including uniqueness and
completeness. Also distinguish suite repetition from native work: PTS
`TimesToRun=3` maps to Crucible `num-samples=3`, while PHPBench's `-i 2000000`
is the native `iterations` benchmark parameter.

## 11. Validate Workshop archives before root extraction

Workshop build stages can run with root privileges. Verify the pinned archive's
size and digest before extraction, reject unsafe paths, duplicate members,
links, special files, unsafe modes and oversized contents, and extract only
validated files into controlled staging. Compare the extracted inventory with
the archive before applying patches or running build steps; validating only
after ordinary extraction cannot undo writes outside the source tree.

Record effective runtime configuration as part of build provenance when
environment variables can change native behavior. PHPBench clears `PHPRC` and
`PHP_INI_SCAN_DIR` overrides but keeps normal PHP configuration loading enabled;
using `php -n` would change the runtime. Its manifest records configuration
file hashes, effective directives, active modules and package identities, and
the client checks them before execution. Include preflight and result validation
in the client deadline, reserve time for process cleanup before the framework
roadblock, and test signal/timeout cleanup against the owned process group.
