# services.json
Create a new config file to contain information about the various
services that a Crucible controller may need to interact with.

# Problem description
Crucible makes use of different services like OpenSearch. Today 
Crucible assumes that some of the services are local to the node
running the Crucible controller. In some cases users will want to 
use a long-lived OpenSearch instance versus the local one that 
Crucible deploys. 

# Proposed change
Introduce a `services.json` which will have the details of the 
services that Crucible depends on. 

### services.json
The proposed `services.json` config file would look something like
this:

```json
{
    "opensearch": {
        "url": "https://my-opensearch",
        "port": 443
    }
}
```

If we need to embed user/password creds, the user can update the
opensearch.url to: 

```json
{
    "opensearch": {
        "url": "https://my-user:12345@my-opensearch",
        "port": 443
    }
}
```