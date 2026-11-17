# Crucible Repository Relationships

This diagram shows how the independently versioned repositories in the
Crucible ecosystem relate to one another. Each repo is a separate git
repository, cloned to `repos/` and activated via symlinks in `subprojects/`.

```mermaid
flowchart TB
    User([User])

    subgraph Primary["Primary repository"]
        CLI["crucible<br/>CLI / installer / controller"]
    end

    subgraph Core["Core repositories"]
        RICKSHAW["rickshaw<br/>Run orchestration"]
        MULTIPLEX["multiplex<br/>Parameter expansion"]
        ROADBLOCK["roadblock<br/>Distributed synchronization"]
        WORKSHOP["workshop<br/>Image build definitions"]
        CDM["CommonDataModel<br/>Data model / queries / dashboard"]
        TOOLBOX["toolbox<br/>Shared library"]
        PACKRAT["packrat<br/>System info collection"]
    end

    subgraph Benchmarks["Benchmark repositories (20+)"]
        BENCH["fio, uperf, stream, cyclictest,<br/>oslat, trafficgen, hammerdb, ..."]
    end

    subgraph Tools["Tool repositories (17+)"]
        TOOLS["sysstat, procstat, ftrace,<br/>ovs, nvidia, power, ..."]
    end

    subgraph Services["Supporting services (containers)"]
        SIS["image-sourcing-service<br/>Builds engine images"]
        VALKEY[("Valkey<br/>Barrier backend")]
        OPENSEARCH[("OpenSearch<br/>Result storage")]
    end

    subgraph DevInfra["Development infrastructure"]
        CI["crucible-ci<br/>Cross-repo CI"]
        DEVTOOLS["crucible-dev-tools<br/>Agent instructions / skills"]
    end

    subgraph Docs["Documentation repositories"]
        EXAMPLES["examples"]
        TESTING["testing"]
    end

    User -->|"writes run file"| CLI
    CLI -->|"invokes for runs"| RICKSHAW

    RICKSHAW -->|"expands parameters<br/>into iterations"| MULTIPLEX
    RICKSHAW -->|"requests engine images"| SIS
    RICKSHAW -->|"synchronizes engines"| ROADBLOCK
    ROADBLOCK -->|"barrier state"| VALKEY

    SIS -->|"reads build requirements"| WORKSHOP
    SIS -->|"layer: toolbox + roadblock<br/>+ rickshaw engine"| ENGIMG["Engine images"]
    ENGIMG -->|"contain"| BENCH
    ENGIMG -->|"contain"| TOOLS

    BENCH -->|"declare integration via"| RICKSHAW
    TOOLS -->|"declare integration via"| RICKSHAW
    BENCH -.->|"shared metrics API"| TOOLBOX
    TOOLS -.->|"shared metrics API"| TOOLBOX
    RICKSHAW -.->|"post-processing helpers"| TOOLBOX

    PACKRAT -->|"collects sysinfo<br/>inside engines"| ENGIMG

    RICKSHAW -->|"generates CDM documents"| CDM
    CDM -->|"indexes results"| OPENSEARCH
    CDM -->|"query API + dashboard"| User

    CI -.->|"validates changes"| CLI
    CI -.->|"validates changes"| Core
    CI -.->|"validates changes"| Benchmarks
    CI -.->|"validates changes"| Tools

    DEVTOOLS -.->|"manages AGENTS.md"| CLI
    DEVTOOLS -.->|"manages AGENTS.md"| Core

    EXAMPLES -.->|"sample run files"| User
    TESTING -.->|"test documentation"| CI
```

## Reading the diagram

- **Solid arrows** show runtime invocation or data flow during a benchmark run.
- **Dashed arrows** show supporting relationships such as shared libraries,
  validation coverage, or documentation links.
- **Benchmark and tool repos** are collapsed into single nodes for readability;
  each is an independent repository with its own `rickshaw.json`,
  `workshop.json`, and optional `multiplex.json`.
- **Engine images** are built by the image-sourcing service using a layered
  approach: toolbox, roadblock, and the rickshaw engine form cached common
  layers, with the benchmark- or tool-specific layer rebuilt on change.
- **Valkey** is the runtime backend for roadblock barriers; it is a service
  container, not a Crucible repository.
- **OpenSearch** stores indexed CDM documents; the CDM server provides the
  query API and web dashboard on top of it.
