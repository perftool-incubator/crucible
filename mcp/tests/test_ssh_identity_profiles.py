import json
import os
import struct
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, call, patch

from ssh_identity_profiles import (
    SSHIdentityError,
    SSHIdentityProfiles,
    _ProfileProxyServer,
    _atomic_json,
    _fingerprint,
    _hide_unfiltered_agent_socket,
    _pack_string,
    main,
    prepare_run,
    run_profile_names,
    run_with_profiles,
    stop_run,
)


class TestSSHIdentityProfiles(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.catalog = Path(self.directory.name) / "profiles.json"
        self.key = b"test-agent-public-key"
        self.fingerprint = _fingerprint(self.key)
        self.manager = SSHIdentityProfiles(
            self.catalog, str(Path(self.directory.name) / "agent.sock")
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_profile_selectors_include_endpoint_and_remote_overrides(self):
        document = {
            "endpoints": [
                {
                    "type": "kube",
                    "ssh-identity-profile": "cluster-admin",
                },
                {
                    "type": "remotehosts",
                    "ssh-identity-profile": "default-admin",
                    "remotes": [
                        {"config": {"ssh-identity-profile": "build-host"}},
                        {"config": {"host": "fallback.example"}},
                    ],
                },
                {"type": "osp", "remotes": [{"config": {"ssh-identity-profile": "ignored"}}]},
            ]
        }

        self.assertEqual(
            run_profile_names(document),
            {"cluster-admin", "default-admin", "build-host"},
        )

    def test_remotehosts_default_is_unused_when_every_remote_overrides_it(self):
        document = {
            "endpoints": [
                {
                    "type": "remotehosts",
                    "ssh-identity-profile": "unused-default",
                    "remotes": [
                        {"config": {"ssh-identity-profile": "build-host"}},
                        {"config": {"ssh-identity-profile": "test-host"}},
                    ],
                }
            ]
        }

        self.assertEqual(run_profile_names(document), {"build-host", "test-host"})

    def test_catalog_snapshot_pins_version_and_list_omits_fingerprint(self):
        with patch.object(
            self.manager, "agent_keys", return_value=[{"fingerprint": self.fingerprint}]
        ):
            created = self.manager.add("performance", self.fingerprint)

        pins = self.manager.snapshot(
            {"endpoints": [{"type": "osp", "ssh-identity-profile": "performance"}]}
        )
        self.assertEqual(created["version"], 1)
        self.assertEqual(pins["performance"]["version"], 1)
        self.assertEqual(pins["performance"]["fingerprint"], self.fingerprint)

        with patch(
            "ssh_identity_profiles._identity_entries",
            return_value=[(self.key, b"test key")],
        ):
            listed = self.manager.list_profiles()
        self.assertEqual(listed, [{"name": "performance", "version": 1, "status": "available"}])
        self.assertNotIn("fingerprint", json.dumps(listed))

    def test_proxy_exposes_and_signs_only_the_selected_identity(self):
        other_key = b"other-agent-public-key"
        proxy = _ProfileProxyServer.__new__(_ProfileProxyServer)
        proxy.manager = self.manager
        proxy.profile = {
            "name": "performance",
            "version": 1,
            "fingerprint": self.fingerprint,
        }

        with (
            patch.object(self.manager, "version_is_usable", return_value=True),
            patch(
                "ssh_identity_profiles._identity_entries",
                return_value=[(other_key, b"other"), (self.key, b"selected")],
            ),
        ):
            identities = proxy.process(b"\x0b")
        self.assertEqual(identities, b"\x0c" + struct.pack(">I", 1) + _pack_string(self.key) + _pack_string(b"selected"))

        selected_request = (
            b"\x0d"
            + _pack_string(self.key)
            + _pack_string(b"payload")
            + struct.pack(">I", 0)
        )
        rejected_request = (
            b"\x0d"
            + _pack_string(other_key)
            + _pack_string(b"payload")
            + struct.pack(">I", 0)
        )
        with (
            patch.object(self.manager, "version_is_usable", return_value=True),
            patch("ssh_identity_profiles._exchange_agent", return_value=b"\x0e\x00\x00\x00\x00") as exchange,
        ):
            self.assertEqual(proxy.process(selected_request), b"\x0e\x00\x00\x00\x00")
            self.assertEqual(proxy.process(rejected_request), b"\x05")
        exchange.assert_called_once_with(self.manager.agent_socket, selected_request)

    def test_revocation_disables_every_version(self):
        with patch.object(
            self.manager, "agent_keys", return_value=[{"fingerprint": self.fingerprint}]
        ):
            self.manager.add("performance", self.fingerprint)

        self.manager.remove("performance")
        pins = self.manager.snapshot(
            {"endpoints": [{"type": "osp", "ssh-identity-profile": "performance"}]}
        )
        self.assertTrue(pins["performance"]["revoked"])
        self.assertFalse(
            self.manager.version_is_usable(
                "performance", pins["performance"]["version"], self.fingerprint
            )
        )

    def test_import_key_loads_identity_and_creates_profile(self):
        private_key = Path(self.directory.name) / "id_test"
        private_key.write_text("fixture", encoding="utf-8")
        key_row = {"fingerprint": self.fingerprint, "comment": "fixture"}
        with (
            patch.object(self.manager, "agent_keys", side_effect=[[], [key_row], [key_row]]),
            patch("ssh_identity_profiles.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run,
        ):
            created = self.manager.import_key("lab-admin", private_key)

        self.assertEqual(created, {"name": "lab-admin", "version": 1})
        self.assertEqual(run.call_args.args[0], ["ssh-add", str(private_key)])
        self.assertEqual(run.call_args.kwargs["env"]["SSH_AUTH_SOCK"], self.manager.agent_socket)
        self.assertEqual(self.manager.snapshot({"endpoints": [{"type": "osp", "ssh-identity-profile": "lab-admin"}]})["lab-admin"]["fingerprint"], self.fingerprint)

    def test_noninteractive_import_disables_prompts_and_bounds_ssh_add(self):
        private_key = Path(self.directory.name) / "id_test"
        private_key.write_text("fixture", encoding="utf-8")
        key_row = {"fingerprint": self.fingerprint, "comment": "fixture"}
        with (
            patch.object(self.manager, "agent_keys", side_effect=[[], [key_row], [key_row]]),
            patch.dict(
                "ssh_identity_profiles.os.environ",
                {
                    "SSH_ASKPASS": "/tmp/untrusted-askpass",
                    "SSH_ASKPASS_REQUIRE": "force",
                    "DISPLAY": ":0",
                    "WAYLAND_DISPLAY": "wayland-0",
                },
                clear=True,
            ),
            patch(
                "ssh_identity_profiles.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as run,
        ):
            self.manager.import_key("lab-admin", private_key, interactive=False)

        options = run.call_args.kwargs
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        self.assertIs(options["stdout"], subprocess.DEVNULL)
        self.assertIs(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(options["timeout"], 30)
        self.assertNotIn("SSH_ASKPASS", options["env"])
        self.assertEqual(options["env"]["SSH_ASKPASS_REQUIRE"], "never")
        self.assertNotIn("DISPLAY", options["env"])
        self.assertNotIn("WAYLAND_DISPLAY", options["env"])

    def test_noninteractive_import_explains_cli_fallback_for_encrypted_key(self):
        private_key = Path(self.directory.name) / "id_test"
        private_key.write_text("fixture", encoding="utf-8")
        with (
            patch.object(self.manager, "agent_keys", return_value=[]),
            patch(
                "ssh_identity_profiles.subprocess.run",
                return_value=SimpleNamespace(returncode=1),
            ),
        ):
            with self.assertRaisesRegex(SSHIdentityError, "passphrase-protected"):
                self.manager.import_key("lab-admin", private_key, interactive=False)

    def test_clear_agent_uses_managed_socket(self):
        with patch(
            "ssh_identity_profiles.subprocess.run",
            return_value=SimpleNamespace(returncode=0),
        ) as run:
            self.manager.clear_agent()

        self.assertEqual(run.call_args.args[0], ["ssh-add", "-D"])
        self.assertEqual(run.call_args.kwargs["env"]["SSH_AUTH_SOCK"], self.manager.agent_socket)

    def test_agent_lifecycle_lock_uses_catalog_storage_not_socket_directory(self):
        socket_directory = Path(self.directory.name) / "read-only-agent"
        manager = SSHIdentityProfiles(
            self.catalog, str(socket_directory / "agent.sock")
        )

        with manager._agent_lifecycle_lock():
            pass

        self.assertFalse(socket_directory.exists())
        self.assertTrue((self.catalog.parent / "agent.lifecycle.lock").is_file())

    def test_agent_clear_waits_for_import_catalog_commit(self):
        private_key = Path(self.directory.name) / "id_test"
        private_key.write_text("fixture", encoding="utf-8")
        agent_socket = str(Path(self.directory.name) / "agent.sock")
        importing = SSHIdentityProfiles(self.catalog, agent_socket)
        clearing = SSHIdentityProfiles(self.catalog, agent_socket)
        key_row = {"fingerprint": self.fingerprint, "comment": "fixture"}
        importing.agent_keys = Mock(side_effect=[[], [key_row], [key_row]])

        update_started = threading.Event()
        allow_catalog_update = threading.Event()
        clear_started = threading.Event()
        errors = []
        original_locked_update = importing._locked_update

        def blocked_catalog_update(update):
            update_started.set()
            if not allow_catalog_update.wait(timeout=5):
                raise TimeoutError("test did not release the profile catalog update")
            return original_locked_update(update)

        importing._locked_update = blocked_catalog_update

        def fake_subprocess_run(command, **_kwargs):
            if command == ["ssh-add", "-D"]:
                clear_started.set()
            return SimpleNamespace(returncode=0)

        def import_key():
            try:
                importing.import_key("lab-admin", private_key, interactive=False)
            except BaseException as exc:  # preserve worker errors for the assertion thread
                errors.append(exc)

        def clear_agent():
            try:
                clearing.clear_agent()
            except BaseException as exc:  # preserve worker errors for the assertion thread
                errors.append(exc)

        with patch("ssh_identity_profiles.subprocess.run", side_effect=fake_subprocess_run):
            importer = threading.Thread(target=import_key)
            importer.start()
            self.assertTrue(update_started.wait(timeout=5))

            clearer = threading.Thread(target=clear_agent)
            clearer.start()
            self.assertFalse(clear_started.wait(timeout=0.1))

            allow_catalog_update.set()
            importer.join(timeout=5)
            clearer.join(timeout=5)

        self.assertFalse(importer.is_alive())
        self.assertFalse(clearer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(clear_started.is_set())
        self.assertEqual(
            importing._load()["profiles"]["lab-admin"]["current_version"], 1
        )

    def test_forget_creates_known_hosts_directory_before_first_run(self):
        known_hosts = Path(self.directory.name) / "new" / "known_hosts"
        self.assertEqual(
            main(
                [
                    "--catalog",
                    str(self.catalog),
                    "--known-hosts",
                    str(known_hosts),
                    "known-hosts",
                    "forget",
                    "first-use.example",
                ]
            ),
            0,
        )
        self.assertTrue(known_hosts.is_file())
        self.assertEqual(known_hosts.read_text(encoding="utf-8"), "")
        self.assertEqual(known_hosts.stat().st_mode & 0o777, 0o600)

    def test_known_hosts_reset_is_routed_through_interactive_container(self):
        script = r'''
source <(awk '
    /^function ssh_identity_command_requires_tty\(\)/ { capture=1 }
    capture { print }
    capture && /^}/ { exit }
' "$ROOT/bin/crucible")
ssh_identity_command_requires_tty known-hosts reset 2 || exit 1
if ssh_identity_command_requires_tty known-hosts list 2; then exit 2; fi
if ssh_identity_command_requires_tty known-hosts reset 3; then exit 3; fi
'''
        repository = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=repository,
            env={**os.environ, "ROOT": str(repository)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_profile_command_holds_lifecycle_lock_for_container_operation(self):
        script = r'''
source <(awk '
    /^function run_ssh_identity_command\(\)/ { capture=1 }
    capture { print }
    capture && /^}/ { exit }
' "$ROOT/bin/crucible")
CRUCIBLE_MCP_LIFECYCLE_LOCK_FD=
LOCK_RELEASES=0
mcp_lifecycle_lock_acquire() { CRUCIBLE_MCP_LIFECYCLE_LOCK_FD=9; }
mcp_lifecycle_lock_release() {
    unset CRUCIBLE_MCP_LIFECYCLE_LOCK_FD
    LOCK_RELEASES=$((LOCK_RELEASES + 1))
}
start_ssh_agent() {
    [ "${CRUCIBLE_MCP_LIFECYCLE_LOCK_FD:-}" = "9" ]
}
fake_podman_run() {
    [ "${CRUCIBLE_MCP_LIFECYCLE_LOCK_FD:-}" = "9" ] || return 21
    return 7
}
podman_run=fake_podman_run
podman_run_interactive=fake_podman_run
ssh_identity_interactive=0
ssh_identity_args=(python3 ssh_identity_profiles.py)
ssh_identity_cli_args=(profiles add profile fingerprint)
container_common_args=()
container_non_service_args=()
CRUCIBLE_CONTROLLER_IMAGE=test-controller
SESSION_ID=test-session
run_ssh_identity_command profiles add
result=$?
[ "$result" -eq 7 ] || exit 1
[ "$LOCK_RELEASES" -eq 1 ] || exit 2
[ -z "${CRUCIBLE_MCP_LIFECYCLE_LOCK_FD:-}" ] || exit 3
'''
        repository = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=repository,
            env={**os.environ, "ROOT": str(repository)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cli_key_import_mounts_only_a_temporary_readonly_key_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_key = Path(temporary) / "source-key"
            source_key.write_text("private-key-material", encoding="utf-8")
            script = r'''
source <(awk '
    /^function run_ssh_identity_command\(\)/ { capture=1 }
    capture { print }
    capture && /^}/ { exit }
' "$ROOT/bin/crucible")
CRUCIBLE_MCP_LIFECYCLE_LOCK_FD=
LOCK_RELEASES=0
mcp_lifecycle_lock_acquire() { CRUCIBLE_MCP_LIFECYCLE_LOCK_FD=9; }
mcp_lifecycle_lock_release() {
    unset CRUCIBLE_MCP_LIFECYCLE_LOCK_FD
    LOCK_RELEASES=$((LOCK_RELEASES + 1))
}
crucible_ssh_agent_socket() { printf '%s/run/crucible/ssh-agent/agent.sock' "$TEST_ROOT"; }
start_ssh_agent() { mkdir -p "${TEST_ROOT}/run/crucible/ssh-agent"; }
stat() {
    if [ "${1:-}" = "-c" ] && [ "${2:-}" = "%u" ]; then
        printf '0\n'
    else
        command stat "$@"
    fi
}
fake_podman_run() {
    IMPORT_MOUNT=
    for argument in "$@"; do
        case "$argument" in
            --mount=type=bind,source=*/ssh-key-import.*,destination=*/ssh-key-import.*,readonly)
                IMPORT_MOUNT="$argument"
                ;;
        esac
    done
    if [ -z "$IMPORT_MOUNT" ]; then return 31; fi
    if [[ " $* " == *hostfs* ]]; then return 32; fi
    IMPORT_SOURCE=${IMPORT_MOUNT#*source=}
    IMPORT_SOURCE=${IMPORT_SOURCE%%,destination=*}
    LAST_ARGUMENT=${@: -1}
    [ "$LAST_ARGUMENT" = "${IMPORT_SOURCE}/key" ] || return 33
    [ -f "$LAST_ARGUMENT" ] || return 34
    [ "$(stat -c '%a' "$LAST_ARGUMENT")" = "600" ] || return 35
    [ "$(cat "$LAST_ARGUMENT")" = "private-key-material" ] || return 36
    return 0
}
podman_run=fake_podman_run
podman_run_interactive=fake_podman_run
ssh_identity_interactive=1
ssh_identity_args=(python3 ssh_identity_profiles.py)
ssh_identity_cli_args=(profiles import lab-admin)
ssh_identity_import_source="$SOURCE_KEY"
ssh_identity_import_profile=lab-admin
container_common_args=()
container_non_service_args=()
CRUCIBLE_CONTROLLER_IMAGE=test-controller
SESSION_ID=test-session
run_ssh_identity_command profiles import || exit 10
[ "$LOCK_RELEASES" -eq 1 ] || exit 11
[ -z "$(find "${TEST_ROOT}/run/crucible" -maxdepth 1 -type d -name 'ssh-key-import.*' -print -quit)" ] || exit 12
'''
            repository = Path(__file__).resolve().parents[2]
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=repository,
                env={
                    **os.environ,
                    "ROOT": str(repository),
                    "TEST_ROOT": temporary,
                    "SOURCE_KEY": str(source_key),
                },
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_profile_removal_runs_without_starting_the_agent(self):
        script = r'''
source <(awk '
    /^function run_ssh_identity_command\(\)/ { capture=1 }
    capture { print }
    capture && /^}/ { exit }
' "$ROOT/bin/crucible")
CRUCIBLE_MCP_LIFECYCLE_LOCK_FD=
LOCK_RELEASES=0
START_CALLS=0
PODMAN_CALLS=0
mcp_lifecycle_lock_acquire() { CRUCIBLE_MCP_LIFECYCLE_LOCK_FD=9; }
mcp_lifecycle_lock_release() {
    unset CRUCIBLE_MCP_LIFECYCLE_LOCK_FD
    LOCK_RELEASES=$((LOCK_RELEASES + 1))
}
start_ssh_agent() { START_CALLS=$((START_CALLS + 1)); return 37; }
fake_podman_run() {
    [ "${CRUCIBLE_MCP_LIFECYCLE_LOCK_FD:-}" = "9" ] || return 21
    PODMAN_CALLS=$((PODMAN_CALLS + 1))
    return 0
}
podman_run=fake_podman_run
podman_run_interactive=fake_podman_run
ssh_identity_interactive=0
ssh_identity_args=(python3 ssh_identity_profiles.py)
ssh_identity_cli_args=(profiles remove profile)
container_common_args=()
container_non_service_args=()
CRUCIBLE_CONTROLLER_IMAGE=test-controller
SESSION_ID=test-session
run_ssh_identity_command profiles remove || exit 10
[ "$START_CALLS" -eq 0 ] || exit 11
[ "$PODMAN_CALLS" -eq 1 ] || exit 12
[ "$LOCK_RELEASES" -eq 1 ] || exit 13
[ -z "${CRUCIBLE_MCP_LIFECYCLE_LOCK_FD:-}" ] || exit 14
'''
        repository = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=repository,
            env={**os.environ, "ROOT": str(repository)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_run_proxy_paths_are_ephemeral_and_cleaned(self):
        run_directory = Path(self.directory.name) / "run"
        config_directory = run_directory / "config"
        config_directory.mkdir(parents=True)
        run_file = config_directory / "run-file.json"
        run_file.write_text(
            json.dumps({
                "endpoints": [
                    {"type": "osp", "ssh-identity-profile": "performance"}
                ]
            }),
            encoding="utf-8",
        )
        socket_root = Path(self.directory.name) / "socket-root"
        pins = {
            "performance": {
                "version": 1,
                "fingerprint": self.fingerprint,
                "revoked": False,
            }
        }

        class FakeChild:
            pid = os.getpid()

            @staticmethod
            def poll():
                return None

        def start_fake_proxy(command, **_kwargs):
            socket_dir = Path(command[command.index("--socket-dir") + 1])
            map_path = Path(command[command.index("--map-file") + 1])
            ready_path = Path(command[command.index("--ready-file") + 1])
            run_token = command[-1].split("=", 1)[1]
            _atomic_json(
                map_path,
                {"performance": str(socket_dir / "performance.sock")},
            )
            _atomic_json(ready_path, {"ready": True, "run_token": run_token})
            return FakeChild()

        args = SimpleNamespace(
            run_file=run_file,
            run_directory=run_directory,
            catalog=self.catalog,
            agent_socket="/tmp/test-agent.sock",
            known_hosts=Path(self.directory.name) / "known_hosts",
            profile_lock=None,
        )
        with (
            patch("ssh_identity_profiles.PROFILE_SOCKET_ROOT", socket_root),
            patch("ssh_identity_profiles._read_profile_lock", return_value=pins),
            patch("ssh_identity_profiles.subprocess.Popen", side_effect=start_fake_proxy),
        ):
            result = prepare_run(args)
            stop_run(SimpleNamespace(state_file=result["state_file"]))

        socket_dir = Path(result["socket_dir"])
        self.assertEqual(Path(result["map_file"]).parent, socket_dir)
        self.assertFalse((config_directory / "ssh-profile-sockets.json").exists())
        self.assertFalse(socket_dir.exists())
        self.assertEqual(list(socket_root.iterdir()), [])

    def test_large_profileless_cli_run_does_not_use_profile_size_limit(self):
        run_file = Path(self.directory.name) / "large-run.json"
        run_file.write_bytes(b'{"benchmarks": []}' + b" " * 1_048_577)
        run_directory = Path(self.directory.name) / "run"
        args = SimpleNamespace(
            run_file=run_file,
            run_directory=run_directory,
            catalog=self.catalog,
            agent_socket="/tmp/test-agent.sock",
            known_hosts=Path(self.directory.name) / "known_hosts",
            profile_lock=None,
        )

        result = prepare_run(args)

        self.assertTrue(Path(result["map_file"]).is_file())
        self.assertEqual(result["socket_dir"], "")

    def test_large_run_with_ssh_profile_still_uses_profile_size_limit(self):
        run_file = Path(self.directory.name) / "large-profile-run.json"
        document = (
            b'{"endpoints":[{"type":"remotehosts",'
            b'"ssh-identity-profile":"performance",'
            b'"remotes":[{"config":{}}]}]}'
        )
        run_file.write_bytes(document + b" " * 1_048_577)
        args = SimpleNamespace(
            run_file=run_file,
            run_directory=Path(self.directory.name) / "run",
            catalog=self.catalog,
            agent_socket="/tmp/test-agent.sock",
            known_hosts=Path(self.directory.name) / "known_hosts",
            profile_lock=None,
        )

        with self.assertRaisesRegex(SSHIdentityError, "run file exceeds its size limit"):
            prepare_run(args)

    def test_run_command_exports_profile_context_and_stops_proxy(self):
        prepared = {
            "map_file": "/run/crucible/ssh-profile-agents/profile-sockets.json",
            "known_hosts": "/var/lib/crucible/ssh-identities/known_hosts",
            "socket_dir": "/run/crucible/ssh-profile-agents/p-test",
            "state_file": "/run/crucible/ssh-profile-agents/p-test/state.json",
        }

        class FakeCommand:
            returncode = None

            @staticmethod
            def poll():
                return FakeCommand.returncode

            @staticmethod
            def wait():
                FakeCommand.returncode = 7
                return 7

            @staticmethod
            def send_signal(_signum):
                pass

            @staticmethod
            def terminate():
                FakeCommand.returncode = -15

            @staticmethod
            def kill():
                FakeCommand.returncode = -9

        args = SimpleNamespace(run_command=["--", "/bin/example", "argument"])
        with (
            patch("ssh_identity_profiles.prepare_run", return_value=prepared),
            patch("ssh_identity_profiles.subprocess.Popen", return_value=FakeCommand()) as launch,
            patch("ssh_identity_profiles.stop_run") as stop,
        ):
            result = run_with_profiles(args)

        self.assertEqual(result, 7)
        command, = launch.call_args.args
        environment = launch.call_args.kwargs["env"]
        self.assertEqual(command, ["/bin/example", "argument"])
        self.assertEqual(environment["CRUCIBLE_SSH_PROFILE_SOCKETS_FILE"], prepared["map_file"])
        self.assertEqual(environment["CRUCIBLE_SSH_KNOWN_HOSTS_FILE"], prepared["known_hosts"])
        self.assertEqual(environment["CRUCIBLE_SSH_PROFILE_ACTIVE"], "1")
        self.assertTrue(launch.call_args.kwargs["preexec_fn"])
        stop.assert_called_once()
        self.assertEqual(
            stop.call_args.args[0].state_file,
            prepared["state_file"],
        )

    def test_workload_child_masks_upstream_agent_mounts(self):
        agent_socket = "/run/crucible/ssh-agent/agent.sock"
        agent_directory = Path(agent_socket).parent
        hostfs_directory = Path("/hostfs") / agent_directory.relative_to("/")
        libc = SimpleNamespace(unshare=Mock(return_value=0), mount=Mock(return_value=0))

        with (
            patch("ssh_identity_profiles.ctypes.CDLL", return_value=libc),
            patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=0o040700)),
        ):
            _hide_unfiltered_agent_socket(agent_socket)

        libc.unshare.assert_called_once_with(0x00020000)
        targets = [
            call.args[1]
            for call in libc.mount.call_args_list
            if call.args[0] == b"tmpfs"
        ]
        self.assertEqual(
            targets,
            [os.fsencode(agent_directory), os.fsencode(hostfs_directory)],
        )

    def test_run_command_delegates_invalid_json_to_child_diagnostics(self):
        class FakeCommand:
            @staticmethod
            def poll():
                return 1

            @staticmethod
            def wait():
                return 1

        run_file = Path(self.directory.name) / "invalid-run-file.json"
        run_file.write_text('{"benchmarks": [\n . "name"]\n}', encoding="utf-8")
        args = SimpleNamespace(
            run_command=["--", "/bin/example", "argument"],
            run_file=run_file,
        )

        with patch("ssh_identity_profiles.subprocess.Popen", return_value=FakeCommand()) as launch:
            result = run_with_profiles(args)

        self.assertEqual(result, 1)
        command, = launch.call_args.args
        self.assertEqual(command, ["/bin/example", "argument"])

    def test_main_dispatches_run_subcommand_without_overwriting_action(self):
        with patch("ssh_identity_profiles.run_with_profiles", return_value=23) as run:
            result = main([
                "run",
                "--run-file", "/tmp/run-file.json",
                "--run-directory", "/tmp/run",
                "--",
                "/bin/example",
                "argument",
            ])

        self.assertEqual(result, 23)
        args, = run.call_args.args
        self.assertEqual(args.command, "run")
        self.assertEqual(args.run_command, ["--", "/bin/example", "argument"])


if __name__ == "__main__":
    unittest.main()
