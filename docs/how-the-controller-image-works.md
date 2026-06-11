# How the Controller Image Works

This document explains the crucible controller image — the
container that all crucible operations run inside. It covers
what the image contains, how it's built, how it's updated,
and how the automated build pipeline works.

For how engine images are built, see
[how-image-sourcing-works.md](how-image-sourcing-works.md).
For how services run inside the controller, see
[how-services-work.md](how-services-work.md).

## Overview

The controller image is a Fedora-based container image
containing all the software needed to orchestrate benchmark
runs. Every `crucible` command runs inside this image — when
you type `crucible run`, `crucible update`, or
`crucible start`, the command is executed inside a controller
container.

A key property: **the controller image is shared across all
releases.** There is one controller image used by all crucible
installations, regardless of which release they're running.
This is why controller image changes trigger multi-release
CI testing — a change could affect any release.

## What it contains

The controller image includes everything needed for
benchmark orchestration, service management, and image
building:

### Core components

The orchestration framework and its dependencies:

- **Rickshaw** — benchmark orchestration, source-images-service
- **Toolbox** — shared Python and Bash libraries
- **Roadblock** — distributed synchronization client and
  utilities
- **CommonDataModel** — result schema, query engine, web UI
- **Multiplex** — parameter expansion engine
- **Workshop** — container image building engine

### Container tools

The controller builds engine images internally, so it needs
its own container tooling:

- **Podman** — container runtime (for building and running
  nested containers)
- **Buildah** — image building
- **Skopeo** — image inspection and registry operations
- **fuse-overlayfs** — overlay filesystem for rootless
  containers

### Services

Infrastructure services that run as processes within
controller containers:

- **Valkey** — Redis-compatible backend for roadblock
- **OpenSearch** — result indexing and search
- **httpd** — web UI for browsing results
- **CDM server** — result query API

### Build tools and languages

- **Python 3** with pip, FastAPI, uvicorn, pydantic,
  jsonschema, invoke
- **Perl** with JSON, validation, REST, and UUID modules
- **GCC, make, autoconf, automake, libtool** — for source
  builds during engine image construction
- **Node.js** — for CDM query engine

### Utilities

- git, curl, jq, ssh tools, network utilities
- GitHub CLI (gh) — for GitHub API operations
- tar, xz, gzip — for data archival

## Contributing repos

The controller image is built from the workshop.json files
of 7 repos, each contributing its own dependencies:

| Repo | What it adds |
|------|-------------|
| crucible | Core packages: podman, buildah, valkey, opensearch, httpd, build tools, gh CLI |
| rickshaw | Python packages for orchestration (FastAPI, pydantic, etc.) |
| workshop | Workshop script dependencies (Perl modules for JSON, HTTP) |
| toolbox | Python libraries and utilities |
| roadblock | Python libraries for synchronization |
| CommonDataModel | Node.js runtime and CDM packages |
| multiplex | Python jsonschema |

### Controller vs engine userenvs

Each contributing repo's `workshop.json` has entries for the
`crucible-controller` userenv. This is distinct from the
userenvs used for engine images (like `rhubi9`,
`fedora-latest`, `alma8`):

- **Controller userenv** (`crucible-controller`): Fedora-based,
  contains orchestration software. Only built as the
  controller image.
- **Engine userenvs** (`rhubi9`, etc.): Various distributions,
  contain benchmark/tool software. Built per-benchmark by
  the source-images-service.

Some core repos target both images. For example, roadblock's
`workshop.json` has entries for `crucible-controller` (the
leader runs on the controller) and engine userenvs (followers
run on engines). Packrat targets engine images only.

## How it's built

The `workshop/controller-image.py` tool manages the build
process with three subcommands:

### Build

```bash
controller-image.py build
```

Assembles the image by:

1. Loading the controller configuration from
   `workshop/controller.json`
2. Collecting workshop.json files from all 7 contributing
   repos
3. Calling `workshop.py` with all requirements to build the
   image layer by layer
4. Embedding provenance data into the image

### Push

```bash
controller-image.py push --authfile /path/to/auth.json
```

Pushes the built image to the container registry with a
content-based tag.

### Manifest

```bash
controller-image.py manifest <tag>
```

Creates a multi-architecture manifest combining the x86_64
and aarch64 images under a single tag. This allows users to
pull the correct architecture automatically.

## Image tagging

### Composite hash

The controller image tag is based on a **composite hash** —
a SHA-256 of the HEAD commits of all 7 contributing repos.
This provides content-based addressing: the same set of
repo versions always produces the same tag, and any change
to any contributing repo produces a new tag.

### Tag format

Architecture-specific images use:

```
<registry>:<YYYY-MM-DD>_<composite_hash>_<arch>
```

For example:
`<registry>/<repo>:2026-06-10_a1b2c3d4_x86_64`

### Tag management

The registry maintains special tags:

- **`latest`** — the current recommended controller image
- **`previous`** — the former `latest` (kept for rollback)

When a new image is published, the automated pipeline:
1. Moves the current `latest` to `previous`
2. Moves the new manifest to `latest`

## Automated build pipeline

Controller rebuilds are triggered automatically when
contributing repos merge changes that affect the image.

### Trigger chain

1. A subproject PR merges to its default branch
2. The subproject's `controller-build.yaml` workflow fires
3. It generates a GitHub App token and dispatches a
   `controller-build` event to the crucible repo
4. Crucible's `build-publish-controller.yaml` workflow
   receives the dispatch
5. It calls `build-publish-controller-worker.yaml`

### Build process

The worker workflow runs three jobs:

1. **Build and push** (matrix: x86_64 + aarch64):
   - Each architecture runs on a native self-hosted runner
   - Installs crucible fresh
   - Runs `controller-image.py build` then
     `controller-image.py push`
   - Both architectures build in parallel

2. **Create manifest** (after both builds complete):
   - Verifies both architecture images exist in the registry
   - Creates a multi-arch manifest
   - Pushes the manifest

3. **Update tags** (after manifest):
   - Rotates `latest` → `previous` via the Quay API
   - Points `latest` to the new manifest

### Cross-repo dispatch

Subproject repos can't directly trigger crucible workflows
(they're in different repos). The system uses a GitHub App
(`APP_ID__CRUCIBLE_WORKFLOW_DISPATCH`) to generate tokens
that allow cross-repo `repository_dispatch` events.

## How crucible uses the image

### Configuration

The controller image URL is configured in
`config/registries.json` and loaded as the
`CRUCIBLE_CONTROLLER_IMAGE` variable.

### Runtime

Every crucible command runs inside a controller container
via podman. The `bin/base` script sets up container arguments
that mount the crucible installation, user home directories,
and result storage into the container. Services run as
detached containers; one-off commands run as ephemeral
containers.

The controller container runs with host networking, host
PID namespace, and privileged mode. This gives it full access
to the host's network stack and devices, which is necessary
for managing engine containers, SSH connections to remote
hosts, and accessing hardware for benchmarking.

### The `crucible wrapper` and `crucible console` commands

For ad-hoc operations, two commands provide direct access
to the controller environment:

- **`crucible wrapper <command>`** — runs a single command
  inside the controller container and returns. Useful for
  running scripts that depend on software installed in the
  controller image (Python packages, Perl modules, etc.)
  that may not be available on the host.

- **`crucible console`** — opens an interactive bash shell
  inside the controller container. Useful for exploring the
  controller environment, debugging, or running multiple
  commands interactively. The session persists until you
  exit the shell.

The controller image is purposefully configured with full
man page documentation for all installed software. Container
base images typically strip man pages via `tsflags=nodocs`
in the package manager configuration to save space. The
controller build process removes this restriction and
reinstalls all packages to restore their man pages. This
means when using `crucible console`, you can run `man`
commands to inspect the exact documentation for the
specific versions of tools installed in the controller —
useful when debugging or investigating behavior that
depends on precise tool versions.

## Updating the controller image

### How updates work

When `crucible update` runs (for `all`, `crucible`, or
`controller-image` targets):

1. Records the current image ID
2. Pulls the latest image: `podman pull <image>`
3. Compares old vs new image ID
4. If different: removes the old image to free space

### Shared-across-releases implication

Because the controller image is shared across all releases,
updating the controller image affects the installation
regardless of which release it's running. A controller update
brings new orchestration capabilities, service versions, and
build tools to every release.

This is intentional — the controller handles orchestration,
not workload execution. Workload behavior is determined by
the engine images (which are release-specific), not the
controller.

## Provenance

Every controller image embeds build traceability data at
`/etc/crucible/build-provenance.json`:

```json
{
    "build-date": "2026-06-10T12:34:56Z",
    "composite-hash": "a1b2c3d4e5f6...",
    "source": {
        "hostname": "build-host.example.com",
        "ip": "10.1.2.3"
    },
    "repos": {
        "crucible": { "commit": "abc123..." },
        "rickshaw": { "commit": "def456..." },
        "toolbox": { "commit": "ghi789..." },
        "roadblock": { "commit": "jkl012..." },
        "workshop": { "commit": "mno345..." },
        "CommonDataModel": { "commit": "pqr678..." },
        "multiplex": { "commit": "stu901..." }
    }
}
```

The same data is also stored as OCI image annotations,
making it accessible via `skopeo inspect` or
`buildah inspect` without pulling the full image.

This enables exact reproduction of any controller image
and makes it clear which code versions are running in any
installation.
