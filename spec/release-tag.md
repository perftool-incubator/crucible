# Release tag
Implement a "Release" mechanism for installing "consistent" versions of
Crucible.

## Problem description
We need to be able to install a "version" of Crucible that is "locked down"
and won't change at all if it is installed over time.

Crucible installations cannot be used for regression testing in a consistent
manner.

bin/update will override the installed repository by pulling the latest commit
if the installed commit is an older one.

Crucible installer clones Crucible repository and all sub-projects from the
latest "main" branch. A default installation contains all the Crucible
repositories from the latest merged commits.

```
#project-name        project-type   git-repo-url                     branch
rickshaw             core           /rickshaw                        master
multiplex            core           /multiplex                       master
roadblock            core           /roadblock                       master
...
hwlatdetect          benchmark      /bench-hwlatdetect               main
tracer               benchmark      /bench-tracer                    main
...
ftrace               tool           /tool-ftrace                     master
testing              doc            /testing-repo                    master
...
```

## Proposed change
"Locked down" release by pinning all sub-projects to a commit that is
verified by CI after code merges. Sub-projects includes core and benchmark
repositories. Stable releases must be labeled to be a "locked-down" version
of Crucible installation.

### Tagging
The solution proposed consists in tagging specific commits of crucible
and its sub-projects to label versions of Crucible that are verified.

The tag format should be as follows:
YYYY	4-digit Year
N	Quarter (1=Jan-Mar, 2=Apr-Jun, 3=Jul-Sept, 4=Oct-Dec)

Example: 2024.1, 2025.3

### Scheduled job
Create a new CI job that runs every quarter (scheduled) against merged
commits that passed pre-merge jobs.

The "crucible-release" job should check for the tag YYYY.N, and tag
every repository, including the main Crucible project if the tag does
NOT exist.

The workflow_dispatch (manual trigger) job includes the following inputs:
 - A `dry-run` mode will skip the push tag step for debugging purposes.
 - A `custom-tag` for pushing/deleting an user-defined version label.
 - An `action` choice to push or delete a tag

### Installer
- Add a new `--release=<tag>` to the installation to specify the release
that will be installed.
*TBD*

### Crucible command
- Add a new `crucible repo release` to list tagged releases
*TBD*
