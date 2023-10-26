#!/bin/bash

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
    THE_DATE=$(date +%Y-%m-%d)
    THE_ARCH=$(uname -m)
    THE_COMMIT=$(git rev-parse HEAD)

    TAG="${THE_DATE}_${THE_COMMIT}_${THE_ARCH}"

    if [ -z "${THE_DATE}" -o -z "${THE_ARCH}" -o -z "${THE_COMMIT}" ]; then
	echo "ERROR: Could not determine proper tag [${TAG}]"
	exit 1
    fi

    source ./workshop/controller.conf

    source="localhost/workshop/${controller_userenv_label}_crucible-controller"
    destination="${controller_repo}:${TAG}"

    cmd="buildah push ${source} ${destination}"

    echo "Running: ${cmd}"
    ${cmd}
else
    echo "ERROR: Failed to pushd to \$CRUCIBLE_HOME [${CRUCIBLE_HOME}]"
    exit 1
fi
