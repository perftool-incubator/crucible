# Crucible - Container-Based Performance Testing Framework

## Project Overview

Crucible is a container-based performance testing framework built primarily in Bash. It orchestrates benchmark execution, data collection, post-processing, and result indexing using Podman containers. The project is part of the `perftool-incubator` GitHub organization.

## Architecture

**Execution flow:** `bin/crucible` (CLI entry point) -> sources `bin/base` (shared functions/config) -> `bin/_main` (command router) -> subcommands

**Key design patterns:**
- All Bash scripts source `bin/base` for shared functions, variables, and configuration
- Commands run inside Podman containers using a controller image (`quay.io/crucible/controller`)
- JSON configuration files are validated against JSON schemas before use
- Subprojects are independently versioned git repos; clones live in `repos/`, activated via symlinks in `subprojects/`

## Key Directories

| Path | Purpose |
|------|---------|
| `bin/` | CLI entry point (`crucible`), command router (`_main`), shared library (`base`), and helper scripts |
| `config/` | JSON configuration: `repos.json`, `services.json`, `registries.json`, `instances.json` |
| `schema/` | JSON schemas for validating config files |
| `spec/` | Markdown specifications for config file formats and release tags |
| `subprojects/` | Symlinks to active repo versions, organized by category (see below) |
| `repos/` | Git clones organized by remote URL (may contain multiple forks of the same repo) |
| `workshop/` | Container image build scripts for the controller |
| `tests/` | Test infrastructure (`test-installer`) |

## Subproject System

Defined in `config/repos.json` with categories:
- **core**: `rickshaw` (run orchestration), `multiplex`, `roadblock`, `workshop`, `CommonDataModel`, `toolbox`, `packrat`, `crucible-ci`
- **benchmarks**: `cyclictest`, `fio`, `flexran`, `hwlatdetect`, `hwnoise`, `ilab`, `iperf`, `oslat`, `osnoise`, `pytorch`, `sleep`, `timerlat`, `tracer`, `trafficgen`, `uperf` (in `subprojects/benchmarks/`)
- **tools**: `sysstat`, `procstat`, `ftrace`, `kernel`, `ovs`, `nvidia`, `power`, `forkstat`, `rt-trace-bpf` (in `subprojects/tools/`)
- **docs**: `examples`, `testing` (in `subprojects/docs/`)

Each entry in `repos.json` has: `name`, `type`, `repository`, `primary-branch`, and `checkout` config.

## Common Commands

```bash
# Install crucible
./crucible-install.sh

# Run a benchmark (requires JSON run-file)
crucible run <run-file.json>

# Update all subprojects and controller image
crucible update [all|crucible|controller-image|<subproject>]

# Manage repos
crucible repo [info|details|config]

# View/manage results
crucible ls
crucible get result [--run <id>]
crucible get metric --run <id> --source <name> --type <metric>
crucible index <dir>
crucible rm --run <id>

# Service management
crucible start <service>   # httpd, opensearch, valkey, image-sourcing
crucible stop <service>

# Run CI tests
crucible run-ci

# Full help
crucible help [command]
```

## Code Conventions

- **Bash style**: 4-space indentation, tabs expanded to spaces
- **Modelines**: All Bash files include both vim and emacs modelines:
  ```
  # -*- mode: sh; indent-tabs-mode: nil; sh-basic-offset: 4 -*-
  # vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=bash
  ```
- **Sourcing pattern**: Scripts source `/etc/sysconfig/crucible` for `$CRUCIBLE_HOME`, then `$CRUCIBLE_HOME/bin/base` for shared functions
- **Error handling**: Use `exit_error "message"` from `bin/base`; exit codes defined as `EC_*` constants
- **Podman usage**: Use `podman_wrapper()` and related functions from `bin/base`; never call podman directly
- **JSON processing**: Use `jq_query()` helper from `bin/base`; validate configs against schemas in `schema/`
- **Variable naming**: Lowercase with underscores for locals; uppercase for exported/environment variables (e.g., `CRUCIBLE_HOME`, `CRUCIBLE_CONTROLLER_IMAGE`)

## Language Strategy
- **New code**: Write new functionality in Python 3 by default
- **Host-side exception**: Code in the crucible repo that runs outside the controller container (e.g., `bin/` scripts) should be Bash to minimize host OS dependencies
- **Existing Perl**: Core subprojects like rickshaw and workshop are written in Perl. These are being incrementally ported to Python as time and need allow — do not rewrite Perl to Python unless specifically asked
- **Existing languages**: When extending an existing file, use that file's language. When adding new functionality to a subproject, prefer Python even if the subproject has Perl code

## Configuration

- `CRUCIBLE_HOME`: Set in `/etc/sysconfig/crucible`, typically `/opt/crucible`
- `~/.crucible/identity`: User identity (CRUCIBLE_NAME, CRUCIBLE_EMAIL)
- Run data stored in `/var/lib/crucible/run/`

## Dependencies

Runtime: `podman`, `git`, `jq`

## Multi-Repo Structure

### repos/ and subprojects/ — clone vs. activate
- **`repos/`** is where git repositories are cloned to, organized by remote URL prefix (e.g., `repos/git@github.com:perftool-incubator/rickshaw.git`). The same repo can exist multiple times from different sources (upstream, user A's fork, user B's fork, etc.).
- **`subprojects/`** is where the **active version** of each repo is set via symlinks. A symlink points to whichever clone in `repos/` the user wants active. For example:
  ```
  subprojects/core/rickshaw -> ../../repos/git@github.com:perftool-incubator/rickshaw.git
  ```

This means:
- Each subproject has its own git history, branches, and commits
- `git status` and `git diff` from `/opt/crucible` only show changes to the crucible repo itself
- To see/commit subproject changes, `cd` into that subproject directory
- Switching between forks/sources is done by changing which clone the symlink points to

### Cross-repo integration patterns
- **toolbox** (`subprojects/core/toolbox/`) is a shared library used by nearly everything. Code references it via the `TOOLBOX_HOME` environment variable. Changes here ripple across all benchmarks, tools, and core components.
- **rickshaw.json** files in benchmarks/tools declare how they integrate with rickshaw (the orchestrator). They specify roles, parameters, and workshop requirements.
- **workshop.json** files declare container image build requirements (packages, pip modules, CPAN modules). The target image depends on context: some go into the **controller image**, others into **engine images**. Benchmarks and tools always target engine images. Core subprojects vary — e.g., toolbox targets the controller image, but packrat and roadblock target engine images (or both, in roadblock's case).
- **JSON schemas** in `rickshaw/schema/` validate run files, benchmark params, and tool params. Schemas in `schema/` (crucible root) validate crucible's own config files.

### Recommended workflow
Run Claude Code from `/opt/crucible` for most work — all subproject code is accessible, and cross-cutting changes have full visibility. Only run from a subproject root (e.g., `subprojects/core/rickshaw/`) for deep isolated work on that specific component.

## Developer Tools

Crucible includes a Claude Code plugin (`crucible-dev-tools`) for development workflows. When opening this project, Claude Code will prompt you to install the crucible-tools plugin. Accept to get access to:

- `/crucible-tools:repo-status` — git status across all crucible repos
- `/crucible-tools:open-prs` — open PRs in the org (optionally filter by author)
- `/crucible-tools:dev-activity` — development activity charts (commits, PRs, workflow runs)
- `/crucible-tools:weekly-summary` — weekly activity report with PR links
- `ci-analyzer` agent — analyze GitHub Actions CI workflow runs to diagnose failures

## Common Terminology

| Abbreviation | Full Name |
|---|---|
| CDM | CommonDataModel — the data model and query engine subproject |
| SIS | source-images-service — the engine image build service in rickshaw |

## Testing and Validation

- **Container-side scripts**: Scripts that run inside the controller container (e.g., `workshop/controller-image.py`, workshop scripts) must be tested using `crucible wrapper <command>`. Running them directly on the host will fail due to missing dependencies (Python packages like `invoke`, Perl modules, etc.) that are only installed inside the container image.
- **Service restart**: When modifying source-images-service code, stop all services with `crucible stop valkey opensearch image-sourcing httpd` before testing so the service picks up the new code.
- **Engine image builds**: Test with `crucible run <run-file.json>` — run from the directory containing any referenced files (e.g., job files).

## Pull Requests and Contributions

- **Branch strategy**: Always submit PRs from branches on the upstream repository, not from forks. Fork PRs cannot access org secrets and variables needed for CI workflows. All repos have a `fork-check` workflow that automatically closes fork PRs.
- **Review requests**: When opening PRs, request review from the **Developers** team and self-assign the PR.
- **Commit messages**: Use conventional commits format (`feat:`, `fix:`, `docs:`, etc.). Be precise and descriptive — prefer nuanced descriptions over broad generalizations.
- **CLAUDE.md updates**: When making structural changes to a subproject, update that subproject's CLAUDE.md in the same PR. Claude should author CLAUDE.md content, not humans — the human role is review and approval.
- **Branch rulesets**: `.github/rulesets/` files are backups of configured rulesets, not authoritative. Do not read or modify them to determine required status checks.
