# How the Repo System Works

This document explains how crucible manages its multi-repo
ecosystem — the configuration, cloning, activation, and
updating of the 40+ repositories that make up the framework.

For how releases interact with the repo system, see
[how-releases-work.md](how-releases-work.md).

## Overview

Crucible is a multi-repo framework. Rather than a single
monolithic repository, it's composed of many independently
versioned repos: the core orchestration components, each
benchmark, each tool, and supporting projects.

The repo system has three layers:

- **`config/repos.json`** — defines the ecosystem: which repos
  exist, where to clone them from, and what version to use
- **`repos/`** — stores the actual git clones, organized by
  remote URL
- **`subprojects/`** — activates specific clones via symlinks,
  organized by category

This separation allows multiple forks of the same repo to
coexist, switching between sources without re-cloning, and
mixing upstream and local development versions.

## repos.json

The `config/repos.json` file is the central configuration
that defines the entire crucible ecosystem.

### Structure

```json
{
    "official": [
        {
            "name": "rickshaw",
            "type": "core",
            "repository": "https://github.com/perftool-incubator/rickshaw",
            "primary-branch": "master",
            "checkout": {
                "mode": "follow",
                "target": "master"
            }
        }
    ],
    "unofficial": []
}
```

### Official vs unofficial

- **Official repos**: The standard crucible ecosystem —
  maintained by the project and included in releases. Users
  should not add repos to this array directly.
- **Unofficial repos**: User-supplied repos not part of the
  official upstream. Added via `crucible repo config add` and
  preserved across updates and release switches.

### Repo types

| Type | Description | Directory |
|------|-------------|-----------|
| `primary` | Crucible itself (only one) | — |
| `core` | Orchestration components (rickshaw, toolbox, roadblock, etc.) | `subprojects/core/` |
| `benchmark` | Workload generators (fio, uperf, cyclictest, etc.) | `subprojects/benchmarks/` |
| `tool` | Data collectors (sysstat, procstat, ftrace, etc.) | `subprojects/tools/` |
| `doc` | Documentation and examples | `subprojects/docs/` |

### Fields

- **name**: Unique identifier used in commands and symlinks
  (e.g., `rickshaw`, `fio`, `sysstat`)
- **type**: Category that determines the subprojects directory
- **repository**: Full git clone URL
- **primary-branch**: The repo's default branch (`master` or
  `main`)
- **checkout**: Controls version pinning:
  - `mode: "follow"` — track upstream, pull changes on update
  - `mode: "locked"` — pin to a specific version, don't update
  - `target` — the branch, tag, or commit to check out

### URL conventions

The committed upstream `repos.json` uses HTTPS URLs, which
work without SSH keys for end-user installations. Development
environments typically override these with SSH URLs for easier
push access. Both URL formats are fully supported — the repo
system handles URL normalization transparently.

## The repos/ directory

The `repos/` directory stores all git clones, organized by
remote URL prefix:

```
repos/
├── git@github.com:perftool-incubator/
│   ├── crucible.git/
│   ├── rickshaw.git/
│   ├── bench-fio.git/
│   ├── tool-sysstat.git/
│   └── ...
└── https:github.com:perftool-incubator/
    ├── crucible/
    ├── rickshaw/
    └── ...
```

### URL-based organization

Clones are keyed by their full remote URL, not by repo name.
The URL is normalized into a directory path: protocol markers
are stripped, slashes become colons, and the result forms the
directory name.

### Multiple forks

Because clones are keyed by URL, the same logical repo can
exist from multiple sources simultaneously:

```
repos/git@github.com:perftool-incubator/rickshaw.git    (upstream)
repos/git@github.com:myuser/rickshaw.git                (personal fork)
```

Only one clone is active at a time (via the symlink in
`subprojects/`), but both exist on disk. Switching between
them is a symlink change, not a re-clone.

## The subprojects/ directory

The `subprojects/` directory is where repos become **active**.
Each active repo has a symlink from its category directory
pointing to the actual clone in `repos/`:

```
subprojects/
├── core/
│   ├── rickshaw -> ../../repos/git@github.com:perftool-incubator/rickshaw.git
│   ├── toolbox -> ../../repos/git@github.com:perftool-incubator/toolbox.git
│   └── ...
├── benchmarks/
│   ├── fio -> ../../repos/git@github.com:perftool-incubator/bench-fio.git
│   ├── uperf -> ../../repos/git@github.com:perftool-incubator/bench-uperf.git
│   └── ...
├── tools/
│   ├── sysstat -> ../../repos/git@github.com:perftool-incubator/tool-sysstat.git
│   └── ...
└── docs/
    ├── examples -> ../../repos/git@github.com:perftool-incubator/crucible-examples.git
    └── ...
```

### Relative symlinks

All symlinks use relative paths (`../../repos/...`) so the
entire tree is portable — it works regardless of where
crucible is installed.

### Type to directory mapping

Repo types map to directory names with a pluralization
convention:

- `benchmark` → `benchmarks/`
- `tool` → `tools/`
- `doc` → `docs/`
- `core` → `core/` (unchanged)

### Switching sources

To switch a subproject to a different source (e.g., from
upstream to a personal fork), update the repository URL:

```bash
crucible repo config update \
    name=rickshaw \
    repository=git@github.com:myuser/rickshaw.git
```

Then run `crucible update rickshaw` to clone from the new
source and update the symlink. The old clone remains in
`repos/` under its original URL path and can be reactivated
by changing the URL back.

## Installing subprojects

The `subprojects-install` script processes `repos.json` and
ensures all repos are cloned and symlinked:

1. **Parse repos.json** — reads the official and unofficial
   arrays
2. **Clean stale symlinks** — scans `subprojects/` for
   symlinks that no longer correspond to a repo in the
   config and removes them. This handles repos that have
   been removed from `repos.json` or changed type. Only the
   symlink is removed — the git clone in `repos/` is
   preserved in case the repo is reactivated later.
3. **For each repo**:
   - Parse the repository URL to determine the clone path
   - Clone if not already present
   - Check out the specified branch/tag/commit
   - Create or update the symlink in `subprojects/`

This script runs during initial installation, during
`crucible update`, and when switching releases.

## Updating repos

### crucible update

The `crucible update` command updates all repos to their
latest versions:

```bash
crucible update              # update everything
crucible update crucible     # update just crucible
crucible update rickshaw     # update a specific subproject
```

### The two-pass self-update

Crucible has a unique challenge: the update script itself
lives in the crucible repo. If the script changes during a
`git pull`, bash may not handle the mid-execution file change
correctly.

To solve this, `crucible update` uses a two-pass mechanism:

1. **Pass 1**: Creates a temporary copy of the update script,
   appends a command to re-execute the real update script, and
   `exec`s the copy. This ensures bash runs a stable copy
   while the real script gets updated by git.
2. **Pass 2**: The freshly-updated real script runs and
   handles all subproject updates.

### Per-repo update behavior

For each repo, the `_update-git` script:

1. Fetches all changes from the remote
2. Stashes any local modifications
3. Checks out the target branch
4. In **follow mode**: merges upstream changes via
   `git pull --ff-only`
5. In **locked mode**: stays at the current commit (fetch
   only, no checkout of new changes)
6. Reapplies stashed modifications

### New subproject discovery

After updating all existing repos, `crucible update` runs
`subprojects-install` to clone and activate any new repos
that were added to `repos.json` since the last update.

## Managing repos

The `crucible repo` command provides repo management:

### Viewing status

```bash
crucible repo info            # one-line status per repo
crucible repo details         # full git status and diffs
crucible repo config show     # tabular config dump
```

### Adding repos

```bash
crucible repo config add \
    name=myrepo \
    type=tool \
    repository=https://github.com/myuser/tool-myrepo \
    primary-branch=main \
    checkout-mode=follow \
    checkout-target=main
```

This adds the repo to the `unofficial` array in repos.json.
Run `crucible update myrepo` afterward to clone and activate
it.

### Modifying repos

```bash
crucible repo config update \
    name=myrepo \
    checkout-mode=locked \
    checkout-target=v1.0
```

All configuration changes are validated against the
`schema/repos.json` JSON schema before being saved. The
schema enforces valid repo types, checkout modes, branch
names, and URL formats, preventing misconfiguration that
could break the update or install process.
