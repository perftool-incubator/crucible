# How Image Sourcing Works

This document explains how crucible builds the container images
that benchmark and tool engines run in. It covers the three-
component pipeline, the multi-stage build approach, content-
based tagging, multi-architecture support, and provenance
tracking.

For how engines use these images, see
[how-engines-work.md](how-engines-work.md).
For how workshop.json files are structured, see
[implementing-a-new-benchmark.md](implementing-a-new-benchmark.md)
and
[implementing-a-new-tool.md](implementing-a-new-tool.md).

## Overview

Every benchmark and tool needs a container image with its
software and dependencies installed. Image sourcing is the
process of building these images. Each benchmark and tool
declares its build requirements in a `workshop.json` file,
and the source-images-service builds images incrementally
using a multi-stage approach where each stage is independently
cached.

The key design property: **images are tagged by content hash.**
The same inputs (workshop requirements, base image, toolbox
version, etc.) always produce the same image tag, regardless
of when or where the build runs. This means images are
automatically reused across runs when nothing has changed,
and automatically rebuilt when something has.

## The pipeline

Image sourcing involves three components:

```
rickshaw-run.py (orchestrator)
    │
    │  determines what images are needed
    │  writes input JSON with file paths
    ▼
rickshaw-source-images-client.py (bridge)
    │
    │  encodes files as base64
    │  submits job via HTTP API
    │  polls for completion
    ▼
source-images-service (builder)
    │
    │  materializes workspace
    │  builds images incrementally
    │  pushes to registry
    ▼
container registry (<registry>/<repo>)
```

### Orchestrator (rickshaw-run.py)

The orchestrator determines which images are needed based on
the benchmarks in the run, the tools configured, and the
architectures reported by endpoints. It builds an `image_ids`
data structure mapping each benchmark/tool × userenv ×
architecture to an image URL (initially empty).

Before calling the builder, the orchestrator deduplicates:
the same benchmark builds identically for all architectures
(only the final tag suffix differs), so the bench-dirs and
workshop content are shared.

### Bridge (rickshaw-source-images-client.py)

The bridge translates local file paths into a self-contained
API request. It recursively collects all files from benchmark
directories, userenv definitions, toolbox, roadblock, and
workshop content, base64-encoding them for transport. The
source-images-service runs inside the controller container
and may not have direct access to the host filesystem, so
everything must be transmitted via the API.

After submitting the job, the bridge polls for completion
with adaptive intervals (faster initially, slowing over
time). It streams build logs to stdout so the user can
monitor progress.

### Builder (source-images-service)

The source-images-service (SIS) is a FastAPI web service that
runs inside the crucible controller container. It receives
the base64-encoded workspace, materializes it to a temporary
directory, and executes the multi-stage image build.

## What drives the build: workshop.json

Each benchmark and tool has a `workshop.json` file declaring
what needs to be installed in its engine image.

### Userenvs

A userenv (user environment) defines the base container image.
Userenvs are JSON files specifying an OS distribution and
version (e.g., `rhubi9`, `fedora-latest`, `alma8`). The
workshop requirements are layered on top of this base image.

A single workshop.json can define different requirements for
different userenvs, allowing the same benchmark to support
multiple OS distributions with distribution-specific package
names or build procedures.

### Requirement types

Workshop.json supports several requirement types:

- **distro**: Install packages via the distribution's package
  manager (rpm, deb)
- **source**: Download and build from source tarball
- **python3**: Install pip packages
- **files**: Copy files from the benchmark directory into the
  image
- **manual**: Execute arbitrary shell commands
- **cpan**: Install Perl modules
- **node**: Install npm packages

### Split workshop files

Benchmarks with fundamentally different client and server
requirements can use separate `client-workshop.json` and
`server-workshop.json` files instead of a single
`workshop.json`. This produces different images for each
role — for example, trafficgen's client image includes TRex
and extensive build tools, while its server image only needs
dpdk-tools.

## Multi-stage incremental builds

The most important efficiency mechanism in image sourcing is
the multi-stage build approach. Instead of building a single
monolithic image, the SIS builds in stages, each adding one
layer of requirements on top of the previous stage.

### Stage ordering

Stages are ordered from most stable to most volatile:

1. **Base userenv** — the OS base image (rarely changes)
2. **Toolbox files** — shared library files
3. **Toolbox dependencies** — toolbox's workshop.json
   requirements
4. **Roadblock dependencies** — Python libraries for
   synchronization
5. **Rickshaw engine** — engine scripts and workshop
   requirements
6. **Utilities** — optional utility workshop.json files
   (e.g., packrat)
7. **Benchmark/tool** — the benchmark's own workshop.json
   requirements (most likely to change)

### Why this ordering matters

Each stage gets its own content hash. When you modify a
benchmark script, only stage 7 changes — stages 1 through 6
are reused from cache. Since the common infrastructure stages
are shared across all benchmarks and tools, a benchmark change
only triggers a rebuild of the final stage, typically taking
seconds rather than minutes.

### Stage reuse

For each stage, the SIS checks (in order):

1. **Remote registry**: Does an image with this tag already
   exist? (checked via `skopeo inspect`)
2. **Local store**: Is the image available locally? (checked
   via `buildah images`)

If found remotely, the stage is ready. If found locally but
not remotely, it's pushed to the registry. Only if the stage
is missing entirely is it built from the previous stage.

The search works backward from the final stage — if the
complete image already exists in the registry, no building
happens at all.

## Content-based image tagging

Image tags are SHA256 hashes of the stage's content:

```
<registry>/<repo>:<hash>_<arch>
```

For example: `quay.io/crucible/engines:a1b2c3d4e5f6_x86_64`

### How the hash is computed

For each stage, the SIS:

1. Runs the workshop script in `--dump-config` mode to get
   the resolved configuration
2. Runs `--dump-files` to discover all input files
3. Normalizes workspace paths to a canonical prefix (so the
   hash doesn't depend on the random temp directory name)
4. Computes SHA256 over the config text + all file contents

### Determinism

The same inputs always produce the same hash:

- Path normalization ensures builds on different hosts with
  different temp directories produce identical tags
- File contents are hashed in sorted order
- The hash captures everything that affects the image:
  package lists, source URLs, build commands, file contents

This means if nothing has changed since the last run, image
sourcing finds all stages already built and completes in
seconds.

## Multi-architecture support

Crucible supports building images for multiple CPU
architectures (e.g., x86_64 and aarch64) in a single run.

### Architecture discovery

During endpoint validation, each endpoint reports its CPU
architectures:

- **remotehosts**: `uname -m` on each remote host
- **kube**: Reads `kubernetes.io/arch` from node info,
  normalizes K8s names to Linux names (`amd64` → `x86_64`,
  `arm64` → `aarch64`)

### Per-architecture SIS instances

Each architecture requires its own SIS instance running on a
host with that native architecture. Cross-architecture builds
(e.g., building aarch64 images on x86_64) are not supported
due to buildah/QEMU incompatibilities.

The `config/services.json` file configures SIS instances:

```json
{
    "image-sourcing": {
        "services": {
            "default": {
                "start": true,
                "location": {
                    "address": "localhost",
                    "port": 8888,
                    "protocol": "http"
                }
            },
            "aarch64": {
                "start": false,
                "location": {
                    "address": "arm-builder.example.com",
                    "port": 8888,
                    "protocol": "http"
                }
            }
        }
    }
}
```

The `"default"` key maps to the controller's native
architecture. Non-native architectures must point to remote
builders with `"start": false`.

### Parallel sourcing

When a run requires multiple architectures, rickshaw sources
images in parallel — one thread per architecture, each
communicating with its own SIS instance. The
`image-sourcing-urls.json` file maps each architecture to its
service URL.

## Registry and caching

### Image storage

All engine images are stored in a single container registry
(default: `<registry>/<repo>`). Each stage of each
build gets its own tag in this registry, enabling fine-grained
caching.

### Build coordination

When multiple runs are executing simultaneously, they may
need the same image. The SIS includes a build coordinator
that prevents redundant builds: if one job is already
building a particular tag, other jobs requesting the same tag
wait for the first build to complete rather than starting
their own.

### Image expiration

The registry (Quay) supports tag expiration — images are
automatically deleted after a configurable period if not
refreshed. During image sourcing, the SIS refreshes the
expiration timer on all existing stages it discovers. This
ensures actively-used images persist while abandoned images
are eventually cleaned up.

## The SIS service

### How it runs

The SIS runs inside the crucible controller container,
managed by `crucible start image-sourcing` and
`crucible stop image-sourcing`. It listens on a configurable
port (default 8888) and validates that incoming requests
match its native architecture.

### Health endpoint

The SIS exposes `/api/v1/health` which reports its status and
native architecture. Crucible polls this endpoint during
startup to wait for the service to be ready before proceeding
with a run.

### Dedicated builders

For non-native architectures, a dedicated builder host runs
the SIS as a systemd service. The setup script
(`bin/services/setup-image-sourcing-service.sh`) installs a
systemd unit that starts the SIS automatically on boot.
The builder host needs a full crucible installation.

### Workspace materialization

When the SIS receives a job, it materializes the
base64-encoded content into a temporary directory tree:

```
/tmp/source-images-service/job-<id>/
├── workshop/      # workshop script and schema
├── toolbox/       # toolbox files
├── roadblock/     # roadblock workshop.json
├── rickshaw/      # engine scripts and userenvs
├── bench-dirs/    # benchmark directories
├── config/        # userenv JSON files
├── registries/    # registry config and tokens
└── build/         # build output and audit files
```

This workspace is deleted after the job completes.

## Provenance tracking

Every image records exactly what went into building it. The
SIS embeds `/etc/crucible/build-provenance.json` as the final
layer of each image:

```json
{
    "build-date": "2026-06-10T12:34:56Z",
    "source": {
        "hostname": "controller.example.com",
        "ip": "10.1.2.3"
    },
    "repos": {
        "toolbox": {
            "commit": "abc123...",
            "dirty": "false"
        },
        "roadblock": {
            "commit": "def456...",
            "dirty": "false"
        },
        "mybench": {
            "commit": "ghi789...",
            "dirty": "true",
            "diff": "--- a/mybench-client\n+++ b/mybench-client\n..."
        }
    }
}
```

Each contributing repository's git commit and dirty status is
recorded. If a repo has uncommitted changes, the diff is
included. This enables exact reproduction of any image and
makes it clear whether a build used modified code.
