# How CI Works

This document explains how crucible's continuous integration
system operates — the workflow hierarchy, runner matching,
docs-only filtering, release matrix testing, and post-merge
validation.

For how the capability-based runner matching was designed, see
the `crucible-ci.json` configuration in the rickshaw repo.
For how releases interact with CI, see
[how-releases-work.md](how-releases-work.md).

## Overview

Crucible validates changes across its multi-repo ecosystem
using GitHub Actions. Each repo has a trigger workflow that
calls reusable workflows in the `crucible-ci` repo. This
layered approach means CI logic is centralized — individual
repos only need a thin trigger workflow, while the test
orchestration, runner matching, and integration test execution
are shared.

Key CI capabilities:

- **Docs-only filtering**: PRs that only change documentation
  skip full integration tests
- **Capability-based runner matching**: Scenarios declare
  requirements, runner pools declare capabilities, and the
  system matches them
- **Release matrix testing**: Core repo changes are tested
  across multiple crucible releases
- **Post-merge validation**: Merged changes are re-tested
  against production registries

## Workflow hierarchy

CI uses a multi-level calling structure:

```
Repo trigger workflow (e.g., crucible-ci.yaml)
  └── crucible-ci reusable workflow
        (e.g., core-release-crucible-ci.yaml)
      └── Per-release workflow
            (e.g., core-crucible-ci.yaml)
          └── Per-endpoint workflow
                (e.g., endpoint-crucible-ci.yaml)
              └── Integration test action
                    (integration-tests/)
```

### Level 1: Repo trigger

Each repo has a `crucible-ci.yaml` in `.github/workflows/`
that fires on pull requests. It performs the docs-only check
and calls the appropriate crucible-ci reusable workflow.

### Level 2: Reusable workflows

The `crucible-ci` repo provides reusable workflows for each
repo type:

- **`core-release-crucible-ci.yaml`**: For core repos
  (crucible, rickshaw, toolbox, etc.). Includes release
  matrix and controller build checks.
- **`benchmark-crucible-ci.yaml`**: For benchmark repos.
  Simpler — no release matrix.
- **`tool-crucible-ci.yaml`**: For tool repos. Similar to
  benchmark CI.

### Level 3: Endpoint workflows

The endpoint workflow generates the test job matrix using
`generate-ci-jobs.py` and `crucible-ci.json`, then runs
integration tests for each job in the matrix.

### Level 4: Integration tests

The `integration-tests` action performs the actual test:
installs crucible, configures endpoints, runs benchmarks,
and collects results.

## Core vs benchmark vs tool CI

### Core CI

Core repos (crucible, rickshaw, toolbox, workshop, etc.)
get the most thorough testing:

- **Release matrix**: Tests across upstream + all supported
  releases (when controller build is triggered)
- **Controller build check**: Detects if the controller
  image needs rebuilding
- **All benchmarks**: Tests all enabled benchmarks in
  `crucible-ci.json`
- **Both endpoint types**: kube and remotehosts

This produces the largest job count — potentially 200+ jobs
per release, multiplied by the number of releases.

### Benchmark CI

Benchmark repos get simpler testing:

- **Single release**: Tests against upstream only (no release
  matrix)
- **Single benchmark**: Tests only the changed benchmark
- **Both endpoint types**: kube and remotehosts
- **All userenvs**: Tests across all supported base images

### Tool CI

Tool repos are similar to benchmark CI:

- **Single release**: upstream only
- **Test workload**: Uses fio as the benchmark to exercise
  the tool
- **Both endpoint types**: kube and remotehosts

## Docs-only filtering

PRs that only change documentation files skip the full
integration test suite. This saves significant CI runner
time and cost.

### How it works

Each repo's trigger workflow uses `tj-actions/changed-files`
to check if all modified files match the docs-only list:

- `LICENSE`, `*.md`, `**/*.md`
- `.github/rulesets/**`
- Most workflow files (CI, release, tracking, fork-check)
- `.gitignore`
- `.claude/**`
- `docs/**`, `spec/**`

### Faux workflows

When only docs changed, a **faux workflow** runs instead of
the real CI. Faux workflows simply echo a success message
without running any tests. This satisfies branch protection
rules (which require CI checks to pass) without wasting
runner resources.

When non-docs files changed (or on manual `workflow_dispatch`
triggers), the **real workflow** runs with full integration
testing.

## Capability-based runner matching

The CI system matches test scenarios to runners based on
capabilities rather than hardcoded runner types.

### Configuration

The `crucible-ci.json` file (in rickshaw's `util/` directory,
validated against `rickshaw/schema/crucible-ci.json`) defines:

- **Capabilities**: A dictionary of valid capability names
  with descriptions (e.g., `k8s-cluster`,
  `remotehosts-access`, `cpu-partitioning`)
- **Endpoints**: Baseline requirements for each endpoint type
  (e.g., kube needs `k8s-cluster`)
- **Runner pools**: What each pool provides and its GitHub
  Actions labels (e.g., `aws-cloud-1` provides
  `k8s-cluster`, `remotehosts-access`, `cpu-partitioning`)
- **Benchmarks**: Scenarios with per-endpoint requirements
  (e.g., cyclictest on remotehosts needs
  `cpu-partitioning`)

### How matching works

For each scenario, the effective requirements are computed:

```
effective = scenario requirements ∪ endpoint requirements
```

A runner pool matches if it provides all effective
requirements. The pool's GitHub Actions labels become the
`runs-on` value for the job.

### Runner types

CI uses both GitHub-provided runners and self-hosted runners:

- **GitHub-provided runners** (`ubuntu-latest`): Used for
  lightweight jobs that don't need special infrastructure —
  parameter generation, docs-only checks, display jobs,
  controller build checks, and other orchestration tasks.
- **Self-hosted runners** (e.g., `aws-cloud-1`): Used for
  integration test jobs that require specific capabilities
  — installed dependencies, system configurations like CPU
  partitioning, Kubernetes cluster access, and SSH access
  to remotehosts test systems.

The capability matching system determines which integration
test jobs land on self-hosted runners and with what labels.

## Job generation and multiplication

### The job matrix

The `generate-ci-jobs.py` script processes `crucible-ci.json`
to generate a job matrix. The matrix is the cartesian product
of:

- **Benchmarks**: Which benchmarks to test (filtered by
  `--benchmark` parameter)
- **Endpoints**: Which endpoints to test (kube, remotehosts)
- **Userenvs**: Which base images to test against

### Userenv filtering

The `--userenv-filter` parameter controls how many userenvs
are included:

- **`all`**: Default, default + all unique userenvs +
  external. Produces the largest matrix.
- **`unique`**: Used for post-merge CI. Tests all unique
  userenvs but no external.
- **`minimal`**: Only the default userenv. Smallest matrix.

### Smart rickshaw PR detection

When a rickshaw PR only modifies userenv files (and no other
code), the CI system detects this and only tests the modified
userenvs instead of all of them. This dramatically reduces
the job count for userenv-only changes.

## Release matrix testing

Core repo PRs can trigger testing across multiple crucible
releases.

### Why controller changes trigger multi-release testing

The controller image is **shared across all releases**. There
is one controller image that all crucible installations use,
regardless of which release they're running. This means a
change to the controller image could break any release, not
just upstream. Therefore, when the controller needs
rebuilding, CI tests the new image against every supported
release to ensure backward compatibility.

### When multi-release testing happens

The `get-releases` action determines which releases to test:

- **No controller rebuild needed**: Tests only `upstream`
  (~200 jobs)
- **Controller rebuild needed**: Tests `upstream` + all
  supported releases (~200 × N jobs, where N is the number
  of supported releases)
- **Installer changed**: Also triggers multi-release testing

### Controller build check

The `check-controller-build` action examines the PR's changed
files to determine if the controller image needs rebuilding.
Rebuilds are triggered when:

- Files in `crucible/workshop/` changed (base image specs)
- `workshop/workshop.py` changed (builder logic)
- `workshop/schema.json` changed (validation rules)

When a rebuild is needed, the job count can grow
significantly because each release gets its own complete
test matrix.

## Integration tests

The `integration-tests` action is where benchmarks actually
run. For each job in the matrix:

### Setup

1. Clean the runner environment (remove leftover containers,
   networks, images from previous jobs)
2. Install crucible (with the PR's changes for the target
   repo)
3. Configure the test endpoint (kube or remotehosts)
4. Import registry authentication secrets

### Execution

1. Run the benchmark with the specified userenv and endpoint
2. Collect results and artifacts
3. Upload artifacts to GitHub Actions (5-day retention)

### Pass/fail

A job passes if the benchmark completes without errors.
Failures can come from:

- Benchmark execution errors
- Container image build failures
- Endpoint connectivity issues
- Timeout exceeded

## Post-merge CI

The `crucible-merged.yaml` workflow runs after a PR is
merged, providing production-level validation.

### What triggers it

Post-merge CI fires on `pull_request_target` (closed +
merged) when specific paths change:

- Workshop files (`workshop*.json`, `client-workshop*.json`,
  `server-workshop*.json`)
- Source scripts (benchmark scripts, tool scripts)
- Userenv definitions

Documentation-only changes do not trigger post-merge CI.

### How it differs from PR CI

- Uses **production registry** credentials instead of CI
  registry
- Tests the **merged code** on the default branch
- Uses **`userenv_filter: "unique"`** to test all unique
  userenvs
- Provides confidence that the merged change works in the
  production context

### Pre-seeding the image cache

An important side effect of post-merge CI is that it
**pre-seeds the image cache** in the production registry.
When a merged change affects workshop requirements (new
packages, updated source builds, etc.), the post-merge CI
run builds the new engine images and pushes them to the
production registry. This means the next user who runs that
benchmark will find the images already cached in the
registry rather than having to build them from scratch.

Without post-merge CI, the first user to run after a
workshop change would pay the full image build cost. With
it, the images are ready and waiting.

## Fork protection

Every repo has a `fork-check.yaml` workflow that
automatically closes PRs opened from forks. Fork PRs cannot
access the organization's secrets and variables needed for
CI workflows (registry tokens, runner access, etc.), so they
would fail with misleading errors.

The workflow comments on the PR explaining that PRs must be
opened from branches on the upstream repository, then closes
it.

## Controller image builds

The controller image contains the orchestration software
(rickshaw, toolbox, roadblock, CDM, etc.) that runs inside
the controller container. When a PR changes files that affect
this image, CI rebuilds it.

### Build trigger

The `check-controller-build` action checks if the PR touches
files in:

- `crucible/workshop/` (image specifications)
- `workshop/workshop.py` (the build script itself)
- `workshop/schema.json` (validation schema)

If any of these changed, `build_controller` is set to `yes`
and the controller image is rebuilt as part of CI.

### Impact on test scope

A controller rebuild triggers multi-release testing because
the image affects all releases. This means a workshop change
to a core repo can produce 400+ CI jobs (200 per release ×
2+ releases), compared to ~200 jobs for a non-workshop
change.

The rebuilt image is tagged with a CI-specific tag
(`{repo}_{target}_{run_number}`) and used for all test jobs
in that CI run. It's not pushed to the production registry
— that happens separately through the controller build
workflow after merge.
