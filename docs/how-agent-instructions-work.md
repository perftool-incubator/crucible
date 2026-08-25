# How Agent Instructions Work

This document explains the `AGENTS.md` / `CLAUDE.md` convention
used to instruct AI coding agents working in crucible and its
subprojects, and how that convention should be applied when
creating or updating repos.

## Overview

Crucible repos are worked on by more than one AI coding agent
(Claude Code, Antigravity, and potentially others in the
future). Each tool has its own convention for where it looks
for instructions — Antigravity reads `AGENTS.md` natively,
Claude Code reads `CLAUDE.md`. Maintaining separate,
independently-written instruction files per tool invites drift:
a rule added for one agent silently doesn't apply to another.

The convention is a single canonical file plus a thin
per-tool layer:

- **`AGENTS.md`** — canonical, tool-agnostic instructions
- **`CLAUDE.md`** — imports `AGENTS.md`, then adds only content
  with no cross-tool equivalent

## AGENTS.md — the canonical file

`AGENTS.md` holds everything that isn't specific to a particular
agent: project overview, architecture, code conventions,
workflow rules (git, testing, debugging), and PR process. This
is the file to edit for almost all instruction changes.

Any tool that supports the `AGENTS.md` convention reads it
directly. No further wiring is needed for those tools.

## CLAUDE.md — the Claude Code layer

`CLAUDE.md` starts with an `@AGENTS.md` import (Claude Code's
file-import syntax), pulling in the full canonical content, then
adds a `## Claude Code only` section for anything that genuinely
has no equivalent in other tools — for example, crucible's
`crucible-dev-tools` plugin marketplace registration and skills
list, which are mechanisms specific to Claude Code's plugin
system.

```markdown
@AGENTS.md

## Claude Code only

(content with no cross-tool equivalent goes here)
```

Nothing should be duplicated between the two files. If a rule
applies beyond Claude Code, it belongs in `AGENTS.md` — not
copy-pasted into `CLAUDE.md`'s Claude Code–only section.

## Applying this to subprojects

This convention applies to the crucible repo and to every
subproject repo. Per the "Agent instruction file updates"
guideline in `AGENTS.md`'s Pull Requests and Contributions
section, structural changes to a subproject should update that
subproject's `AGENTS.md` and/or `CLAUDE.md` in the same PR,
following the same split described here.

New repos should be scaffolded with both files from creation —
`AGENTS.md` following the shape of crucible's own (project
overview, architecture, conventions, PR process), and `CLAUDE.md`
as the two-line `@AGENTS.md` import plus a `## Claude Code only`
section, populated only if the repo actually needs Claude
Code–specific content.

Crucible's own [`AGENTS.md`](../AGENTS.md) and
[`CLAUDE.md`](../CLAUDE.md) are the reference implementation of
this pattern.
