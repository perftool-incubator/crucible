# Stable Release
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
repositories. Stable releases must be labeled to be an "out of the box" 
Crucible install.

### Tagging
Create a new CI job that depends on the existing periodic CI job 
(scheduled) that runs against merged commits that passed pre-merge jobs.
The new CI job will run only if the existing scheduled CI job succeeds.

                           PASS  
CI workflow:
crucible-core-ci-scheduled --> crucible-core-STABLE-RELEASE-ci-scheduled 
       (existing)                      (new / to be created)

The "STABLE-RELEASE" job should check for the tag YYYY.N, and tag
every repository, including the main Crucible project if the tag does
NOT exist. Example: 2024.1, 2025.3, etc.

YYYY	4-digit Year
N	Quarter (1=Jan-Mar, 2=Apr-Jun, 3=Jul-Sept, 4=Oct-Dec)

The job should exit and do nothing if the tag already exists.

A `--force` option will override the tag if specified (manual trigger).

### Installer
- Add a new `--stable-release=<tag>` to the installation
- Add a new `crucible stable-release` to list tagged releases 
