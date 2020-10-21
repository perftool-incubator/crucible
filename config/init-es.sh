#!/bin/bash

cdm_dir="$1"
rc=1
if [ -e $cdm_dir ]; then
    pushd $cdm_dir
    ./init.sh
    rc=$?
    popd
else
    echo "ERROR: $cdm_dir not found"
fi
exit $rc
