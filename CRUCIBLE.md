# Crucible

Crucible's purpose is to provide a single interface
for all of these subprojects for a seamless performance
benchmark and tool automation solution

Crucible expects to have these subprojects:
```
Project-name      User-Commands   Purpose                   Status

Rickshaw          run             Coordinate a full "run"   Implemented with
                                  of a benchmark with one   localhost, remotehost,
                                  or more iterations        and k8s endpoints

Multiplex         <TBD>           Convert a user's list     Currently only
                                  of benchamrk params       supported via
                                  (--opt val1,val2)         --mv-params with
                                  into multiple benchmark   run command (and
                                  iterations                requires mv-params
                                                            JSON format

Roadblock         none            Provide synchrnization    Fully integrated
                                  and message passing
                                  across all participants
                                  in benchmark execution

Workshop          none            Dynamic, multi-distro     Fully integrated
                                  container builder for
                                  all SW needed for bench-
                                  marks and tools.

Tools             none            Helper scripts for tool   Framework intregated,
subprojects                       execution and post-       per-tool status with
                                  processing                post-processed metrics
                                                            supported below:

                                                            mpstat: cpu util
                                                            sar: netdev tput
                                                            iostat:
                                                            pidstat:
                                                            perf:

Benchmarks                      Helper scripts for         Framework intregated,
subprojects       none          benchmark execution        per-benchmark status
                                and post-processing        below:
                                                           uperf: fully integrated
                                                           fio: fully integrated

CommonDataModel   <TBD>         Data storage, query        OpenSearch instance
                                                           automatically created
                                                           and indices created.
```
