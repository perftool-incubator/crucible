# Implementing a New Endpoint

This guide explains how to implement a new endpoint type for
crucible. Endpoints are the deployment abstraction layer that
handles engine deployment, image management, service discovery,
and cleanup across different infrastructure targets.

For how endpoints operate at runtime, see
[how-endpoints-work.md](how-endpoints-work.md).
For how engines run inside endpoints, see
[how-engines-work.md](how-engines-work.md).
For how roadblock synchronization works, see
[how-roadblock-works.md](how-roadblock-works.md).

## When you need a new endpoint

You need a new endpoint when you want to deploy benchmark and
tool engines on an infrastructure type that isn't supported by
the existing endpoints:

- **remotehosts** — remote Linux hosts via SSH (podman or
  chroot)
- **kube** — Kubernetes clusters
- **osp** — OpenStack environments

Examples of infrastructure that would require a new endpoint:
cloud VMs on a specific provider (AWS, GCP, Azure), bare-metal
provisioning systems (Ironic), or specialized HPC schedulers
(Slurm).

## Directory structure

Each endpoint type lives in its own directory under
`rickshaw/endpoints/`:

```
endpoints/
├── endpoints.py              # Base module (shared by all)
├── myendpoint/
│   ├── myendpoint.py         # Main implementation
│   └── myendpoint            # Symlink: myendpoint -> myendpoint.py
```

The symlink is required — rickshaw invokes the endpoint by
running `./myendpoint` from the endpoint directory.

Additionally, create a JSON schema for validating endpoint
configuration in run files:

```
rickshaw/schema/myendpoint.json
```

### Optional marker files

Place these files in the endpoint directory to signal status:

- **`deprecated`** — contains a deprecation message shown to
  users. Indicates the endpoint will be removed in a future
  release.
- **`experimental`** — contains a warning message shown to
  users. Indicates the endpoint is not yet stable.

## How rickshaw discovers and invokes endpoints

### Discovery

Rickshaw uses directory-based discovery — no registration is
needed. If `endpoints/myendpoint/` exists with an executable
named `myendpoint`, rickshaw can use it. The endpoint type name
in the run file must match the directory name exactly.

### Invocation

Endpoints are invoked as **subprocesses**, not imported as
Python modules. Rickshaw calls the endpoint executable with
command-line arguments in two modes:

**Validation mode** (`--validate`):

```bash
./myendpoint \
    --endpoint-label=myendpoint-1 \
    --base-run-dir=/var/lib/crucible/run/latest \
    --rickshaw-dir=/opt/crucible/subprojects/core/rickshaw \
    --run-file=/path/to/run.json \
    --endpoint-index=0 \
    --crucible-dir=/opt/crucible \
    --log-level=normal \
    --validate \
    --<endpoint-specific-opts>
```

**Deployment mode** (no `--validate`):

```bash
./myendpoint \
    --rickshaw-dir=... \
    --packrat-dir=... \
    --endpoint-label=myendpoint-1 \
    --run-id=<uuid> \
    --base-run-dir=... \
    --max-sample-failures=1 \
    --endpoint-deploy-timeout=<seconds> \
    --engine-script-start-timeout=<seconds> \
    --image-map=<path-to-image-map.json> \
    --roadblock-id=<uuid> \
    --roadblock-passwd=<password> \
    --crucible-dir=... \
    --log-level=normal \
    --run-file=/path/to/run.json \
    --endpoint-index=0
```

### Two integration paths

Rickshaw currently has two integration paths for endpoints
(visible in `rickshaw-run.py`):

1. **Run-file native endpoints** (remotehosts, kube): Receive
   `--run-file` and `--endpoint-index` so they can read the
   run file directly and extract their own configuration.
   Also receive `--crucible-dir` and `--log-level`. Use the
   `endpoints.py` base module for settings management and
   roadblock integration.

2. **Legacy bash endpoints** (osp): Receive options as a single
   `--endpoint-opts=<serialized-string>` argument. Use the
   legacy `base` bash script.

New endpoints should follow the Python path.

## The validation protocol

When invoked with `--validate`, the endpoint must verify
connectivity, detect capabilities, and report them to stdout
using specific keywords that `rickshaw-run.py` parses:

### Required output keywords

| Keyword | Format | Purpose |
|---------|--------|---------|
| `arch` | `arch x86_64 [aarch64...]` | CPU architectures available |
| `engine-userenv` | `engine-userenv client 1 rhubi9` | Maps each engine (by role and ID) to its userenv |
| `engine-types` | `engine-types client server profiler` | Supported engine roles |
| `client` | `client 1 2 3` | Client engine IDs |
| `server` | `server 4 5 6` | Server engine IDs |
| `profiler` | `profiler <id>...` | Profiler engine IDs (if applicable) |

### Output conventions

- Lines starting with `#` are treated as comments (ignored by
  the parser)
- Lines starting with `ERROR:` signal validation failures
- All other non-empty lines must use a recognized keyword or
  rickshaw will abort with an error
- Use the helper functions from `endpoints.py`:
  - `endpoints.validate_log(msg)` — output a keyword line
  - `endpoints.validate_comment(msg)` — output a comment
  - `endpoints.validate_error(msg)` — output an error

### What validation should check

1. **Schema validation** — validate the endpoint section of the
   run file against `schema/myendpoint.json`
2. **Connectivity** — verify the endpoint can reach its
   infrastructure (SSH, API access, etc.)
3. **Architecture detection** — detect CPU architecture(s)
   available on the target infrastructure
4. **Capability detection** — verify required software or
   services are available (e.g., podman, kubectl)
5. **Engine enumeration** — report what engine roles and IDs
   this endpoint will provide

### Example validation output

```
# environment: {'HOME': '/root', ...}
# endpoint-settings: {'type': 'myendpoint', ...}
engine-types client server profiler
client 1 2
server 3 4
engine-userenv client 1 rhubi9
engine-userenv client 2 rhubi9
engine-userenv server 3 rhubi9
engine-userenv server 4 rhubi9
arch x86_64
```

## The base module (`endpoints.py`)

The `endpoints.py` module provides extensive shared
functionality. New endpoints should use these functions rather
than reimplementing them.

### Key function groups

**Entry point and settings:**

| Function | Purpose |
|----------|---------|
| `process_options()` | Parse standard CLI arguments via argparse; returns args namespace |
| `load_settings(settings, ...)` | Load run file, rickshaw settings, and endpoint config; calls your normalizer callback |
| `init_settings(settings, args)` | Initialize basic settings from CLI args |
| `log_settings(settings, ...)` | Log settings for debugging |
| `create_local_dirs(settings)` | Create local run/log/roadblock directories |

**Remote execution:**

| Function | Purpose |
|----------|---------|
| `remote_connection(host, user)` | Create SSH connection via Fabric with retry logic |
| `run_remote(connection, command)` | Execute command on remote host via SSH |
| `run_local(command)` | Execute command locally |

**Roadblock synchronization:**

| Function | Purpose |
|----------|---------|
| `process_pre_deploy_roadblock(...)` | Execute pre-deployment synchronization |
| `process_roadblocks(callbacks, ...)` | Main roadblock loop — manages all test phases |
| `process_bench_roadblocks(...)` | Handle per-iteration/sample roadblocks |
| `do_roadblock(...)` | Execute a single roadblock checkpoint |
| `create_roadblock_msg(...)` | Construct a roadblock message |
| `evaluate_roadblock(...)` | Evaluate roadblock result and handle abort/timeout |

**Image and engine lookups:**

| Function | Purpose |
|----------|---------|
| `get_image(settings, ...)` | Retrieve image info dict for a benchmark/tool |
| `get_engine_id_image(settings, ...)` | Get image info dict for a specific engine by role and ID |
| `get_benchmark(settings, id)` | Look up benchmark name from engine ID |
| `build_benchmark_engine_mapping(...)` | Build mapping of benchmarks to engine IDs |
| `expand_ids(ids)` | Expand ID range strings to lists |

**Validation output:**

| Function | Purpose |
|----------|---------|
| `validate_log(msg)` | Print a keyword line (parsed by rickshaw-run.py) |
| `validate_comment(msg)` | Print a comment line (prefixed with #) |
| `validate_error(msg)` | Print an error line (prefixed with ERROR:) |

**Network utilities:**

| Function | Purpose |
|----------|---------|
| `get_controller_ip(host)` | Determine controller IP reachable from a remote host |
| `is_ip(address)` | Validate an IPv4 or IPv6 address |

### The main pipeline

Every Python endpoint follows this pipeline in `main()`:

```python
def main():
    args = endpoints.process_options()
    endpoints.setup_logger(args.log_level)

    if args.validate:
        return validate()

    endpoints.init_settings(settings, args)
    endpoints.load_settings(
        settings,
        endpoint_name="myendpoint",
        endpoint_normalizer_callback=normalize_endpoint_settings
    )
    endpoints.create_local_dirs(settings)

    # Endpoint-specific deployment
    check_requirements()
    pull_images()
    launch_engines()

    # Pre-deployment synchronization
    endpoints.process_pre_deploy_roadblock(...)

    # Main test loop with callbacks
    callbacks = {
        "engine-init": engine_init,
        "collect-sysinfo": collect_sysinfo,
        "test-start": test_start,
        "test-stop": test_stop,
        "remote-cleanup": remote_cleanup,
        "rescue-engine-logs": rescue_engine_logs
    }
    return endpoints.process_roadblocks(
        callbacks=callbacks, ...
    )
```

## Settings normalization

Each endpoint must provide a `normalize_endpoint_settings()`
function that is passed as a callback to
`endpoints.load_settings()`. This function:

1. Merges endpoint-level default settings with per-host or
   per-config overrides
2. Expands engine ID ranges (e.g., `"1-5"` → `[1,2,3,4,5]`)
   using `endpoints.expand_ids()`
3. Resolves the controller IP address using
   `endpoints.get_controller_ip()`
4. Builds the internal data structures the endpoint needs for
   deployment

```python
def normalize_endpoint_settings(endpoint, rickshaw):
    # Apply defaults
    for key, default in endpoint_defaults.items():
        endpoint.setdefault(key, default)

    # Expand ID ranges
    for remote in endpoint["remotes"]:
        for engine in remote["engines"]:
            if "ids" in engine:
                engine["ids"] = endpoints.expand_ids(engine["ids"])

    # Resolve controller IP
    for remote in endpoint["remotes"]:
        host = remote["config"]["host"]
        remote["config"]["settings"].setdefault(
            "controller-ip-address",
            endpoints.get_controller_ip(host)
        )
```

## Engine deployment

### Creating engines

How you create engines depends on your infrastructure. The
existing endpoints demonstrate three approaches:

- **remotehosts/podman**: SSH to host, `podman create` + `podman
  start` with the engine container image
- **remotehosts/chroot**: SSH to host, `podman create` (without
  starting), mount the container filesystem, `chroot` into it
- **kube**: Generate Kubernetes Job CRDs, apply them with
  `kubectl create`

Regardless of method, each engine must:

1. Run the bootstrap script (`/usr/local/bin/bootstrap`) which
   is embedded in every engine container image
2. Have access to a shared data directory for inter-engine
   communication
3. Be able to reach the controller's Valkey instance for
   roadblock synchronization
4. Receive its configuration (engine name, role, run ID,
   controller IP, etc.)

### Configuration delivery

Engines receive configuration through different mechanisms
depending on the runtime:

- **Environment variables** (podman, kube): Written to an env
  file, passed via `--env-file` or pod spec
- **CLI arguments** (chroot): Passed directly to the bootstrap
  script
- **Command files** (all): Workload-specific parameters written
  to the shared data directory

### Thread pool pattern

Both remotehosts and kube use thread pools for parallel
deployment across multiple hosts or nodes. If your endpoint
manages multiple targets, follow this pattern using Python's
`threading` module with a work queue.

### Engine persistence

Engines are created once during the deployment phase and persist
for the entire run. They execute all iterations and samples
sequentially. This avoids the overhead of creating and
destroying engines for each sample.

## Image management

### Image map

Container images are passed to endpoints via the
`--image-map=<filepath>` CLI argument, pointing to a JSON file
(schema: `schema/image-map.json`) that maps benchmark and tool
names to container image URLs organized by role, userenv, and
architecture.

### Pulling images

Your endpoint must pull the required images onto its
infrastructure before launching engines.  Use the helper
functions:

- `endpoints.get_engine_id_image(settings, role, id, userenv,
  arch)` — get the image info for a specific engine
- `endpoints.get_image(settings, image_role, engine_role,
  userenv, arch)` — get image info by role and userenv

Both return a dict with an `image` key (the container image
URL) and an optional `auth-file` key (path to auth
credentials), or None if no matching image is found.

### Authentication

When an image requires authentication, the image info dict
contains an `auth-file` key with the path to a Docker/Podman
auth JSON file.  Your endpoint must:

1. Copy the auth file to the remote infrastructure (or create
   the equivalent, e.g., a Kubernetes ImagePullSecret)
2. Use the credentials during image pull
3. Clean up the credentials after pulling

## Roadblock integration

Endpoints participate as roadblock followers in the distributed
synchronization system. The base module handles most of the
complexity, but your endpoint must provide callbacks for key
lifecycle events.

### Pre-deployment

Before deploying engines, call
`endpoints.process_pre_deploy_roadblock()` to synchronize with
rickshaw-run and other endpoints. This is where endpoints can
exchange deployment metadata (e.g., additional follower IDs for
auto-provisioned profiler engines).

### The main roadblock loop

Call `endpoints.process_roadblocks()` with a callbacks dict.
This function manages the entire test execution lifecycle,
calling your callbacks at the appropriate phases:

```python
callbacks = {
    "engine-init": engine_init,
    "collect-sysinfo": collect_sysinfo,
    "test-start": test_start,
    "test-stop": test_stop,
    "remote-cleanup": remote_cleanup,
    "rescue-engine-logs": rescue_engine_logs
}
```

### Required callbacks

| Callback | When called | What to do |
|----------|-------------|------------|
| `engine-init` | After engines are running | Register engines as roadblock followers, send metadata (endpoint label, userenv, host info) |
| `collect-sysinfo` | During system info collection | Gather infrastructure metadata (host info, platform details) |
| `test-start` | Before each benchmark iteration | Set up networking (services, firewall rules) for client-server communication |
| `test-stop` | After each benchmark iteration | Tear down per-iteration networking |
| `remote-cleanup` | After all tests complete | Collect logs, destroy engines, clean up resources |
| `rescue-engine-logs` | On early roadblock failure | Collect engine logs without destroying engines or cleaning up — environment is left intact for debugging |

### Registering new followers

If your endpoint auto-provisions profiler engines (for tool
collection), add them to `settings["engines"]["new-followers"]`
before the roadblock loop. This tells roadblock to wait for
these additional participants.

## Service discovery

Client-server benchmarks need clients to discover where servers
are running. The mechanism depends on your endpoint's networking
model:

- **Direct IP** (remotehosts): Server publishes its host IP and
  ports via roadblock messaging. Client reads the message and
  connects directly.
- **Service abstraction** (kube): Server publishes pod IP via
  roadblock. Endpoint creates a networking abstraction (K8s
  Service) and sends the service IP to the client via a
  follow-up roadblock message.

Implement service discovery in your `test_start` callback by
reading server-start-end messages from the roadblock messages
directory and creating any necessary networking resources.

## Cleanup

Your `remote-cleanup` callback must:

1. **Collect engine logs** — retrieve stdout/stderr from each
   engine, compress, and store locally
2. **Destroy engines** — stop and remove containers, pods, VMs,
   or whatever your endpoint created
3. **Clean up networking** — remove services, firewall rules,
   or other networking resources
4. **Clean up authentication** — remove auth files or secrets
5. **Manage image cache** — optionally prune old images to
   conserve disk space

Your `rescue-engine-logs` callback must:

1. **Collect engine logs** — retrieve stdout/stderr from each
   engine, compress, and store locally (same as step 1 above)
2. **Preserve the environment** — do not destroy engines,
   clean up resources, or remove log files from remotes

This callback is invoked by `process_roadblocks()` when an
init-phase roadblock fails and the function exits early. The
normal `remote-cleanup` callback is never reached in this
case, so `rescue-engine-logs` ensures engine logs are captured
for debugging. The environment is intentionally left intact so
operators can inspect the failed state. Use WARNING log level
for operational messages within this callback to distinguish
rescue activity from normal operations. Wrap all remote
operations in `try/except` since the endpoint may be
unreachable — the failure that triggered the rescue may have
been caused by a connectivity loss.

## Schema definition

Create `schema/myendpoint.json` to validate your endpoint's
run file configuration. Follow the pattern from existing
schemas:

```json
{
    "title": "My Endpoint Schema",
    "description": "This schema defines the user interface to
        the myendpoint endpoint.",
    "type": "object",
    "properties": {
        "type": {
            "description": "The endpoint type identifier.",
            "type": "string",
            "enum": [ "myendpoint" ]
        },
        ...
    },
    "required": [ "type", ... ],
    "additionalProperties": false
}
```

Include `description` fields on every property. Reuse the
`number-lists` definition from existing schemas if your
endpoint uses engine ID specifications.

Validation in your endpoint code:

```python
from toolbox.json import load_json_file, validate_schema

valid, err = validate_schema(
    endpoint_settings,
    args.rickshaw_dir + "/schema/myendpoint.json"
)
if not valid:
    endpoints.validate_error(err)
    return 1
```

## CI integration

CI integration requires a runner pool that can provide the
infrastructure your endpoint needs. For example, the kube
endpoint requires a runner with a Kubernetes cluster, and
the remotehosts endpoint requires a runner with SSH-accessible
hosts.

If no suitable test environment is available, CI testing can
be deferred — not all endpoints can be CI-tested. The osp
endpoint, for instance, has no CI coverage because no runner
pool provides an OpenStack environment.

When a suitable environment exists, add your endpoint to the
CI configuration:

1. Define the endpoint in `crucible-ci.json` with its
   `enabled` state and capability `requirements`
2. Ensure a runner pool exists that `provides` those
   capabilities
3. Add benchmark scenarios that use your endpoint

See [how-ci-works.md](how-ci-works.md) for details on the
CI system.

## Reference implementations

### remotehosts (recommended starting point)

`rickshaw/endpoints/remotehosts/remotehosts.py` (~2500 lines)

The remotehosts endpoint is the simpler of the two Python
endpoints. It demonstrates:

- SSH-based remote execution via Fabric
- Dual runtime modes (podman and chroot)
- Thread pool deployment across multiple hosts
- Image pulling and cache management
- CPU partitioning
- Host mount configuration

### kube (complex reference)

`rickshaw/endpoints/kube/kube.py` (~3400 lines)

The kube endpoint demonstrates more advanced patterns:

- Kubernetes API interaction via kubectl
- Pod/Job CRD generation and management
- Namespace lifecycle management with ownership labels
- K8s Service creation for client-server networking
- ImagePullSecret management
- Architecture detection from K8s node API
- Multi-container pods (engine grouping)

### osp (legacy, not recommended)

`rickshaw/endpoints/osp/osp` (~700 lines, bash)

The osp endpoint uses the older bash integration path. It is
not recommended as a template for new endpoints — use the
Python pattern instead.

## Checklist

- [ ] Create `endpoints/myendpoint/` directory
- [ ] Write `myendpoint.py` with `validate()` and `main()`
- [ ] Create symlink `myendpoint -> myendpoint.py`
- [ ] Create `schema/myendpoint.json` with description fields
- [ ] Implement validation: connectivity, arch detection,
      schema validation, engine enumeration
- [ ] Implement settings normalization callback
- [ ] Implement engine deployment (create + start)
- [ ] Implement image pulling with auth support
- [ ] Implement roadblock callbacks (engine-init,
      collect-sysinfo, test-start, test-stop, remote-cleanup,
      rescue-engine-logs)
- [ ] Implement service discovery in test-start callback
- [ ] Implement cleanup: log collection, engine teardown,
      resource cleanup
- [ ] Implement rescue: log-only collection for early failure
      path (no engine teardown, no resource cleanup)
- [ ] Test with a simple benchmark (e.g., `sleep`)
- [ ] Add CI integration if a suitable test environment exists
