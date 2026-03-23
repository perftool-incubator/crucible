#!/bin/bash
# -*- mode: sh; indent-tabs-mode: nil; sh-basic-offset: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=bash

log_base="$1"

python3 -m source_images_service.main 2>&1 \
    | tee ${log_base}/logs/image-sourcing.log
