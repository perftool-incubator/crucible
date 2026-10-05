import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class TestSSHAgentService(unittest.TestCase):
    repository = Path(__file__).resolve().parents[2]

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
