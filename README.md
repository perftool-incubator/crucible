# crucible

## Introduction
Crucible takes multiple performance tooling projects and integrates them, with the intention to provide a well rounded, portable, and highly functional end-to-end performance tool harness.  If successful, crucible can be used for test automation which can execute, measure, store, visualize, and analyze the performance of various systems-under-test.

## Goals

### Simple Installation
Crucible is designed to have a simple, portable installation, required on only a single target host (a controller), and not require installation on any other host (end-point) that is used for testing.  Installation should be possible on nearly any Linux distribution and nearly any platform.

### Easy to Develop
By design Crucible itself is not providing a specific function for performance engineering - other than providing a point of integration for other performance engineering projects.  Crucible only includes such projects which are small and provide a specific function.  Crucible is designed to operate directly with git repositories for each of these projects, allowing flexibility in choosing different branches/commits/tags among all of the projects.  There is no monolithic, do-everything code base here.

Crucible operates on your host the same way whether it is used by a developer or an end-user, with the only difference being which git branch/commit is being used at that time.  The details of the Git operations are abstracted from the end-user, but the installation target does require Git to operate.  All software dependencies are handled at run-time.  There are no binary packages to build to provide to end-users.
