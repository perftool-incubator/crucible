#!/bin/bash

es_dir="$1"
chown -R elasticsearch:elasticsearch $es_dir
su -c "/usr/share/elasticsearch/bin/elasticsearch -Epath.data=$es_dir" elasticsearch
