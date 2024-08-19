# repos.json
Re-implement the [default_subprojects](/config/default_subprojects) as
a JSON config file.

## Problem Description
The existing [default_subprojects](/config/default_subprojects) config
file is very limiting in terms of the information it is capable of
holding, it is difficult/annoying to parse when looking for a single
piece of information (such as a project's checkout target), and
updating it is is similarly complicated.  It also makes assumptions
about the relationship of the repositories -- ie. they all belong to
the same project/user as the primary Crucible repository.

Up until now our needs have been fairly limited so the limitations of
this config file have not been overly problematic.  However, new (and
future) capabilities require additional flexibility in the config file
that we must now implement.

## Proposed change
By redesigning the config file to use a JSON format the idea is that
parsing (and formatting) of the configuration file becomes a
non-issue.  A JSON schema can be written that is used to validate the
structure, formatting, and contents of the config file.  Additionally,
many languages (ie. Python) and CLI tools (ie. jq) have support for
automatically loading, querying, updating, and saving JSON files so we
will not have to write/update custom tools needlessly.

Further, the contents of the file will be updated.  There will no
longer be an assumption of a common repository parentage and
additional properties will be added to assist in the implementation of
new and future capabilities

### Current Config File

The current [default_subprojects](/config/default_subprojects) looks
like this:

```
# All subprojects will be pulled from the same project/user as the Crucible environment.
# By default this is "https://github.com/perftool-incubator" + "/<git-repo-url>"
#project-name        project-type   git-repo-url                                    branch
rickshaw             core           /rickshaw                                       master
multiplex            core           /multiplex                                      master
roadblock            core           /roadblock                                      master
workshop             core           /workshop                                       master
CommonDataModel      core           /CommonDataModel                                master
toolbox              core           /toolbox                                        main
packrat              core           /packrat                                        master
crucible-ci          core           /crucible-ci                                    main
fio                  benchmark      /bench-fio                                      master
uperf                benchmark      /bench-uperf                                    master
oslat                benchmark      /bench-oslat                                    master
trafficgen           benchmark      /bench-trafficgen                               main
cyclictest           benchmark      /bench-cyclictest                               main
hwlatdetect          benchmark      /bench-hwlatdetect                              main
tracer               benchmark      /bench-tracer                                   main
sysstat              tool           /tool-sysstat                                   master
procstat             tool           /tool-procstat                                  master
ftrace               tool           /tool-ftrace                                    master
kernel               tool           /tool-kernel                                    master
ovs                  tool           /tool-ovs                                       master
rt-trace-bpf         tool           /tool-rt-trace-bpf                              main
forkstat             tool           /tool-forkstat                                  main
flexran              benchmark      /bench-flexran                                  main
iperf                benchmark      /bench-iperf                                    main
examples             doc            /crucible-examples                              main
testing              doc            /testing-repo                                   master
ilab                 benchmark      /bench-ilab                                     main
nvidia               tool           /tool-nvidia                                    main

```

### Proposed Config File

The proposed JSON config file will be called
[repos.json](/config/repos.json) and would look something like this:

```
{
  "official": [      # all of the repositories that are part of the "official upstream project"
    {
      "name": "crucible",
      "type": "glue",              # choices: glue (only 1 -- Crucible), core, tool, benchmark, or doc
      "repository": "https://github.com/perftool-incubator/crucible.git",
      "primary-branch": "master",  # choices: master or main
      "checkout": {
        "mode": "follow",          # choices: follow (upstream) or user (chosen, stay at)
        "target": "master"         # branch/tag/commit the repo is expected to be at
      }
    }.
    {
      "name": "rickshaw",
      "type": "core",
      "repository": "https://github.com/perftool-incubator/rickshaw.git",
      "primary-branch": "master",
      "checkout": {
        "mode": "user",
        "target": "2024.4"
      }
    },
    ...
    {
      "name": "iperf",
      "type": "benchmark",
      "respository": "https://github.com/perftool-incubator/bench-iperf.git",
      "primary-branch": "main",
      "checkout": {
        "mode": "follow",
        "target": "main"
      }
    },
  ...
  ],
  "3rd-party": [     # this would be user supplied repositories that should be managed by crucible (for updates, checkouts, etc.) but are not part of the "official upstream project" -- ie. tools, benchmarks, userenv libraries, etc.
  ]                  # formatting: placing the [] on separate lines is important to make seemless merges of upstream changes when local changes (ie. array contents) are present
}
```

### Other Changes

Making a significant change like this to the repository configuration
file will require changes to several other parts of Crucible such as
the installer and update utilities.  Once a consensus on the schema
for the new JSON config has been reach then the additional changes to
other parts of Crucible should become better understood.
