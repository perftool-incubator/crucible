# services.json
A configuration file containing settings for all services that a
Crucible controller interacts with.

# Problem description
Crucible makes use of different services like OpenSearch, httpd, and
image sourcing. Previously, OpenSearch instance configuration was
maintained in a separate `instances.json` file. This created a split
configuration surface where some services were in `services.json` and
OpenSearch instances were elsewhere.

# Current structure
The `services.json` config file contains the following sections:

```json
{
    "cdm-server": {
        "port": 3000
    },
    "httpd": {
        "port": 8080
    },
    "image-sourcing": {
        "use": true,
        "services": {
            "x86_64": {
                "start": true,
                "location": {
                    "address": "localhost",
                    "port": 8888,
                    "protocol": "http"
                }
            }
        }
    },
    "opensearch": {
        "instances": [
            {
                "name": "local-v9",
                "host": "localhost:9200",
                "cdmver": "v9dev"
            }
        ],
        "index-to": "local-v9",
        "query-from": ["local-v9"]
    },
    "valkey": {
        "monitor": {
            "enabled": false
        }
    }
}
```

## opensearch section
The `opensearch` section replaces the former `config/instances.json`
file. It defines OpenSearch instances and controls which instances are
used for indexing and querying benchmark results.

- **instances**: Array of OpenSearch instance objects, each with:
  - `name`: Unique identifier for the instance
  - `host`: Hostname and port (e.g., `localhost:9200`)
  - `cdmver`: Common Data Model version (e.g., `v8dev`, `v9dev`)
  - `userpass` (optional): User:password credentials for authentication
- **index-to**: Name of the default instance for indexing new results
- **query-from**: List of instance names available for querying

Managed via `crucible opensearch [add|remove|update|info|...]`.
