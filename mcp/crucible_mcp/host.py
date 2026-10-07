"""Helpers for running Crucible commands in the host execution context."""

from collections.abc import Mapping, Sequence
import sys
from pathlib import Path


_CONTAINER_RUNTIME_ENVIRONMENT = (
    "CONTAINER_HOST",
    "CONTAINER_CONNECTION",
    "CONTAINERS_STORAGE_CONF",
    "CONTAINERS_CONF",
    "PODMAN_CONNECTIONS_CONF",
    "DOCKER_HOST",
    "SESSION_ID",
    "PYTHONPATH",
    "PYTHONHOME",
    "CRUCIBLE_MCP_HOST_SERVICE_STARTS",
)
_MCP_SESSION_ENVIRONMENT = (
    "CRUCIBLE_MCP_SESSION_ID",
    "CRUCIBLE_MCP_EVENT_FILE",
)
DEFAULT_HOST_BRIDGE_SOCKET = Path(
    "/var/lib/crucible/mcp/host-bridge/bridge.sock"
)


def host_context_command(
    command: Sequence[str], working_directory: str = "/"
) -> list[str]:
    """Enter the host namespaces/root before executing a command."""

    return [
        "nsenter",
        "--mount=/proc/1/ns/mnt",
        "--cgroup=/proc/1/ns/cgroup",
        "--net=/proc/1/ns/net",
        "--root=/proc/1/root",
        f"--wdns={working_directory}",
        "--",
        *command,
    ]


def host_context_environment(
    environment: Mapping[str, str], *, preserve_mcp_session: bool = False
) -> dict[str, str]:
    """Remove container-only runtime overrides before using host Podman."""

    result = dict(environment)
    for name in _CONTAINER_RUNTIME_ENVIRONMENT:
        result.pop(name, None)
    if not preserve_mcp_session:
        for name in _MCP_SESSION_ENVIRONMENT:
            result.pop(name, None)
    return result


def host_bridge_command(
    command: Sequence[str],
    socket_path: str | Path = DEFAULT_HOST_BRIDGE_SOCKET,
    working_directory: str = "/",
) -> list[str]:
    """Run an allowlisted host operation through the private host bridge."""

    return [
        sys.executable,
        "-m",
        "crucible_mcp.host_bridge_client",
        "--socket",
        str(socket_path),
        "--working-directory",
        working_directory,
        "--",
        *command,
    ]


def host_bridge_environment(
    environment: Mapping[str, str], crucible_home: str | Path | None = None
) -> dict[str, str]:
    """Pass only MCP lifecycle correlation fields through the host bridge."""

    result = {
        name: environment[name]
        for name in _MCP_SESSION_ENVIRONMENT
        if name in environment
    }
    if crucible_home is not None:
        result["PYTHONPATH"] = str(Path(crucible_home) / "mcp")
    return result
