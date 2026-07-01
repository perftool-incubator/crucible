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

## remote-archive section
The `remote-archive` section configures remote storage backends for
uploading and downloading result archives via rclone. This enables
backup, sharing, and centralized storage of benchmark results on
services like S3-compatible storage.

- **remotes**: A map of user-defined remote names to backend
  configurations. Each remote has:
  - `type`: The rclone backend type (`s3`)
  - `credentials`: Absolute path to a JSON credential file (format
    varies by backend type, see below)
  - `bucket` (optional, S3 only): The target bucket name
  - `tls-verify` (optional): Whether to verify TLS certificates
    when connecting (default: `true`). Set to `false` for
    self-signed certificates.
  - `settings`: Backend-specific configuration (non-secret settings
    like endpoint URL, region, root folder ID)
- **default**: Name of the default remote (used with `--remote default`),
  or `null` if no default is configured

### Credential file formats

**S3** (`s3-creds.json`):
```json
{
    "access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
}
```

### Example configuration

```json
"remote-archive": {
    "remotes": {
        "team-s3": {
            "type": "s3",
            "credentials": "/root/crucible-internal/s3-creds.json",
            "bucket": "crucible-archives",
            "tls-verify": false,
            "settings": {
                "endpoint": "https://rustfs.example.com:9000",
                "region": "us-east-1"
            }
        }
    },
    "default": "team-s3"
}
```

Managed via `crucible archive config [add|remove|info|default]`.
