@AGENTS.md

## Claude Code only

### Developer Tools

Crucible includes a Claude Code plugin (`crucible-dev-tools`) for development
workflows. New installations have the plugin repo already cloned. For existing
installs, run `crucible update` first. Then register the plugin marketplace:

```
claude plugin marketplace add ${CRUCIBLE_HOME}/subprojects/core/crucible-dev-tools
```

Claude Code will prompt you to install the crucible-tools plugin — accept it.

Antigravity users don't need this manual step: `.agents/plugins.json` at the
repo root points Antigravity at the same plugin directory for automatic
discovery.

Available skills:
- `/crucible-tools:activity-summary` — generate an activity summary for the GitHub organization
- `/crucible-tools:architecture-review` — top-down architecture and design review across crucible subsystems
- `/crucible-tools:codebase-audit` — comprehensive multi-pass codebase audit across all crucible repos
- `/crucible-tools:debug-log` — analyze crucible logs to debug failed runs or commands
- `/crucible-tools:dev-activity` — generate development activity charts (commits, PRs, workflow runs)
- `/crucible-tools:image-cleanup` — clean up local podman images (engine images, dangling images, local builds)
- `/crucible-tools:new-repo` — create a new repository in the GitHub organization with standard config
- `/crucible-tools:open-prs` — show all open PRs in the org (optionally filter by author)
- `/crucible-tools:pr-review` — structured multi-dimension code review for a PR
- `/crucible-tools:repo-status` — git status across all crucible repos
- `/crucible-tools:workflow-status` — show active CI workflow runs across crucible repos
- `ci-analyzer` agent — analyze GitHub Actions CI workflow runs to diagnose failures
