#!/bin/bash

tag="${1}"

if [ -z "${tag}" ]; then
    echo "ERROR: You must supply a tag"
    exit 1
fi

source="localhost/workshop/fedora33_crucible-controller"
destination="quay.io/crucible/controller:${tag}"

cmd="buildah push ${source} ${destination}"

echo "Running: ${cmd}"
${cmd}
