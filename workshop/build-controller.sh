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
    source ./workshop/controller.conf

    export TOOLBOX_HOME=${CRUCIBLE_HOME}/subprojects/core/toolbox
    
    exec ./subprojects/core/workshop/workshop.pl --userenv ./workshop/${controller_userenv_file} --requirements ./workshop/controller-workshop.json --label crucible-controller "$@"
else
    echo "ERROR: Failed to pushd to \$CRUCIBLE_HOME [${CRUCIBLE_HOME}]"
    exit 1
fi
