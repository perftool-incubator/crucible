"""Allowlisted host execution service for the unprivileged MCP listener.

The bridge runs in a separate, local-only controller container with host
namespace access. The network-facing MCP listener can request only the
specific Crucible and supervisor operations that MCP exposes; it cannot ask
the bridge to execute an arbitrary host command.
"""

import argparse
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import subprocess
import sys
import uuid
from typing import Any

from .host import host_context_command, host_context_environment
from .host_bridge_client import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES

_SUPERVISOR_INSPECT_FORMAT = "{{.Id}}|{{.State.Status}}|{{.State.ExitCode}}|{{.State.Pid}}"
_JOB_NAME = re.compile(r"^crucible-mcp-job-([0-9a-f-]{36})$")
_RUN_NAME = re.compile(r"^crucible-rickshaw-run-([0-9a-f-]{36})$")
_ALLOWED_OPERATIONS = {
    "run",
    "postprocess",
    "index",
    "delete_indexed_result",
    "archive_local_run",
    "unarchive_local_run",
}


class BridgeRequestError(ValueError):
    """The request is outside the MCP host-operation contract."""


class HostCommandPolicy:
    def __init__(
        self,
        crucible_home: Path,
        controller_image: str,
        job_root: Path,
        run_root: Path,
        archive_root: Path,
    ):
        self.home = crucible_home.resolve(strict=True)
        self.controller_image = controller_image
        self.job_root = job_root.resolve(strict=True)
        self.run_root = run_root.resolve(strict=True)
        self.archive_root = archive_root.resolve(strict=True)
        self.crucible_command = str(
            (self.home / "bin" / "crucible").resolve(strict=True)
        )

    def _canonical_cli_command(self, command: list[str]) -> list[str] | None:
        if not command or not Path(command[0]).is_absolute():
            return None
        try:
            executable = Path(command[0]).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if str(executable) != self.crucible_command:
            return None
        return [self.crucible_command, *command[1:]]

    @staticmethod
    def _canonical_child(candidate: str, root: Path, *, directory: bool) -> Path:
        path = Path(candidate)
        if not path.is_absolute():
            raise BridgeRequestError("host operation paths must be absolute")
        try:
            canonical = path.resolve(strict=True)
            canonical.relative_to(root)
        except (OSError, ValueError) as exc:
            raise BridgeRequestError("host path is outside the configured MCP operation roots") from exc
        if canonical == root:
            raise BridgeRequestError("host operation cannot target an approved root itself")
        if directory and not canonical.is_dir():
            raise BridgeRequestError("host operation target must be a directory")
        if not directory and not canonical.is_file():
            raise BridgeRequestError("host operation target must be a regular file")
        return canonical

    @staticmethod
    def _uuid(value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError) as exc:
            raise BridgeRequestError("invalid MCP job identity") from exc
        if str(parsed) != value.lower():
            raise BridgeRequestError("invalid MCP job identity")
        return str(parsed)

    def _validate_cli_operation(
        self, operation: str, command: list[str], job_directory: Path | None = None
    ) -> None:
        command = self._canonical_cli_command(command) or []
        if not command:
            raise BridgeRequestError("only Crucible CLI operations may run through the bridge")
        args = command[1:]
        expected_cli = {
            "run": "run",
            "postprocess": "postprocess",
            "index": "index",
            "delete_indexed_result": "rm",
            "archive_local_run": "archive",
            "unarchive_local_run": "unarchive",
        }.get(operation)
        if not expected_cli or not args or args[0] != expected_cli:
            raise BridgeRequestError("CLI command does not match the MCP job operation")

        if operation == "run":
            if len(args) != 2:
                raise BridgeRequestError("run operation has invalid arguments")
            input_file = self._canonical_child(args[1], self.job_root, directory=False)
            if input_file.name != "run-file.json" or input_file.parent.name != "input":
                raise BridgeRequestError("run operation must use its staged run file")
            try:
                self._uuid(input_file.parent.parent.name)
            except BridgeRequestError:
                raise BridgeRequestError("run file is not in an MCP job directory") from None
            if job_directory is not None:
                try:
                    input_file.relative_to(job_directory)
                except ValueError as exc:
                    raise BridgeRequestError("run file is outside its MCP job directory") from exc
            return

        if operation == "delete_indexed_result":
            if len(args) != 3 or args[1] != "--run" or not args[2] or len(args[2]) > 256:
                raise BridgeRequestError("indexed-result deletion has invalid arguments")
            if "\x00" in args[2]:
                raise BridgeRequestError("indexed-result identifier is invalid")
            return

        if len(args) != 2:
            raise BridgeRequestError("local run operation has invalid arguments")
        if operation in {"postprocess", "index"}:
            for root in (self.run_root, self.job_root):
                try:
                    self._canonical_child(args[1], root, directory=True)
                    return
                except BridgeRequestError:
                    continue
            raise BridgeRequestError("local processing target is outside the configured run roots")

        is_unarchive = operation == "unarchive_local_run"
        root = self.archive_root if is_unarchive else self.run_root
        target = self._canonical_child(args[1], root, directory=not is_unarchive)
        if target.parent != root:
            raise BridgeRequestError("archive operations must target a direct root child")
        if is_unarchive and target.suffixes[-2:] != [".tar", ".xz"]:
            raise BridgeRequestError("unarchive operation must target a .tar.xz file")

    def _validate_supervisor_run(self, command: list[str]) -> None:
        if len(command) < 36 or command[:5] != [
            "podman", "run", "--detach", "--rm", "--pull=never"
        ]:
            raise BridgeRequestError("unsupported host Podman command")
        if command[5] != "--name":
            raise BridgeRequestError("supervisor container name is required")
        match = _JOB_NAME.fullmatch(command[6])
        if not match:
            raise BridgeRequestError("unsupported supervisor container name")
        job_id = self._uuid(match.group(1))
        job_directory = (self.job_root / job_id).resolve(strict=True)
        if job_directory.parent != self.job_root or not job_directory.is_dir():
            raise BridgeRequestError("supervision directory is outside the configured job root")
        if command[7:11] != [
            "--label",
            f"io.crucible.mcp.job-id={job_id}",
            "--label",
            "io.crucible.mcp.supervisor=true",
        ]:
            raise BridgeRequestError("supervisor labels do not match the job")

        session_value = command[21] if len(command) > 21 else ""
        event_value = command[23] if len(command) > 23 else ""
        operation = command[29] if len(command) > 29 else ""
        try:
            session_id = self._uuid(session_value.removeprefix("CRUCIBLE_MCP_SESSION_ID="))
        except BridgeRequestError:
            raise BridgeRequestError("invalid supervisor session identity") from None
        event_path = str(job_directory / "events.jsonl")
        expected = [
            "podman", "run", "--detach", "--rm", "--pull=never", "--name", command[6],
            "--label", f"io.crucible.mcp.job-id={job_id}", "--label",
            "io.crucible.mcp.supervisor=true", "--privileged", "--pid=host", "--ipc=host",
            "--net=host", "--security-opt=label=disable",
            f"--mount=type=bind,source={job_directory},destination=/job",
            f"--mount=type=bind,source={self.home},destination={self.home}",
            "--env", f"PYTHONPATH={self.home / 'mcp'}", "--env",
            f"CRUCIBLE_MCP_SESSION_ID={session_id}", "--env",
            f"CRUCIBLE_MCP_EVENT_FILE={event_path}", self.controller_image,
            "python3", "-m", "crucible_mcp.supervisor", "--operation", operation,
            "--job-directory", "/job", "--working-directory", str(self.home), "--",
        ]
        if command[:35] != expected or event_value != f"CRUCIBLE_MCP_EVENT_FILE={event_path}":
            raise BridgeRequestError("supervisor launch arguments do not match the host policy")
        if operation not in _ALLOWED_OPERATIONS:
            raise BridgeRequestError("unsupported MCP supervisor operation")
        self._validate_cli_operation(operation, command[35:], job_directory)

    def validate(self, request: dict[str, Any]) -> tuple[list[str], str, float | None]:
        command = request.get("command")
        cwd = request.get("working_directory")
        environment = request.get("environment", {})
        if (
            not isinstance(command, list)
            or not command
            or len(command) > 256
            or any(not isinstance(item, str) or "\x00" in item for item in command)
        ):
            raise BridgeRequestError("invalid host command")
        command = self._canonical_cli_command(command) or command
        if not isinstance(cwd, str) or cwd not in {"/", str(self.home)}:
            raise BridgeRequestError("working directory is outside the host bridge policy")
        if not isinstance(environment, dict) or set(environment) - {
            "CRUCIBLE_MCP_SESSION_ID",
            "CRUCIBLE_MCP_EVENT_FILE",
        }:
            raise BridgeRequestError("unsupported host environment override")
        if environment:
            session_id = environment.get("CRUCIBLE_MCP_SESSION_ID")
            event_path = environment.get("CRUCIBLE_MCP_EVENT_FILE")
            if not isinstance(session_id, str) or not isinstance(event_path, str):
                raise BridgeRequestError("MCP correlation fields must be supplied together")
            self._uuid(session_id)
            canonical_event = self._canonical_child(
                event_path, self.job_root, directory=False
            )
            if canonical_event.name != "events.jsonl":
                raise BridgeRequestError("MCP event file is outside its approved job path")
            try:
                self._uuid(canonical_event.parent.name)
            except BridgeRequestError:
                raise BridgeRequestError("MCP event file is outside its approved job path") from None

        if command == [self.crucible_command, "start", "opensearch"] and cwd == str(self.home):
            timeout = 300.0
        elif command[0] == "podman" and len(command) == 5 and command[1:4] in (
            ["inspect", "--format", _SUPERVISOR_INSPECT_FORMAT],
            ["inspect", "--format", "{{.Id}}"],
        ):
            if not (_JOB_NAME.fullmatch(command[4]) or _RUN_NAME.fullmatch(command[4])):
                raise BridgeRequestError("unsupported container inspection target")
            if command[3] == "{{.Id}}" and not _RUN_NAME.fullmatch(command[4]):
                raise BridgeRequestError("unsupported container inspection target")
            timeout = 15.0
        elif command[0] == "podman" and len(command) == 3 and command[1] in {"wait", "stop"}:
            if not _JOB_NAME.fullmatch(command[2]):
                raise BridgeRequestError("unsupported supervisor container target")
            timeout = None if command[1] == "wait" else 30.0
        elif command[:2] == ["podman", "run"]:
            if cwd != "/":
                raise BridgeRequestError("supervisor launch must use the host root")
            self._validate_supervisor_run(command)
            timeout = 30.0
        elif command[0] == self.crucible_command:
            operation = next(
                (
                    name
                    for name, cli in {
                        "run": "run",
                        "postprocess": "postprocess",
                        "index": "index",
                        "delete_indexed_result": "rm",
                        "archive_local_run": "archive",
                        "unarchive_local_run": "unarchive",
                    }.items()
                    if len(command) > 1 and command[1] == cli
                ),
                "",
            )
            self._validate_cli_operation(operation, command)
            if cwd != str(self.home):
                raise BridgeRequestError("Crucible CLI operation must use the configured checkout")
            timeout = 24 * 60 * 60.0
        else:
            raise BridgeRequestError("host command is not in the MCP host-operation allowlist")

        child_environment = host_context_environment(os.environ)
        for name, value in environment.items():
            if not isinstance(value, str) or "\x00" in value or len(value) > 4096:
                raise BridgeRequestError("invalid MCP environment value")
            child_environment[name] = value
        return host_context_command(command, cwd), child_environment, timeout


class _ThreadingUnixStreamServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        response: dict[str, Any]
        if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
            response = {"returncode": 125, "stdout": "", "stderr": "", "error": "request too large or incomplete"}
        else:
            try:
                request = json.loads(raw.decode("utf-8"))
                if not isinstance(request, dict):
                    raise BridgeRequestError("request must be a JSON object")
                command, environment, timeout = self.server.policy.validate(request)
                result = subprocess.run(
                    command,
                    cwd=request["working_directory"],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                    timeout=timeout,
                )
                response = {
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            except subprocess.TimeoutExpired as exc:
                response = {
                    "returncode": 124,
                    "stdout": _as_text(exc.stdout),
                    "stderr": _as_text(exc.stderr),
                    "error": "host operation timed out",
                }
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                response = {"returncode": 125, "stdout": "", "stderr": "", "error": str(exc)}
        encoded = json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > MAX_RESPONSE_BYTES:
            encoded = json.dumps(
                {"returncode": 125, "stdout": "", "stderr": "", "error": "host command output exceeded response limit"},
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
        try:
            self.wfile.write(encoded)
        except OSError:
            return


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--crucible-home", type=Path, required=True)
    parser.add_argument("--controller-image", required=True)
    parser.add_argument("--job-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.socket.is_symlink():
        parser.error("refusing to bind a symlink socket path")
    if args.socket.exists():
        if not args.socket.is_socket():
            parser.error("existing host bridge path is not a socket")
        args.socket.unlink()
    policy = HostCommandPolicy(
        args.crucible_home,
        args.controller_image,
        args.job_root,
        args.run_root,
        args.archive_root,
    )
    server = _ThreadingUnixStreamServer(str(args.socket), _RequestHandler)
    server.policy = policy
    os.chmod(args.socket, 0o600)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if args.socket.is_socket() and not args.socket.is_symlink():
            args.socket.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
