#!/bin/bash

MANIFEST_TAG=$1

if [ -z "${MANIFEST_TAG}" ]; then
    echo "ERROR: You must supply a manifest tag to create"
    exit 1
fi

if [ ! -e /etc/sysconfig/crucible ]; then
    echo "ERROR: /etc/sysconfig/crucible does not exist"
    exit 1
else
    source /etc/sysconfig/crucible
fi

if [ -z "${CRUCIBLE_HOME}" ]; then
    echo "ERROR: \$CRUCIBLE_HOME not defined"
    exit 1
fi

if pushd ${CRUCIBLE_HOME} > /dev/null; then
    THE_COMMIT=$(git rev-parse HEAD)

    source ./workshop/controller.conf

    cmd="podman search --list-tags --no-trunc ${controller_repo}"

    echo "Running: ${cmd}"
    IMAGES=$(${cmd} | grep ${THE_COMMIT} | awk '{ print $1":"$2 }')

    echo "Found images:"
    for IMAGE in ${IMAGES}; do
	echo ${IMAGE}
    done

    local_manifest="localhost/controller-manifest:${MANIFEST_TAG}"

    if podman manifest exists ${local_manifest}; then
	podman manifest rm ${local_manifest}
    fi

    podman manifest create ${local_manifest}
    for IMAGE in ${IMAGES}; do
	podman manifest add ${local_manifest} docker://${IMAGE}
    done
    podman manifest push ${local_manifest} ${controller_repo}:${MANIFEST_TAG}
else
    echo "ERROR: Failed to pushd to \$CRUCIBLE_HOME [${CRUCIBLE_HOME}]"
    exit 1
fi
