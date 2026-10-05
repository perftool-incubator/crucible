#!/usr/bin/env python3
"""Crucible SSH profile catalog and short-lived filtered agent sockets.

The catalog stores profile names, versions, and public-key fingerprints only.
The upstream agent remains the only holder of private key material.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CATALOG = Path("/var/lib/crucible/ssh-identities/profiles.json")
DEFAULT_KNOWN_HOSTS = Path("/var/lib/crucible/ssh-identities/known_hosts")
DEFAULT_AGENT_SOCKET = "/run/crucible/ssh-agent/agent.sock"
PROFILE_SOCKET_ROOT = Path("/run/crucible/ssh-profile-agents")
PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
MAX_AGENT_PACKET = 1024 * 1024
MAX_CATALOG_BYTES = 1024 * 1024
MAX_PROFILE_COUNT = 1000
SSH_ADD_TIMEOUT_SECONDS = 30


class SSHIdentityError(RuntimeError):
    """A profile is invalid, unavailable, or unsafe to use."""


def _pack_string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def _read_exact(stream: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = stream.recv(length - len(chunks))
        if not chunk:
            raise SSHIdentityError("SSH agent closed an incomplete response")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_packet(stream: socket.socket) -> bytes:
    length = struct.unpack(">I", _read_exact(stream, 4))[0]
    if length < 1 or length > MAX_AGENT_PACKET:
        raise SSHIdentityError("SSH agent packet length is invalid")
    return _read_exact(stream, length)


def _exchange_agent(socket_path: str, payload: bytes) -> bytes:
    if not socket_path or not os.path.isabs(socket_path):
        raise SSHIdentityError("SSH agent socket is not configured")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(3.0)
    try:
        client.connect(socket_path)
        client.sendall(struct.pack(">I", len(payload)) + payload)
        return _read_packet(client)
    except OSError as exc:
        raise SSHIdentityError("configured SSH agent is unavailable") from exc
    finally:
        client.close()


def _fingerprint(key_blob: bytes) -> str:
    digest = base64.b64encode(hashlib.sha256(key_blob).digest()).decode("ascii")
    return "SHA256:" + digest.rstrip("=")


def _identity_entries(socket_path: str) -> list[tuple[bytes, bytes]]:
    response = _exchange_agent(socket_path, b"\x0b")
    if not response or response[0] != 12 or len(response) < 5:
        raise SSHIdentityError("configured SSH agent returned an invalid identity list")
    count = struct.unpack(">I", response[1:5])[0]
    if count > 4096:
        raise SSHIdentityError("configured SSH agent returned too many identities")
    entries: list[tuple[bytes, bytes]] = []
    offset = 5
    for _ in range(count):
        values = []
        for _field in range(2):
            if offset + 4 > len(response):
                raise SSHIdentityError("configured SSH agent returned a truncated identity")
            length = struct.unpack(">I", response[offset:offset + 4])[0]
            offset += 4
            if length > MAX_AGENT_PACKET or offset + length > len(response):
                raise SSHIdentityError("configured SSH agent returned an invalid identity")
            values.append(response[offset:offset + length])
            offset += length
        entries.append((values[0], values[1]))
    if offset != len(response):
        raise SSHIdentityError("configured SSH agent returned trailing identity data")
    return entries


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not PROFILE_NAME.fullmatch(name):
        raise SSHIdentityError("profile name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return name


def run_profile_names(document: Any) -> set[str]:
    """Return the profile names selected by the supported endpoint contract."""

    if not isinstance(document, dict):
        return set()
    selected: set[str] = set()
    endpoints = document.get("endpoints", [])
    if not isinstance(endpoints, list):
        return selected
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        default = endpoint.get("ssh-identity-profile")
        if endpoint.get("type") != "remotehosts":
            if isinstance(default, str) and default:
                selected.add(_safe_name(default))
            continue
        remotes = endpoint.get("remotes", [])
        if not isinstance(remotes, list):
            remotes = []
        inherits_default = False
        for remote in remotes:
            config = remote.get("config") if isinstance(remote, dict) else None
            profile = config.get("ssh-identity-profile") if isinstance(config, dict) else None
            if isinstance(profile, str) and profile:
                selected.add(_safe_name(profile))
            else:
                inherits_default = True
        # Rickshaw copies the endpoint default to a remote only if that
        # remote has no profile override. Do not pin an unused endpoint
        # default when every configured remote selects its own profile.
        if inherits_default and isinstance(default, str) and default:
            selected.add(_safe_name(default))
    return selected


class SSHIdentityProfiles:
    """Read/write the installation-wide public metadata profile catalog."""

    def __init__(self, catalog_path: Path | str = DEFAULT_CATALOG, agent_socket: str | None = None):
        self.catalog_path = Path(catalog_path)
        self.agent_socket = agent_socket or ""
        self._import_lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            if self.catalog_path.stat().st_size > MAX_CATALOG_BYTES:
                raise SSHIdentityError("SSH identity catalog exceeds its size limit")
            value = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": 1, "profiles": {}}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SSHIdentityError("SSH identity catalog is unavailable or invalid") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or not isinstance(value.get("profiles"), dict)
            or len(value["profiles"]) > MAX_PROFILE_COUNT
        ):
            raise SSHIdentityError("SSH identity catalog has an unsupported format")
        return value

    def _locked_update(self, update) -> dict[str, Any]:
        self.catalog_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.catalog_path.parent, 0o700)
        lock_path = self.catalog_path.with_name(self.catalog_path.name + ".lock")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lock_fd = os.open(lock_path, flags, 0o600)
        try:
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            catalog = self._load()
            update(catalog)
            temporary = self.catalog_path.with_name(
                f".{self.catalog_path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
            )
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(catalog, output, sort_keys=True, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.catalog_path)
            os.chmod(self.catalog_path, 0o600)
            return catalog
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    @contextmanager
    def _agent_lifecycle_lock(self):
        """Serialize key imports with operations that clear the shared agent."""

        if not self.agent_socket:
            raise SSHIdentityError("Crucible's managed SSH agent is unavailable")
        socket_path = Path(self.agent_socket)
        lock_path = socket_path.with_name(socket_path.name + ".lifecycle.lock")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = -1
        try:
            socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(socket_path.parent, 0o700)
            lock_fd = os.open(lock_path, flags, 0o600)
            os.fchmod(lock_fd, 0o600)
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise SSHIdentityError("managed SSH-agent lock is not a regular file")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except OSError as exc:
            if lock_fd >= 0:
                os.close(lock_fd)
            raise SSHIdentityError("could not acquire the managed SSH-agent lock") from exc
        except BaseException:
            if lock_fd >= 0:
                os.close(lock_fd)
            raise
        try:
            yield
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def _entry(self, name: str, version: int | None = None) -> dict[str, Any]:
        name = _safe_name(name)
        profile = self._load()["profiles"].get(name)
        if not isinstance(profile, dict):
            raise SSHIdentityError(f"SSH identity profile '{name}' does not exist")
        if version is None:
            version = profile.get("current_version")
        versions = profile.get("versions")
        entry = versions.get(str(version)) if isinstance(versions, dict) else None
        if isinstance(version, bool) or not isinstance(version, int) or not isinstance(entry, dict):
            raise SSHIdentityError(f"SSH identity profile '{name}' has no version {version}")
        return {
            "name": name,
            "version": version,
            "fingerprint": entry.get("fingerprint"),
            "revoked": bool(entry.get("revoked")) or bool(profile.get("disabled")),
        }

    def snapshot(self, document: Any) -> dict[str, dict[str, Any]]:
        """Resolve names to immutable profile versions without requiring a live agent."""

        pins: dict[str, dict[str, Any]] = {}
        for name in sorted(run_profile_names(document)):
            entry = self._entry(name)
            if not isinstance(entry["fingerprint"], str) or not FINGERPRINT_RE.fullmatch(entry["fingerprint"]):
                raise SSHIdentityError(f"SSH identity profile '{name}' has invalid metadata")
            pins[name] = {
                "version": entry["version"],
                "fingerprint": entry["fingerprint"],
                "revoked": entry["revoked"],
            }
        return pins

    def validate_available(self, pins: dict[str, dict[str, Any]]) -> None:
        if not pins:
            return
        if not self.agent_socket:
            raise SSHIdentityError("SSH identity profiles are configured but Crucible's SSH agent is unavailable")
        entries = _identity_entries(self.agent_socket)
        available = {_fingerprint(key_blob) for key_blob, _comment in entries}
        for name, pin in pins.items():
            current = self._entry(name, pin.get("version"))
            if current["revoked"] or pin.get("revoked"):
                raise SSHIdentityError(f"SSH identity profile '{name}' is revoked")
            if current["fingerprint"] != pin.get("fingerprint"):
                raise SSHIdentityError(f"SSH identity profile '{name}' changed after it was selected")
            if current["fingerprint"] not in available:
                raise SSHIdentityError(f"SSH identity profile '{name}' is not loaded in Crucible's managed SSH agent")

    def list_profiles(self) -> list[dict[str, Any]]:
        catalog = self._load()
        try:
            available = {
                _fingerprint(key_blob)
                for key_blob, _comment in _identity_entries(self.agent_socket)
            } if self.agent_socket else set()
        except SSHIdentityError:
            available = set()
        results = []
        for name, profile in sorted(catalog["profiles"].items()):
            try:
                version = profile["current_version"]
                entry = self._entry(name, version)
                status = "revoked" if entry["revoked"] else (
                    "available" if entry["fingerprint"] in available else "unavailable"
                )
                results.append({"name": name, "version": version, "status": status})
            except (KeyError, SSHIdentityError):
                results.append({"name": name, "version": None, "status": "invalid"})
        return results

    def agent_keys(self) -> list[dict[str, str]]:
        if not self.agent_socket:
            raise SSHIdentityError("Crucible's managed SSH agent is unavailable")
        return [
            {"fingerprint": _fingerprint(blob), "comment": comment.decode("utf-8", errors="replace")}
            for blob, comment in _identity_entries(self.agent_socket)
        ]

    def import_key(
        self,
        name: str,
        key_path: Path | str,
        *,
        interactive: bool = True,
    ) -> dict[str, Any]:
        """Load a host key file into the managed agent and bind its identity."""

        with self._import_lock, self._agent_lifecycle_lock():
            return self._import_key_locked(name, key_path, interactive=interactive)

    def _import_key_locked(
        self,
        name: str,
        key_path: Path | str,
        *,
        interactive: bool,
    ) -> dict[str, Any]:

        name = _safe_name(name)
        key_path = Path(key_path)
        if not key_path.is_file():
            raise SSHIdentityError("the selected private-key file is unavailable")
        if not self.agent_socket:
            raise SSHIdentityError("Crucible's managed SSH agent is unavailable")

        before = {item["fingerprint"] for item in self.agent_keys()}
        environment = os.environ.copy()
        environment["SSH_AUTH_SOCK"] = self.agent_socket
        command_options: dict[str, Any] = {}
        if not interactive:
            # MCP requests have no trusted terminal for passphrase entry. Do not
            # inherit an askpass helper that could prompt in an unexpected UI.
            environment.pop("SSH_ASKPASS", None)
            environment.pop("SSH_ASKPASS_REQUIRE", None)
            environment.pop("DISPLAY", None)
            environment.pop("WAYLAND_DISPLAY", None)
            environment["SSH_ASKPASS_REQUIRE"] = "never"
            command_options = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "timeout": SSH_ADD_TIMEOUT_SECONDS,
            }
        try:
            result = subprocess.run(
                ["ssh-add", str(key_path)],
                env=environment,
                check=False,
                **command_options,
            )
        except subprocess.TimeoutExpired as exc:
            raise SSHIdentityError("SSH key import timed out") from exc
        except OSError as exc:
            raise SSHIdentityError("could not run ssh-add in the controller image") from exc
        if result.returncode != 0:
            if interactive:
                raise SSHIdentityError("ssh-add did not load the selected private key")
            raise SSHIdentityError(
                "the key could not be imported non-interactively; if it is "
                "passphrase-protected, use `crucible ssh profiles import` "
                "from a terminal to enter its passphrase"
            )

        loaded = self.agent_keys()
        added = [item["fingerprint"] for item in loaded if item["fingerprint"] not in before]
        if len(added) == 1:
            fingerprint = added[0]
        elif not added and len(loaded) == 1:
            # Re-importing the sole loaded identity is a harmless idempotent
            # operation and keeps profile setup convenient.
            fingerprint = loaded[0]["fingerprint"]
        else:
            raise SSHIdentityError(
                "the imported key could not be identified unambiguously; use "
                "profiles agent-keys and profiles add with its fingerprint"
            )
        try:
            return self.add(name, fingerprint)
        except OSError as exc:
            raise SSHIdentityError(
                "the key was loaded, but its profile could not be saved; "
                "use `crucible ssh profiles agent-keys` and `crucible ssh "
                "profiles add` to finish binding it"
            ) from exc

    def clear_agent(self) -> None:
        if not self.agent_socket:
            raise SSHIdentityError("Crucible's managed SSH agent is unavailable")
        environment = os.environ.copy()
        environment["SSH_AUTH_SOCK"] = self.agent_socket
        with self._agent_lifecycle_lock():
            try:
                result = subprocess.run(["ssh-add", "-D"], env=environment, check=False)
            except OSError as exc:
                raise SSHIdentityError("could not run ssh-add in the controller image") from exc
            if result.returncode != 0:
                raise SSHIdentityError("ssh-add could not clear the managed agent")

    def add(self, name: str, fingerprint: str) -> dict[str, Any]:
        name = _safe_name(name)
        if not isinstance(fingerprint, str) or not FINGERPRINT_RE.fullmatch(fingerprint):
            raise SSHIdentityError("a valid SHA256 public-key fingerprint is required")
        if fingerprint not in {item["fingerprint"] for item in self.agent_keys()}:
            raise SSHIdentityError("the selected fingerprint is not present in Crucible's managed SSH agent")
        created_at = datetime.now(timezone.utc).isoformat()

        def update(catalog):
            profiles = catalog["profiles"]
            if name not in profiles and len(profiles) >= MAX_PROFILE_COUNT:
                raise SSHIdentityError("SSH identity catalog has reached its profile limit")
            profile = profiles.setdefault(name, {"current_version": 0, "versions": {}, "disabled": False})
            current = profile.get("versions", {}).get(str(profile.get("current_version")), {})
            if current.get("fingerprint") == fingerprint and not current.get("revoked"):
                return
            version = max([int(item) for item in profile.get("versions", {}) if str(item).isdigit()] + [0]) + 1
            profile.setdefault("versions", {})[str(version)] = {
                "fingerprint": fingerprint,
                "created_at": created_at,
                "revoked": False,
            }
            profile["current_version"] = version
            profile["disabled"] = False

        catalog = self._locked_update(update)
        profile = catalog["profiles"][name]
        return {"name": name, "version": profile["current_version"]}

    def remove(self, name: str) -> None:
        name = _safe_name(name)

        def update(catalog):
            profile = catalog["profiles"].get(name)
            if not isinstance(profile, dict):
                raise SSHIdentityError(f"SSH identity profile '{name}' does not exist")
            versions = profile.get("versions")
            if not isinstance(versions, dict) or not versions:
                raise SSHIdentityError(f"SSH identity profile '{name}' is invalid")
            for entry in versions.values():
                if isinstance(entry, dict):
                    entry["revoked"] = True
            profile["disabled"] = True

        self._locked_update(update)

    def version_is_usable(self, name: str, version: int, fingerprint: str) -> bool:
        try:
            entry = self._entry(name, version)
        except SSHIdentityError:
            return False
        return not entry["revoked"] and entry["fingerprint"] == fingerprint


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True, separators=(",", ":"))
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


@contextmanager
def _lock_known_hosts(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_name(path.name + ".lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _replace_known_hosts(path: Path, lines: list[str]) -> None:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write("\n".join(lines) + ("\n" if lines else ""))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(
            path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _agent_failure() -> bytes:
    return b"\x05"


def _parse_sign_key(payload: bytes) -> bytes | None:
    if not payload or payload[0] != 13:
        return None
    offset = 1
    if offset + 4 > len(payload):
        return None
    length = struct.unpack(">I", payload[offset:offset + 4])[0]
    offset += 4
    if length > MAX_AGENT_PACKET or offset + length > len(payload):
        return None
    key_blob = payload[offset:offset + length]
    offset += length
    if offset + 4 > len(payload):
        return None
    data_length = struct.unpack(">I", payload[offset:offset + 4])[0]
    offset += 4
    if data_length > MAX_AGENT_PACKET or offset + data_length + 4 != len(payload):
        return None
    return key_blob


class _AgentProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            try:
                payload = _read_packet(self.request)
                response = self.server.process(payload)
                self.request.sendall(struct.pack(">I", len(response)) + response)
            except (OSError, SSHIdentityError):
                return


class _ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class _ProfileProxyServer(_ThreadedUnixServer):
    def __init__(self, socket_path: str, manager: SSHIdentityProfiles, profile: dict[str, Any]):
        self.manager = manager
        self.profile = profile
        super().__init__(socket_path, _AgentProxyHandler)

    def process(self, payload: bytes) -> bytes:
        name = self.profile["name"]
        version = self.profile["version"]
        fingerprint = self.profile["fingerprint"]
        if not self.manager.version_is_usable(name, version, fingerprint):
            return _agent_failure()
        try:
            if payload == b"\x0b":
                entries = _identity_entries(self.manager.agent_socket)
                matching = [
                    (blob, comment)
                    for blob, comment in entries
                    if _fingerprint(blob) == fingerprint
                ]
                if not matching:
                    return _agent_failure()
                blob, comment = matching[0]
                return b"\x0c" + struct.pack(">I", 1) + _pack_string(blob) + _pack_string(comment)
            key_blob = _parse_sign_key(payload)
            if key_blob is None or _fingerprint(key_blob) != fingerprint:
                return _agent_failure()
            response = _exchange_agent(self.manager.agent_socket, payload)
            return response if response and response[0] == 14 else _agent_failure()
        except SSHIdentityError:
            return _agent_failure()


def _run_proxy(args) -> None:
    manager = SSHIdentityProfiles(args.catalog, args.agent_socket)
    profiles = json.loads(args.profiles_json)
    if not isinstance(profiles, dict):
        raise SSHIdentityError("proxy profile snapshot is invalid")
    servers = []
    mapping = {}
    for name, pin in sorted(profiles.items()):
        name = _safe_name(name)
        profile = {
            "name": name,
            "version": pin.get("version"),
            "fingerprint": pin.get("fingerprint"),
        }
        if not isinstance(profile["version"], int) or not FINGERPRINT_RE.fullmatch(str(profile["fingerprint"])):
            raise SSHIdentityError("proxy profile pin is invalid")
        socket_name = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12] + ".sock"
        socket_path = Path(args.socket_dir) / socket_name
        server = _ProfileProxyServer(str(socket_path), manager, profile)
        os.chmod(socket_path, 0o600)
        servers.append(server)
        mapping[name] = str(socket_path)
    stopping = threading.Event()

    def stop(_signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        for server in servers:
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threads = []
    for server in servers:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
    _atomic_json(Path(args.map_file), mapping)
    _atomic_json(Path(args.ready_file), {"ready": True, "run_token": args.run_token})
    if not servers:
        # A profile-less run uses ambient SSH and needs no proxy child.
        return
    try:
        while not stopping.wait(0.25):
            pass
    finally:
        for server in servers:
            server.server_close()
            try:
                Path(server.server_address).unlink()
            except FileNotFoundError:
                pass


def _read_profile_lock(path: Path | None, manager: SSHIdentityProfiles, document: Any) -> dict[str, dict[str, Any]]:
    if path is None:
        pins = manager.snapshot(document)
    else:
        try:
            if path.stat().st_size > 65536:
                raise SSHIdentityError("job SSH identity lock exceeds its size limit")
            lock_data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SSHIdentityError("job SSH identity lock is unavailable or invalid") from exc
        pins = lock_data.get("profiles") if isinstance(lock_data, dict) else None
        if not isinstance(pins, dict) or set(pins) != run_profile_names(document):
            raise SSHIdentityError("job SSH identity lock does not match the run file")
        for name, pin in pins.items():
            _safe_name(name)
            if not isinstance(pin, dict) or not isinstance(pin.get("version"), int):
                raise SSHIdentityError("job SSH identity lock contains an invalid profile")
            entry = manager._entry(name, pin["version"])
            if entry["fingerprint"] != pin.get("fingerprint") or entry["revoked"]:
                raise SSHIdentityError(f"SSH identity profile '{name}' is no longer usable")
    manager.validate_available(pins)
    return pins


def prepare_run(args) -> dict[str, Any]:
    run_file = Path(args.run_file)
    try:
        run_file_bytes = run_file.read_bytes()
        document = json.loads(run_file_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SSHIdentityError("run file is unavailable or invalid") from exc

    manager = SSHIdentityProfiles(args.catalog, args.agent_socket)
    selected_profiles = run_profile_names(document)
    # Direct CLI runs without identity profiles retain Rickshaw's existing
    # input-size behavior; the tighter bound is needed only when profile
    # metadata or an MCP-provided identity lock must be processed.
    if len(run_file_bytes) > 1_048_576 and (
        selected_profiles or args.profile_lock is not None
    ):
        raise SSHIdentityError("run file exceeds its size limit")
    pins = _read_profile_lock(Path(args.profile_lock) if args.profile_lock else None, manager, document)
    run_directory = Path(args.run_directory)
    config_directory = run_directory / "config"
    config_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(config_directory, 0o700)
    known_hosts = Path(args.known_hosts)
    known_hosts.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(known_hosts.parent, 0o700)
    known_hosts_fd = os.open(known_hosts, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(known_hosts_fd).st_mode):
            raise SSHIdentityError("managed SSH known-hosts path is not a regular file")
        os.fchmod(known_hosts_fd, 0o600)
    finally:
        os.close(known_hosts_fd)
    if not pins:
        map_path = config_directory / "ssh-profile-sockets.json"
        _atomic_json(map_path, {})
        return {
            "map_file": str(map_path),
            "state_file": "",
            "socket_dir": "",
            "known_hosts": str(known_hosts),
        }

    socket_root = PROFILE_SOCKET_ROOT
    socket_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(socket_root, 0o700)
    socket_dir = Path(tempfile.mkdtemp(prefix="p-", dir=socket_root))
    os.chmod(socket_dir, 0o700)
    map_path = socket_dir / "profile-sockets.json"
    state_path = socket_dir / "state.json"
    run_token = secrets.token_hex(16)
    ready_path = socket_dir / "ready.json"
    with open(socket_dir / "proxy.log", "ab") as output:
        os.chmod(socket_dir / "proxy.log", 0o600)
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "proxy",
            "--catalog", str(manager.catalog_path),
            "--agent-socket", manager.agent_socket,
            "--profiles-json", json.dumps(pins, separators=(",", ":")),
            "--socket-dir", str(socket_dir),
            "--map-file", str(map_path),
            "--ready-file", str(ready_path),
            "--run-token=" + run_token,
        ]
        try:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
                close_fds=True,
            )
        except BaseException:
            (socket_dir / "proxy.log").unlink(missing_ok=True)
            socket_dir.rmdir()
            raise
    state = {
        "pid": child.pid,
        "run_token": run_token,
        "socket_dir": str(socket_dir),
        "map_file": str(map_path),
        "ready_file": str(ready_path),
        "profiles": pins,
    }
    try:
        _atomic_json(state_path, state)
    except BaseException:
        try:
            child.terminate()
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        for entry in socket_dir.iterdir():
            entry.unlink()
        socket_dir.rmdir()
        raise
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if child.poll() is not None:
            stop_run(args=argparse.Namespace(state_file=str(state_path)))
            raise SSHIdentityError("filtered SSH agent proxy failed to start")
        if ready_path.exists():
            try:
                ready = json.loads(ready_path.read_text(encoding="utf-8"))
                if ready.get("ready") and ready.get("run_token") == run_token:
                    sockets = list(json.loads(map_path.read_text(encoding="utf-8")).values())
                    ready_path.unlink(missing_ok=True)
                    return {
                        "map_file": str(map_path),
                        "state_file": str(state_path),
                        "socket_dir": str(socket_dir),
                        "sockets": sockets,
                        "known_hosts": str(known_hosts),
                    }
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        time.sleep(0.05)
    stop_run(args=argparse.Namespace(state_file=str(state_path)))
    raise SSHIdentityError("timed out starting filtered SSH agent proxy")


def stop_run(args) -> None:
    state_path = Path(args.state_file)
    if not state_path.exists():
        return
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SSHIdentityError("SSH agent proxy state is invalid; refusing cleanup")
    pid = state.get("pid")
    token = state.get("run_token")
    if isinstance(pid, bool) or not isinstance(pid, int) or not isinstance(token, str):
        raise SSHIdentityError("SSH agent proxy state is invalid; refusing cleanup")
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    try:
        cmdline = cmdline_path.read_bytes().split(b"\0")
    except FileNotFoundError:
        cmdline = []
    expected = ("--run-token=" + token).encode()
    if cmdline and expected in cmdline:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
            try:
                current = cmdline_path.read_bytes().split(b"\0")
            except FileNotFoundError:
                break
            if expected not in current:
                break
            time.sleep(0.05)
    for field in ("map_file", "ready_file"):
        path = state.get(field)
        if isinstance(path, str):
            Path(path).unlink(missing_ok=True)
    socket_dir = state.get("socket_dir")
    if isinstance(socket_dir, str):
        directory = Path(socket_dir)
        if directory.is_dir() and directory.parent == PROFILE_SOCKET_ROOT:
            for entry in directory.iterdir():
                if entry.is_socket() or entry.is_file():
                    entry.unlink()
            directory.rmdir()
    state_path.unlink(missing_ok=True)


def _hide_unfiltered_agent_socket(agent_socket: str) -> None:
    """Mask the upstream agent in the workload child, not its proxy parent."""

    if Path(agent_socket) != Path(DEFAULT_AGENT_SOCKET):
        raise OSError("run isolation requires Crucible's standard managed agent socket")
    agent_directory = Path(agent_socket).parent
    targets = [agent_directory]
    if agent_directory.is_absolute():
        targets.append(Path("/hostfs") / agent_directory.relative_to("/"))

    existing_targets = []
    for target in targets:
        try:
            target_info = target.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(target_info.st_mode):
            raise OSError(f"managed SSH-agent directory is not a directory: {target}")
        existing_targets.append(os.fsencode(target))
    if not existing_targets:
        # If the managed socket directory is absent, no upstream socket was
        # mounted into this run container and there is nothing to mask.
        return

    libc = ctypes.CDLL(None, use_errno=True)
    clone_newns = 0x00020000
    mount_recursive = 0x4000
    mount_private = 0x40000
    mount_nosuid = 0x2
    mount_nodev = 0x4
    mount_noexec = 0x8
    if libc.unshare(clone_newns) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), "unshare mount namespace")
    if libc.mount(None, b"/", None, mount_recursive | mount_private, None) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), "make mount namespace private")
    for target in existing_targets:
        if libc.mount(
            b"tmpfs",
            target,
            b"tmpfs",
            mount_nosuid | mount_nodev | mount_noexec,
            b"mode=700,size=4096",
        ) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), os.fsdecode(target))


def run_with_profiles(args) -> int:
    """Run a controller command with run-scoped SSH profile sockets.

    The setup process can reach the upstream agent to create filtered proxies;
    the workload child enters a private mount namespace where the unfiltered
    socket paths are masked before Rickshaw or benchmark code starts.
    """

    command = list(args.run_command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise SSHIdentityError("a command is required")

    prepared = None
    child = None
    forwarded_signals: list[int] = []
    previous_handlers = {}

    def forward_signal(signum, _frame):
        forwarded_signals.append(signum)
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous_handlers[signum] = signal.signal(signum, forward_signal)

    cleanup_failed = False
    result = 1
    try:
        try:
            prepared = prepare_run(args)
        except SSHIdentityError as exc:
            # Let Rickshaw retain its existing JSON syntax diagnostics. Invalid
            # JSON cannot select an SSH profile, so it is safe to delegate it
            # without constructing run-scoped profile state.
            if (
                str(exc) != "run file is unavailable or invalid"
                or not isinstance(exc.__cause__, json.JSONDecodeError)
            ):
                raise
            prepared = None
            environment = os.environ.copy()
        else:
            environment = os.environ.copy()
            environment["CRUCIBLE_SSH_PROFILE_SOCKETS_FILE"] = prepared["map_file"]
            environment["CRUCIBLE_SSH_KNOWN_HOSTS_FILE"] = prepared["known_hosts"]
            if prepared["socket_dir"]:
                environment["CRUCIBLE_SSH_PROFILE_ACTIVE"] = "1"
            else:
                environment.pop("CRUCIBLE_SSH_PROFILE_ACTIVE", None)

        if forwarded_signals:
            result = 128 + forwarded_signals[-1]
        else:
            agent_socket = getattr(args, "agent_socket", DEFAULT_AGENT_SOCKET)
            try:
                child = subprocess.Popen(
                    command,
                    env=environment,
                    preexec_fn=lambda: _hide_unfiltered_agent_socket(agent_socket),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise SSHIdentityError(
                    "could not hide the unfiltered SSH agent from the run command"
                ) from exc
            if forwarded_signals and child.poll() is None:
                child.send_signal(forwarded_signals[-1])
            result = child.wait()
            if forwarded_signals:
                result = 128 + forwarded_signals[-1]
            elif result < 0:
                result = 128 + -result
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if child is not None and child.poll() is None:
            try:
                child.terminate()
            except ProcessLookupError:
                pass
            if child.poll() is None:
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
                    child.wait()
        if prepared and prepared["state_file"]:
            try:
                stop_run(argparse.Namespace(state_file=prepared["state_file"]))
            except (SSHIdentityError, OSError) as exc:
                cleanup_failed = True
                print(
                    f"WARNING: Could not stop the run-scoped SSH identity proxy: {exc}",
                    file=sys.stderr,
                )
        if cleanup_failed and result == 0:
            result = 1
    return result


def _print_profiles(args) -> None:
    manager = SSHIdentityProfiles(args.catalog, args.agent_socket)
    rows = manager.list_profiles()
    if not rows:
        print("No SSH identity profiles are configured.")
        return
    print("NAME\tVERSION\tSTATUS")
    for row in rows:
        print(f"{row['name']}\t{row['version']}\t{row['status']}")


def _print_agent_keys(args) -> None:
    manager = SSHIdentityProfiles(args.catalog, args.agent_socket)
    keys = manager.agent_keys()
    if not keys:
        print("The configured SSH agent has no identities loaded.")
        return
    print("FINGERPRINT\tCOMMENT")
    for key in keys:
        print(f"{key['fingerprint']}\t{key['comment']}")


def _known_hosts_rows(path: Path) -> list[tuple[str, str]]:
    rows = []
    if not path.exists():
        return rows
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise SSHIdentityError("known-hosts file exceeds its size limit")
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SSHIdentityError("managed known-hosts file is unavailable") from exc
    for line in lines:
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            blob = base64.b64decode(fields[2], validate=True)
        except (ValueError, binascii.Error):
            continue
        rows.append((fields[0], _fingerprint(blob)))
    return rows


def _known_hosts_action(args) -> None:
    path = Path(args.known_hosts)
    if args.known_action == "list":
        print("HOSTS\tFINGERPRINT")
        for hosts, fingerprint in _known_hosts_rows(path):
            print(f"{hosts}\t{fingerprint}")
        return
    if args.known_action == "forget":
        if not args.host:
            raise SSHIdentityError("known-hosts forget requires a host")
        with _lock_known_hosts(path):
            try:
                lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            except (OSError, UnicodeDecodeError) as exc:
                raise SSHIdentityError("managed known-hosts file is unavailable") from exc
            kept = []
            for line in lines:
                fields = line.split()
                hosts = fields[0].split(",") if fields else []
                if args.host not in hosts:
                    kept.append(line)
            _replace_known_hosts(path, kept)
        print(f"Forgot managed SSH host key for {args.host}.")
        return
    if args.known_action == "reset":
        if input("Type RESET to remove every Crucible-managed host-key pin: ") != "RESET":
            raise SSHIdentityError("known-hosts reset cancelled")
        with _lock_known_hosts(path):
            _replace_known_hosts(path, [])
        print("Cleared Crucible-managed SSH host-key pins.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage Crucible SSH identity profiles")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--known-hosts", type=Path, default=DEFAULT_KNOWN_HOSTS)
    parser.add_argument("--agent-socket", default=DEFAULT_AGENT_SOCKET)
    commands = parser.add_subparsers(dest="command", required=True)
    profile_parser = commands.add_parser("profiles")
    profile_commands = profile_parser.add_subparsers(dest="profile_action", required=True)
    profile_commands.add_parser("list")
    profile_commands.add_parser("agent-keys")
    add_parser = profile_commands.add_parser("add")
    add_parser.add_argument("name")
    add_parser.add_argument("fingerprint")
    import_parser = profile_commands.add_parser("import")
    import_parser.add_argument("name")
    import_parser.add_argument("private_key", type=Path)
    remove_parser = profile_commands.add_parser("remove")
    remove_parser.add_argument("name")
    known_parser = commands.add_parser("known-hosts")
    known_parser.add_argument("known_action", choices=("list", "forget", "reset"))
    known_parser.add_argument("host", nargs="?")
    agent_parser = commands.add_parser("agent")
    agent_commands = agent_parser.add_subparsers(dest="agent_action", required=True)
    agent_commands.add_parser("clear")
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--run-file", required=True)
    prepare_parser.add_argument("--run-directory", required=True)
    prepare_parser.add_argument("--profile-lock")
    run_parser = commands.add_parser("run", help=argparse.SUPPRESS)
    run_parser.add_argument("--run-file", required=True)
    run_parser.add_argument("--run-directory", required=True)
    run_parser.add_argument("--profile-lock")
    run_parser.add_argument("run_command", nargs=argparse.REMAINDER)
    commands.add_parser("stop", help=argparse.SUPPRESS).add_argument("--state-file", required=True)
    proxy_parser = commands.add_parser("proxy", help=argparse.SUPPRESS)
    proxy_parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    proxy_parser.add_argument("--agent-socket", default=DEFAULT_AGENT_SOCKET)
    proxy_parser.add_argument("--profiles-json", required=True)
    proxy_parser.add_argument("--socket-dir", required=True)
    proxy_parser.add_argument("--map-file", required=True)
    proxy_parser.add_argument("--ready-file", required=True)
    proxy_parser.add_argument("--run-token", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "profiles":
            manager = SSHIdentityProfiles(args.catalog, args.agent_socket)
            if args.profile_action == "list":
                _print_profiles(args)
            elif args.profile_action == "agent-keys":
                _print_agent_keys(args)
            elif args.profile_action == "add":
                created = manager.add(args.name, args.fingerprint)
                print(f"Added SSH identity profile {created['name']} version {created['version']}.")
            elif args.profile_action == "import":
                created = manager.import_key(args.name, args.private_key)
                print(f"Imported key and added SSH identity profile {created['name']} version {created['version']}.")
            elif args.profile_action == "remove":
                manager.remove(args.name)
                print(f"Revoked SSH identity profile {args.name}.")
        elif args.command == "agent":
            manager = SSHIdentityProfiles(args.catalog, args.agent_socket)
            if args.agent_action == "clear":
                manager.clear_agent()
                print("Cleared all identities from Crucible's managed SSH agent.")
        elif args.command == "known-hosts":
            _known_hosts_action(args)
        elif args.command == "prepare":
            result = prepare_run(args)
            print(json.dumps(result, sort_keys=True))
        elif args.command == "run":
            return run_with_profiles(args)
        elif args.command == "stop":
            stop_run(args)
        elif args.command == "proxy":
            _run_proxy(args)
        return 0
    except (SSHIdentityError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
