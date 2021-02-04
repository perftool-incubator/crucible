#!/bin/bash

cdm_dir="$1"
rc=1
if [ -e $cdm_dir ]; then
    pushd $cdm_dir >/dev/null
    ./init.sh
    rc=$?
    popd >/dev/null
else
    echo "ERROR: $cdm_dir not found"
fi
exit $rc
