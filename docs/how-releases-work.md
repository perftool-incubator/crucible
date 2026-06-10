# How Releases Work

This document explains how crucible's release system operates —
the quarterly release cycle, how releases are created and
installed, how repos are pinned to specific versions, and how
to move between releases.

For how repos and subprojects are managed, see
[how-the-repo-system-works.md](how-the-repo-system-works.md).
For how CI tests releases, see
[how-ci-works.md](how-ci-works.md).

## Overview

Crucible uses a quarterly release system to provide stable,
reproducible installations. A release is a coordinated
snapshot of all repositories in the ecosystem — every repo
gets a branch with the same release name at the same point in
time. This ensures that all components are compatible and
have been tested together.

Releases enable:

- **Reproducible testing**: Running the same crucible version
  across multiple systems for consistent regression results
- **Stability**: Pinning to a known-good version while
  upstream development continues
- **Rollback**: Returning to a previous version if an upgrade
  introduces issues

## Release naming

Releases follow a `YYYY.Q` naming convention:

- `YYYY` — four-digit year
- `Q` — quarter number (1 = Jan–Mar, 2 = Apr–Jun,
  3 = Jul–Sep, 4 = Oct–Dec)

Examples: `2025.4`, `2026.1`, `2026.2`

The four most recent releases are considered **supported**.
Older releases still exist as branches but are considered
unsupported.

## How releases are created

Release creation is automated via a GitHub Actions scheduled
workflow that runs on the first day of each quarter (January
1, April 1, July 1, October 1). The workflow:

1. Calculates the release name from the current date
   (`YYYY.Q`)
2. Creates a release branch in the crucible repo
3. Reads `config/repos.json` to get the list of all
   subproject repos
4. Creates a release branch with the same name in every
   subproject repo

This produces a synchronized snapshot — all repos have a
branch named (for example) `2026.2` pointing to their
current main/master HEAD at the moment the release was
created.

Releases can also be created manually via `workflow_dispatch`
for testing or ad-hoc releases.

## Follow vs locked mode

The `repos.json` checkout configuration has two modes that
control how repos behave:

### Follow mode (default, upstream development)

```json
{
    "checkout": {
        "mode": "follow",
        "target": "master"
    }
}
```

In follow mode, `crucible update` pulls the latest changes
from the target branch. The repo stays current with upstream
development. This is the default for new installations and
development environments.

### Locked mode (release installations)

```json
{
    "checkout": {
        "mode": "locked",
        "target": "2026.1"
    }
}
```

In locked mode, `crucible update` does not pull new changes.
The repo stays pinned to the exact commit on the release
branch. This provides the stability and reproducibility that
release installations require.

## Installing a release

### New installation

To install a specific release from scratch:

```bash
./crucible-install.sh --release 2026.1 [other options]
```

The installer clones all repos at the specified release
branch and sets their checkout mode to locked.

### Interactive selection

To choose from available releases interactively:

```bash
./crucible-install.sh --release select [other options]
```

This lists the supported releases and prompts for a choice.

## Switching an existing installation

The `--set-release` flag switches an existing installation
between releases or between release and upstream modes:

```bash
./crucible-install.sh --set-release 2026.1
```

### What set-release does

1. **Validates the release** — checks that the requested
   release branch exists and is within the supported window
   (the 4 most recent releases)
2. **Stops all services** — shuts down any running crucible
   containers to avoid conflicts
3. **Updates repos.json** — sets all official repos to
   `mode: "locked"` with `target: "<release>"`
4. **Checks out crucible** — switches the crucible repo to
   the release branch, preserving any local changes via stash
5. **Updates subprojects** — runs `subprojects-install` which
   clones or checks out each subproject at the release branch

After set-release completes, the installation is fully
pinned to the specified release version.

## Updates in release mode

When a crucible installation is in release mode (locked),
`crucible update` behaves differently:

- Repos in **follow mode** pull the latest changes from
  their target branch
- Repos in **locked mode** fetch changes from the remote
  (so the local clone knows about any upstream commits on
  the release branch) but do not check them out or merge
  them. The working tree stays at the original commit.

This means `crucible update` in a release installation
keeps the local clone aware of remote state without
changing the checked-out code. This is useful because
release branches may receive critical fixes after the
initial release — the fetch makes those commits available
locally if you later decide to pull them, but they don't
apply automatically. It still updates any unofficial repos
that may be in follow mode.

## Release CI

Releases are tested automatically in two ways:

### PR-level testing

The `crucible-release-ci.yaml` workflow runs on every pull
request to crucible's master branch. It exercises the release
lifecycle by:

1. Deleting a test release branch (`ci-version-test`)
2. Creating the test release branch
3. Updating the test release branch

This validates that the release creation and management
workflows function correctly with the current code.

### Release verification

When a release branch is created or updated, the release
workflow includes a verification job that:

1. Performs a clean install using the new release
2. Validates that the installation is functional

This catches installation issues before the release is
available to users.

## Unsupported releases

Releases older than the four most recent are considered
unsupported. They remain available as branches but
`--set-release` will refuse to switch to them by default.

To use an unsupported release, add the force flag:

```bash
./crucible-install.sh --set-release 2024.3 --set-release-force
```

This acknowledges that the release is outside the supported
window and may not work with current infrastructure (CI
runners, container registries, etc.).

## Moving between releases

To upgrade or downgrade between releases, use `--set-release`
with the target version:

```bash
# Upgrade from 2025.4 to 2026.1
./crucible-install.sh --set-release 2026.1

# Downgrade back to 2025.4
./crucible-install.sh --set-release 2025.4
```

The same mechanism works in both directions. Set-release
stops services, updates `repos.json` to point all repos at
the new release branch, checks out that branch in every repo,
and runs `subprojects-install` to ensure everything is
consistent.

When moving between releases, any local modifications to
the crucible repo are preserved via git stash. However,
local changes to subproject repos may conflict with the
target release and should be committed or stashed manually
before switching.

## Returning to upstream

To switch from a release back to upstream development mode:

```bash
./crucible-install.sh --set-release upstream
```

This changes all repos from `mode: "locked"` back to
`mode: "follow"` and sets their target back to their
primary branch (master or main). Running `crucible update`
afterward pulls the latest upstream changes.
