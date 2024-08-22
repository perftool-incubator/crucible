# registries.json
Create a new config file to contain information about the various
container image registries that a Crucible controller may need to
access.

## Problem Description
Currently Crucible contains support for specifying a single image
registry to use as a repository for engine container images.  To go
along with this, there is also support for specifying a single token
file which can be used to push images to the aforementioned repository
and the ability to specify if TLS verification should be performed on
the registry.  The location of the controller image is specified in a
similar manner.  This information is contained in
`/etc/sysconfig/crucible` and looks like this:

```
# cat /etc/sysconfig/crucible
...
CRUCIBLE_CONTROLLER_IMAGE=quay.io/crucible/controller:latest
CRUCIBLE_ENGINE_REPO=quay.io/crucible/client-server
CRUCIBLE_ENGINE_REPO_AUTH_TOKEN="/root/crucible-internal/crucible-client-server-token.json"
CRUCIBLE_ENGINE_REPO_TLS_VERIFY="true"
...
```

While this has been sufficient to support Crucible's need up until
this point, the inability to configure multiple registries with per
registry settings has become a limiting factor with respect to what we
can do when it comes to supporting things like public/private images,
userenv base images that require authentication to pull, etc.

## Proposed change
By designing a image registry configuration file and adopting it's use
across the breadth of the Crucible project we can begin to add support
for multiple registries with per registry configuration settings.  In
order to be consistent with other configuration files that are
currently present in Crucible it seems to make the most sense to use a
JSON file for this purpose since they are quite ubiquitous across the
Crucible project.  By choosing the JSON file format we can also write
a JSON schema (also ubiquitous across the Crucible project) that can
be used to validate the format and input data that exists within this
new configuration file.

In order to be consistent and all for for a more detailed set of
configuration information it also makes sense to include the Crucible
controller image in this configuration file.

### registries.json
The proposed `registries.json` config file would look something like
this:

```
{
  "controller": {
    "url": "quay.io/crucible/controller",
    "tag": "latest",       # since the controller image is being referred to explicitly, include the ability to specify which tag should be used
    "pull-token": "/path/to/optional/controller/pull/token.json",     # at the moment we have no need for pull token for the controller image, but it may be useful/necessary at some point in the future or for certain use cases
    "tls-verify": true
  },
  "engines": {
    "public": {
      "url": "quay.io/crucible/engines",
      "tokens": {
        "push": "/path/to/public/engines/push/token.json"
      },
      "tls-verify": true,
      "quay": {            # optional quay object that is used for quay specific things -- which at this time is all image expiration related (this was in rickshaw-settings.json but it should probably be migrated hear)
        "expiration-length": "2w",
        "refresh-expiration": {
          "token-file": "/path/to/public/engines/refresh/expiration/token.json",
          "api-url": "https://quay.io/api/v1/repository/crucible/client-server"
        }
      }
    },
    "private": {
      "url": "foo.com/crucible/private-engines",
      "tokens": {          # it is entirely possible (even likely?) that the push and pull token files may be the exact same file, but by specifying them separately the option to use separate tokens is preserved (which may be desirable since the pull token will need to be distributed to the endpoints)
        "push": "/path/to/private/engines/push/token.json",
        "pull": "/path/to/private/engines/pull/token.json"
      },
      "tls-verify": true,
      "quay": {            # this could also have an optional quay object as above
        ...
      }
    }
  },
  "userenvs": [            # this array would contain information to be provided to workshop so it would know how to access base userenv images that have controlled access (by matching against the url?)
    {
      "url": "quay.io/project1/distro",
      "pull-token": "/path/to/project1/userenv/pull/token.json",
      "tls-verify": true
    },
    {
      "url": "foo.com/project2/distro",
      "pull-token": "/path/to/project2/userenv/pull/token.json",
      "tls-verify": true
    },
    ...
  ]
}
```
