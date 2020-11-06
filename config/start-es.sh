#!/bin/bash

es_dir="$1"
chown -R elasticsearch:elasticsearch $es_dir
mkdir -p /var/lib/crucible/logs
su -c "/usr/share/elasticsearch/bin/elasticsearch -Epath.data=$es_dir" elasticsearch 2>&1 | tee /var/lib/crucible/logs/elastic.log
