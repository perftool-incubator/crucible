# Developer Guide

This guide covers the crucible development workflow — setting
up a development environment, working on subprojects, testing
changes, coordinating cross-repo work, and submitting PRs.

For the architecture and how each subsystem operates, see the
[architecture overview](crucible-architecture-overview.md) and
the subsystem guides it references.

## Development environment setup

### Prerequisites

The host needs only three tools:

- **podman** — container runtime
- **git** — for cloning and updating repositories
- **jq** — for JSON manipulation

All other dependencies (Python packages, Perl modules, build
tools) live inside the controller container image.

### Installing for development

Install crucible in upstream/follow mode (the default):

```bash
./crucible-install.sh \
    --engine-registry <engine-registry-url> \
    --name "Your Name" \
    --email "you@example.com"
```

This installs all repos in `follow` mode, tracking their
default branches. Running `crucible update` pulls the latest
changes.

### The controller container

Most crucible software runs inside the controller container.
Two commands give you access:

- **`crucible wrapper <command>`** — runs a single command
  inside the controller and returns. Use this for scripts that
  need controller dependencies (Python packages, Perl modules,
  workshop scripts).
- **`crucible console`** — opens an interactive shell inside
  the controller. Useful for debugging, running multiple
  commands, or exploring installed software. Man pages are
  available for all installed tools.

### Developer tooling

Crucible includes a Claude Code plugin for development
workflows. Register it after installation:

```bash
claude plugin marketplace add \
    ${CRUCIBLE_HOME}/subprojects/core/crucible-dev-tools
```

Available skills:

- `/crucible-tools:activity-summary` — generate an activity summary for the GitHub organization
- `/crucible-tools:debug-log` — analyze crucible logs to debug failed runs or commands
- `/crucible-tools:dev-activity` — generate development activity charts (commits, PRs, workflow runs)
- `/crucible-tools:image-cleanup` — clean up local podman images (engine images, dangling images, local builds)
- `/crucible-tools:new-repo` — create a new repository in the GitHub organization with standard config
- `/crucible-tools:open-prs` — show all open PRs in the org (optionally filter by author)
- `/crucible-tools:repo-status` — git status across all crucible repos
- `/crucible-tools:workflow-status` — show active CI workflow runs across crucible repos
- `ci-analyzer` agent — analyze GitHub Actions CI workflow runs to diagnose failures

## Repository structure

### The clone/symlink model

Crucible uses a two-directory model:

- **`repos/`** — git clones, organized by remote URL. The same
  repo can exist from multiple sources (upstream, fork A, fork
  B).
- **`subprojects/`** — symlinks to the active clone of each
  repo, organized by category:
  - `subprojects/core/` — rickshaw, toolbox, roadblock,
    workshop, CommonDataModel, multiplex, packrat, crucible-ci
  - `subprojects/benchmarks/` — fio, uperf, iperf, etc.
  - `subprojects/tools/` — sysstat, procstat, ftrace, etc.
  - `subprojects/docs/` — examples, testing

For example:

```
subprojects/core/rickshaw ->
    ../../repos/git@github.com:perftool-incubator/rickshaw.git
```

### repos.json

`config/repos.json` defines all subproject repositories with
two categories:

- **official** — maintained by the crucible project, included
  in releases
- **unofficial** — user-added repos extending crucible with
  custom benchmarks, tools, or other subprojects

Each entry specifies:

```json
{
    "name": "rickshaw",
    "type": "core",
    "repository": "https://github.com/perftool-incubator/rickshaw.git",
    "primary-branch": "main",
    "checkout": {
        "mode": "follow",
        "target": "main"
    }
}
```

The committed `repos.json` uses HTTPS URLs (for end users).
Developers typically override locally with SSH URLs for push
access — these local changes should never be committed.

### subprojects-install

Running `subprojects-install` (called during install and
update) clones any repos that aren't present, checks out the
configured branch/tag/commit, and creates or updates symlinks
in `subprojects/`. It also cleans up symlinks for repos that
have been removed from `repos.json`.

### Working directory

Run Claude Code from `/opt/crucible` for most work — all
subproject code is accessible through symlinks, and cross-repo
changes have full visibility. Only work from a subproject root
(e.g., `subprojects/core/rickshaw/`) for deep isolated work on
that specific component.

## Working on subprojects

Each subproject is its own git repo with independent history,
branches, and commits. Running `git status` or `git diff` from
`/opt/crucible` only shows changes to the crucible repo itself.

To work on a subproject:

```bash
cd subprojects/core/rickshaw
git checkout -b my-feature-branch
# edit, test, commit
```

Check status across all repos:

```bash
crucible repo info      # one-line status per repo
crucible repo details   # full git status and diffs
```

New repos use `main` as their primary branch. Some older repos
still use `master` — this is a legacy configuration. Check the
repo's `repos.json` entry if unsure.

## Switching sources and forks

To work from a different fork or source:

```bash
crucible repo config update \
    name=rickshaw \
    repository=git@github.com:myuser/rickshaw.git
crucible update rickshaw
```

This clones the new source into `repos/` and repoints the
symlink. Multiple clones of the same repo from different
forks can coexist — only one is active at a time.

To switch back to upstream:

```bash
crucible repo config update \
    name=rickshaw \
    repository=https://github.com/perftool-incubator/rickshaw.git
crucible update rickshaw
```

## Testing changes

### Unit tests

Individual repos have their own test suites:

- **Python repos** (multiplex, source-images-service): `pytest`
- **Bash repos** (roadblock): `test/run-test.sh`
- **Crucible installer**: `./tests/test-installer`

### Container-side testing

Scripts that run inside the controller container (workshop
scripts, Python packages only available in the container) must
be tested with:

```bash
crucible wrapper <command>
```

Running them directly on the host will fail due to missing
dependencies.

### Service testing

When modifying the source-images-service, stop all services
first so the service picks up your code changes:

```bash
crucible stop valkey opensearch image-sourcing httpd
```

Then start them again or run a benchmark to exercise the
changes.

### Integration testing

The most thorough test is running a benchmark:

```bash
crucible run my-run-file.json
```

This exercises the full pipeline: image sourcing, engine
deployment, benchmark execution, tool collection,
post-processing, and result indexing.

### CI testing

Run the full CI suite locally:

```bash
crucible run-ci
```

This executes the same test matrix that runs on PRs, including
benchmark scenarios across endpoints and userenvs. It requires
a runner environment with the necessary infrastructure.

## Cross-repo changes

Changes that span multiple repos require coordination:

1. **Identify affected repos** — consult the docs index and
   key integration points:
   - `toolbox` changes ripple across all benchmarks, tools, and
     core components
   - `rickshaw.json` changes in benchmarks/tools affect how
     they integrate with the orchestrator
   - `workshop.json` changes affect container image builds
   - Schema changes in `rickshaw/schema/` affect run file
     validation

2. **Create feature branches** in each affected repo

3. **Test locally** — run a benchmark that exercises the
   changed code paths

4. **Submit separate PRs** for each repo — CI tests each PR
   independently against upstream

5. **Merge in dependency order** — shared libraries first
   (toolbox), then consuming repos. If a benchmark PR depends
   on a rickshaw change, merge the rickshaw PR first.

6. **Controller image rebuilds** — when a contributing repo
   (crucible, rickshaw, workshop, toolbox, roadblock,
   CommonDataModel, multiplex) merges changes that affect the
   controller image, a rebuild is triggered automatically.

## Pull request workflow

- Always create a feature branch — never push directly to
  the default branch
- Submit PRs from branches on the **upstream repository**, not
  from forks. Fork PRs are automatically closed because CI
  workflows need access to org secrets and variables.
- Request review from the **Developers** team and self-assign
- Use conventional commit messages: `feat:`, `fix:`, `docs:`,
  `refactor:`, etc.
- One commit per independent change — only group changes
  together when they depend on each other
- CI runs automatically on every PR: installer tests and
  integration tests
- Docs-only changes trigger faux CI (lightweight pass without
  running benchmarks)
- Branch rulesets require 1 approving review and all review
  threads resolved before merge

## Code conventions

### Languages

- **New code**: Python 3 by default
- **Host-side scripts** (in `bin/`): Bash, to minimize host OS
  dependencies
- **Extending existing files**: use that file's language

### Style

- **Bash**: 4-space indentation, tabs expanded to spaces.
  Include vim and emacs modelines:
  ```
  # -*- mode: sh; indent-tabs-mode: nil; sh-basic-offset: 4 -*-
  # vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=bash
  ```
- **Python**: PEP 8, 4-space indentation
- **JSON keys**: `kebab-case`
- **CLI arguments**: `--kebab-case`

### Naming

- Lowercase with underscores for local variables
- Uppercase for exported/environment variables (`CRUCIBLE_HOME`,
  `TOOLBOX_HOME`)

### CLI changes

When adding a new CLI parameter, option, or subcommand, update
all three locations:

1. `bin/_help` — main help output
2. `bin/_crucible_completions` — bash tab completions
3. The relevant command's own help text

### Documentation

- When making structural changes to a subproject, update that
  subproject's CLAUDE.md in the same PR
- When changes affect user-facing behavior, configuration,
  architecture, or workflows, check whether the `docs/` guides
  need corresponding updates — treat stale docs as a bug

### Comments

Write comments that explain the intent behind the code —
why a decision was made, what constraint is being satisfied,
or what behavior would surprise a future reader. Focus on
the WHY rather than restating WHAT the code does.

## Releases

Crucible produces quarterly releases named `YYYY.Q` (e.g.,
`2026.2` for Q2 2026). Release branches are created
automatically on the first day of each quarter across all
repos.

### For developers

- Most development happens on upstream (follow mode) —
  releases are automatic snapshots
- The 4 most recent releases are supported
- `crucible update` in follow mode pulls the latest upstream
  changes
- Use `--set-release <version>` to test against a specific
  release, `--set-release upstream` to return to development

### Controller image

The controller image is shared across all releases. Changes to
any of the 7 contributing repos (crucible, rickshaw, workshop,
toolbox, roadblock, CommonDataModel, multiplex) trigger an
automatic rebuild. A controller update brings new capabilities
to every release.

For details on the release system, see
[how-releases-work.md](how-releases-work.md).

## Key integration points

| Component | What it affects | How |
|-----------|----------------|-----|
| toolbox (`TOOLBOX_HOME`) | Everything | Shared Python and Bash libraries |
| `rickshaw.json` | Benchmarks and tools | Declares integration with the orchestrator |
| `workshop.json` | Container images | Declares build requirements |
| JSON schemas (`rickshaw/schema/`) | Run file validation | Define valid configurations |
| Controller image | All operations | Contains all orchestration software |

Changes to these components have wide blast radius. Test
thoroughly and consider CI impact.

## Debugging

### Run logs

```bash
crucible log view last     # most recent run
crucible log view list     # browse available logs
```

### Engine logs

Engine stdout/stderr is archived in the run directory:

```
/var/lib/crucible/run/<run-id>/
```

### Interactive debugging

```bash
crucible console           # shell inside the controller
```

This gives you access to all controller software (Python
packages, Perl modules, tools) with full man page
documentation.

### Run file validation

If a run file fails validation, check it against the schema:

```bash
crucible wrapper python3 -c "
from toolbox.json import load_json_file, validate_schema
data, _ = load_json_file('my-run-file.json')
valid, err = validate_schema(data, '/opt/crucible/subprojects/core/rickshaw/schema/run-file.json')
print('Valid' if valid else f'Error: {err}')
"
```
