# How the Installer Works

This document explains how `crucible-install.sh` installs
crucible — the prerequisites, installation flow, registry
configuration, and post-install steps.

For how releases interact with the installer, see
[how-releases-work.md](how-releases-work.md).
For how the repo system is set up during install, see
[how-the-repo-system-works.md](how-the-repo-system-works.md).

## Overview

The crucible installer (`crucible-install.sh`) handles fresh
installations, release selection, and switching between
releases. It clones the crucible repository, installs all
subprojects, pulls the controller container image, and
configures the registry settings.

The host system has minimal requirements — only podman, git,
and jq are needed. All other software dependencies are
satisfied by the controller container image.

## Prerequisites

The installer requires three tools on the host:

- **podman** — container runtime for running all crucible
  operations
- **git** — for cloning and updating repositories
- **jq** — for manipulating JSON configuration files

The installer checks for these at startup and attempts to
install them via `yum` if they're missing. If automatic
installation fails, the user must install them manually.

## Fresh install

A basic installation:

```bash
./crucible-install.sh \
    --engine-registry <engine-registry-url> \
    --name "Your Name" \
    --email "you@example.com"
```

### What happens during install

1. **Dependency check** — verifies podman, git, and jq are
   available
2. **User identity** — prompts for name and email if not
   provided via flags (or reads from existing
   `~/.crucible/identity`)
3. **Clean previous install** — if `/opt/crucible` exists,
   backs it up with a timestamp suffix
4. **Clone crucible** — clones the repository to
   `/opt/crucible`, optionally checking out a specific
   branch or release
5. **Install subprojects** — runs `subprojects-install` to
   clone all component repos and create symlinks
6. **Pull controller image** — downloads the controller
   container image from the configured registry
7. **Configure registries** — creates `config/registries.json`
   with engine and controller registry settings
8. **Create sysconfig** — writes `/etc/sysconfig/crucible`
   with environment variables for the installation

## Install options

### Required

| Flag | Description |
|------|-------------|
| `--engine-registry <url>` | Registry URL for engine container images |

### Optional

| Flag | Description | Default |
|------|-------------|---------|
| `--controller-registry <url>` | Controller image URL | `<controller-registry-url>:latest` |
| `--name <name>` | User's full name | Prompted interactively |
| `--email <email>` | User's email address | Prompted interactively |
| `--release <version\|select>` | Install a specific release | Latest upstream |
| `--git-repo <url>` | Clone from a custom repository | Auto-detected or default upstream |
| `--git-branch <branch>` | Check out a specific branch | Default branch |
| `--engine-auth-file <file>` | Docker auth JSON for engine registry | None |
| `--engine-tls-verify <bool>` | TLS verification for engine registry | `true` |
| `--quay-engine-expiration-refresh-token <file>` | Quay OAuth token for image expiration | None |
| `--quay-engine-expiration-refresh-api-url <url>` | Quay API URL for expiration management | None |
| `--quay-engine-expiration-length <duration>` | Image expiration duration | `13w` |
| `--verbose` | Enable verbose output | Off |

### Release and management

| Flag | Description |
|------|-------------|
| `--set-release <version\|upstream\|select>` | Switch existing install to a different release |
| `--set-release-force` | Allow switching to unsupported (older) releases |
| `--list-releases` | List available releases and exit |

## Registry configuration

The installer creates `config/registries.json` which tells
crucible where to find container images:

- **Engine registry**: Where benchmark and tool engine images
  are stored and pulled from. This is the registry that the
  source-images-service pushes to and engines pull from.
- **Controller registry**: Where the controller image is
  stored. Typically the project's official controller image registry.

### Authentication

If the engine registry requires authentication, provide a
Docker/Podman auth JSON file via `--engine-auth-file`. This
file is referenced (not copied) by the configuration.

### Quay expiration management

For registries hosted on Quay.io, crucible can automatically
refresh image tag expirations to prevent auto-deletion of
cached images. This requires:

- An OAuth refresh token (`--quay-engine-expiration-refresh-token`)
- The Quay API URL (`--quay-engine-expiration-refresh-api-url`)
- Optionally, a custom expiration duration (`--quay-engine-expiration-length`, default 13 weeks)

## User identity

Crucible tracks who runs benchmarks for result attribution.
The user identity is stored at `~/.crucible/identity`:

```bash
CRUCIBLE_NAME="Jane Smith"
CRUCIBLE_EMAIL="jsmith@example.com"
```

This file is created during installation (prompted
interactively or set via `--name` and `--email` flags) and
preserved across reinstalls and updates. The name and email
appear in benchmark result metadata.

## The sysconfig file

The installer creates `/etc/sysconfig/crucible` which is
sourced by every crucible command to set up the environment:

```bash
CRUCIBLE_USE_CONTAINERS=1
CRUCIBLE_USE_LOGGER=1
CRUCIBLE_HOME=/opt/crucible
```

This file tells crucible where it's installed and enables
container-based execution and logging. It's sourced by every
crucible command as the first step of initialization.

## Release installs

To install a specific quarterly release:

```bash
./crucible-install.sh \
    --release 2026.1 \
    --engine-registry <engine-registry-url>
```

This clones all repos at the release branch and sets their
checkout mode to `locked`, ensuring the installation stays
at that exact version.

For interactive release selection:

```bash
./crucible-install.sh \
    --release select \
    --engine-registry <engine-registry-url>
```

This lists the supported releases (the four most recent) and
prompts for a choice.

## Switching releases

An existing installation can switch between releases using
`--set-release`:

```bash
./crucible-install.sh --set-release 2026.2
./crucible-install.sh --set-release upstream
./crucible-install.sh --set-release select
```

For details on how release switching works, see
[how-releases-work.md](how-releases-work.md).

## Post-install

After installation:

1. **Verify the install**: Run `crucible help` to see
   available commands
2. **Enable tab completion**: Source the completions file or
   re-login:
   ```bash
   source /etc/profile.d/crucible_completions.sh
   ```
3. **Run your first benchmark**: Create a run file and
   execute:
   ```bash
   crucible run my-benchmark.json
   ```

For how to write run files, see
[how-run-files-work.md](how-run-files-work.md).
