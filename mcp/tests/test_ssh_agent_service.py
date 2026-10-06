import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class TestSSHAgentService(unittest.TestCase):
    repository = Path(__file__).resolve().parents[2]

    def test_host_root_mount_is_scoped_to_run_file_execution(self):
        base = (self.repository / "bin/base").read_text(encoding="utf-8")
        common_start = base.index("container_common_args=()")
        run_mount_start = base.index("container_run_file_args=", common_start)
        shared_mounts = base[common_start:run_mount_start]
        self.assertNotIn("destination=/hostfs", shared_mounts)
        self.assertIn(
            'container_run_file_args=("--mount=type=bind,source=/,destination=/hostfs")',
            base,
        )
        listener_start = base.index("container_mcp_listener_args=()")
        bridge_start = base.index("container_mcp_host_bridge_args=()", listener_start)
        listener_args = base[listener_start:bridge_start]
        self.assertNotIn("source=/root,destination=/root", listener_args)
        self.assertNotIn("source=/home,destination=/home", listener_args)
        self.assertNotIn('("--privileged")', listener_args)
        self.assertNotIn('("--pid=host")', listener_args)
        self.assertIn('"${container_mcp_listener_args[@]}"', base)
        self.assertIn('"${container_mcp_host_bridge_args[@]}"', base)
        self.assertIn('function start_mcp_host_bridge()', base)
        self.assertIn('function stop_mcp_host_bridge()', base)
        self.assertIn('container_mcp_host_bridge_args+=("-e PYTHONPATH=${CRUCIBLE_HOME}/mcp")', base)
        self.assertIn('5) mcp_data_path=${mcp_archive_root}; mcp_data_readonly=true ;;', base)
        self.assertIn(
            'Writable MCP data paths cannot overlap the local archive directory',
            base,
        )
        run_command = (self.repository / "bin/_main").read_text(encoding="utf-8")
        self.assertIn('"${container_run_file_args[@]}"', run_command)
        self.assertIn(
            '"--mount=type=bind,source=${mcp_ssh_profile_import_root},'
            'destination=${mcp_ssh_profile_import_root},readonly"',
            base,
        )
        self.assertIn(
            '"--mount=type=bind,source=${mcp_ssh_agent_dir},'
            'destination=${mcp_ssh_agent_dir},readonly"',
            base,
        )

    def test_listener_arguments_use_canonical_mount_paths(self):
        base = (self.repository / "bin/base").read_text(encoding="utf-8")
        command_position = base.index("mcp_cmd=(")
        canonicalized_paths = (
            'mcp_database=$(mcp_canonical_file_path "${mcp_database}")',
            'mcp_audit_log=$(mcp_canonical_file_path "${mcp_audit_log}")',
            'mcp_token_file=$(mcp_canonical_file_path "${mcp_token_file}")',
            'mcp_log_db=$(mcp_canonical_file_path "${LOG_DB}")',
            'mcp_input_root=$(mcp_canonical_directory_path "${mcp_input_root}")',
        )
        for assignment in canonicalized_paths:
            self.assertLess(base.index(assignment), command_position)
        for argument in (
            '--database "${mcp_database}"',
            '--input-root "${mcp_input_root}"',
            '--audit-log "${mcp_audit_log}"',
            '--log-db "${mcp_log_db}"',
        ):
            self.assertIn(argument, base)

    def test_data_mount_policy_blocks_protected_subtrees_but_allows_crucible_data(self):
        script = r'''
set -eu
TEST_ROOT=$(mktemp -d)
trap 'rm -rf "$TEST_ROOT"' EXIT
source <(awk '
    /^function mcp_canonical_directory_path\(\)/ { capture=1 }
    /^function mcp_logger_store_directory\(\)/ { capture=1 }
    /^function mcp_data_mount_is_protected\(\)/ { capture=1 }
    /^function stop_mcp_host_bridge\(\)/ { exit }
    capture { print }
' "$ROOT/bin/base")
var_crucible=/var/lib/crucible
mkdir -p "$TEST_ROOT/home/.crucible" "$TEST_ROOT/external"
mkdir -p "$TEST_ROOT/physical/config" "$TEST_ROOT/physical/input"
ln -s "$TEST_ROOT/physical" "$TEST_ROOT/alias"
HOME="$TEST_ROOT/home"
export HOME
canonical_dir=$(mcp_canonical_directory_path "$TEST_ROOT/alias/input")
canonical_file=$(mcp_canonical_file_path "$TEST_ROOT/alias/config/jobs.db")
if [ "$canonical_dir" != "$TEST_ROOT/physical/input" ] || \
   [ "$canonical_file" != "$TEST_ROOT/physical/config/jobs.db" ]; then
    echo "configured paths were not canonicalized consistently" >&2
    exit 1
fi
logger_store=$(mcp_logger_store_directory)
if [ "$logger_store" != "$TEST_ROOT/home/.crucible" ]; then
    echo "unexpected logger store directory: $logger_store" >&2
    exit 1
fi
if mcp_data_mount_is_protected "$logger_store" true "$logger_store"; then
    echo "expected the exact read-only logger store to be allowed" >&2
    exit 1
fi
if ! mcp_data_mount_is_protected /root/.crucible false /root/.crucible; then
    echo "expected a writable logger store mount to remain protected" >&2
    exit 1
fi
if ! mcp_data_mount_is_protected /root true "$logger_store"; then
    echo "expected the home directory itself to remain protected" >&2
    exit 1
fi
if ! mcp_data_mount_is_protected /root/.crucible/child true /root/.crucible; then
    echo "expected descendants of the logger store to remain protected" >&2
    exit 1
fi
if mcp_data_mount_is_protected /root/.crucible true /root/.crucible; then
    echo "expected the exact read-only root logger store to be allowed" >&2
    exit 1
fi
if ! mcp_data_mount_is_protected /root/.crucible true /root/other; then
    echo "expected only the validated logger store to receive the exception" >&2
    exit 1
fi
mv "$TEST_ROOT/home/.crucible" "$TEST_ROOT/home/.crucible-original"
ln -s "$TEST_ROOT/external" "$TEST_ROOT/home/.crucible"
if mcp_logger_store_directory >/dev/null; then
    echo "expected a symlinked logger store to be rejected" >&2
    exit 1
fi
rm "$TEST_ROOT/home/.crucible"
mv "$TEST_ROOT/home/.crucible-original" "$TEST_ROOT/home/.crucible"
for path in /etc/ssh /root/.ssh /home/alice /usr/lib /opt/other /boot/efi /proc/1 /sys/kernel /dev/shm /var/log /var/lib/crucible; do
    if ! mcp_data_mount_is_protected "$path"; then
        echo "expected protected path: $path" >&2
        exit 1
    fi
done
for path in /var/lib/crucible/run /var/lib/crucible/archive /var/lib/crucible/mcp; do
    if mcp_data_mount_is_protected "$path"; then
        echo "expected allowed Crucible data path: $path" >&2
        exit 1
    fi
done
'''
        result = subprocess.run(
            ["bash", "-c", script],
            env={**os.environ, "ROOT": str(self.repository)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    harness = r'''
set -u
unset CRUCIBLE_MCP_LIFECYCLE_LOCK_FD
source <(awk '
    /^function mcp_lifecycle_lock_acquire\(\)/ { capture=1 }
    /^function start_mcp_server\(\)/ { exit }
    capture { print }
' "$ROOT/bin/base")

var_crucible="$TEST_ROOT"
crucible_ssh_agent_dir() { printf '%s\n' "${TEST_ROOT}/ssh-agent"; }
CRUCIBLE_CONTROLLER_IMAGE=test-controller
container_common_args=()
container_service_args=()
AGENT_RUNNING=0
AGENT_PID=""
RUN_CONTAINERS=""
ACTIVE_JOBS=""
PODMAN_PS_STATUS=0
CLEAR_RUN_CALLS=0
AGENT_MOUNT_SEEN=0

mcp_lifecycle_lock_acquire() { CRUCIBLE_MCP_LIFECYCLE_LOCK_FD=9; }
mcp_lifecycle_lock_release() { unset CRUCIBLE_MCP_LIFECYCLE_LOCK_FD; }
stat() {
    if [ "${1:-}" = "-c" ] && [ "${2:-}" = "%u" ]; then
        printf '0\n'
    else
        command stat "$@"
    fi
}
podman_running() {
    case "${1:-}" in
        crucible-ssh-agent) [ "${AGENT_RUNNING}" -eq 1 ] ;;
        crucible-mcp-server) return 1 ;;
        *) return 1 ;;
    esac
}
fake_podman_ps() {
    [ -z "${RUN_CONTAINERS}" ] || printf '%s\n' "${RUN_CONTAINERS}"
    return "${PODMAN_PS_STATUS}"
}
mcp_active_jobs() {
    [ -z "${ACTIVE_JOBS}" ] || printf '%s\n' "${ACTIVE_JOBS}"
}
fake_podman_run() {
    if [[ " $* " == *" --mount=type=bind,source=${TEST_ROOT}/ssh-agent,destination=${TEST_ROOT}/ssh-agent "* ]]; then
        AGENT_MOUNT_SEEN=1
    fi
    if [[ " $* " == *" agent clear "* ]]; then
        CLEAR_RUN_CALLS=$((CLEAR_RUN_CALLS + 1))
        return 0
    fi
    local socket_path="${@: -1}"
    python3 -c 'import socket,sys,time; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(); time.sleep(60)' "${socket_path}" >/dev/null 2>&1 &
    AGENT_PID=$!
    AGENT_RUNNING=1
}
fake_podman_stop() {
    if [ -n "${AGENT_PID}" ]; then
        kill "${AGENT_PID}" 2>/dev/null || true
        wait "${AGENT_PID}" 2>/dev/null || true
    fi
    AGENT_RUNNING=0
}
podman_run=fake_podman_run
podman_stop=fake_podman_stop
podman_ps=fake_podman_ps

if ! start_ssh_agent; then
    exit 10
fi
if [ "${AGENT_MOUNT_SEEN}" -ne 1 ]; then
    exit 24
fi
socket_path=$(crucible_ssh_agent_socket)
if [ ! -S "${socket_path}" ]; then
    exit 11
fi
if ! ssh_agent_run_start_reserve; then
    exit 18
fi
if stop_ssh_agent; then
    exit 19
fi
if [ "${AGENT_RUNNING}" -ne 1 ] || [ ! -S "${socket_path}" ]; then
    exit 20
fi
if ! ssh_agent_run_start_release; then
    exit 21
fi
RUN_CONTAINERS=crucible-rickshaw-run-test
if stop_ssh_agent; then
    exit 14
fi
if [ "${AGENT_RUNNING}" -ne 1 ] || [ ! -S "${socket_path}" ]; then
    exit 15
fi
RUN_CONTAINERS=
ACTIVE_JOBS=mcp-job-test
if stop_ssh_agent; then
    exit 16
fi
if [ "${AGENT_RUNNING}" -ne 1 ] || [ ! -S "${socket_path}" ]; then
    exit 17
fi
ACTIVE_JOBS=
PODMAN_PS_STATUS=2
if clear_ssh_agent; then
    exit 22
fi
if [ "${AGENT_RUNNING}" -ne 1 ] || [ ! -S "${socket_path}" ] || [ "${CLEAR_RUN_CALLS}" -ne 0 ]; then
    exit 23
fi
PODMAN_PS_STATUS=0
if ! stop_ssh_agent; then
    exit 12
fi
if [ -e "${socket_path}" ] || [ -L "${socket_path}" ]; then
    exit 13
fi
'''

    def test_managed_agent_starts_on_shared_socket_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                ["bash", "-c", self.harness],
                cwd=self.repository,
                env={**os.environ, "ROOT": str(self.repository), "TEST_ROOT": temporary},
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "Checking for Crucible SSH agent...not present\n"
            "Starting Crucible SSH agent\n",
            result.stdout,
        )
        self.assertIn(
            "Checking for Crucible SSH agent...appears to be running\n"
            "Successfully stopped Crucible SSH agent\n",
            result.stdout,
        )
        self.assertIn(
            "ERROR: Cannot stop SSH agent while a Crucible run is active",
            result.stdout,
        )
        self.assertIn(
            "ERROR: Cannot stop SSH agent while MCP jobs are active",
            result.stdout,
        )
        self.assertIn(
            "ERROR: Cannot stop SSH agent while a profile-enabled run is starting",
            result.stdout,
        )
        self.assertIn(
            "ERROR: Cannot verify Crucible run state; refusing to clear SSH identities",
            result.stdout,
        )
        self.assertNotIn("agentSuccessfully", result.stdout)

    def test_agent_wrappers_reuse_an_already_held_lifecycle_lock(self):
        script = r'''
set -u
source <(awk '
    /^function mcp_lifecycle_lock_acquire\(\)/ { capture=1 }
    /^function start_mcp_server\(\)/ { exit }
    capture { print }
' "$ROOT/bin/base")
CRUCIBLE_MCP_LIFECYCLE_LOCK_FD=42
LOCK_ACQUIRES=0
LOCK_RELEASES=0
START_CALLS=0
STOP_CALLS=0
mcp_lifecycle_lock_acquire() {
    LOCK_ACQUIRES=$((LOCK_ACQUIRES + 1))
    return 1
}
mcp_lifecycle_lock_release() {
    LOCK_RELEASES=$((LOCK_RELEASES + 1))
    unset CRUCIBLE_MCP_LIFECYCLE_LOCK_FD
}
start_ssh_agent_unlocked() { START_CALLS=$((START_CALLS + 1)); }
stop_ssh_agent_unlocked() { STOP_CALLS=$((STOP_CALLS + 1)); }

start_ssh_agent || exit 10
stop_ssh_agent || exit 11
[ "$LOCK_ACQUIRES" -eq 0 ] || exit 12
[ "$LOCK_RELEASES" -eq 0 ] || exit 13
[ "$START_CALLS" -eq 1 ] || exit 14
[ "$STOP_CALLS" -eq 1 ] || exit 15
[ "$CRUCIBLE_MCP_LIFECYCLE_LOCK_FD" = 42 ] || exit 16
'''
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=self.repository,
            env={**os.environ, "ROOT": str(self.repository)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
