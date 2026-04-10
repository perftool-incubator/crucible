#!/bin/bash
# -*- mode: sh; indent-tabs-mode: nil; sh-basic-offset: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=bash
#
# Wrapper for backwards compatibility. Calls controller-image.py build.

SCRIPT_DIR=$(dirname "$(readlink -e "$0")")
exec "${SCRIPT_DIR}/controller-image.py" build "$@"
