#!/bin/bash

opensearch_dir="$1"

chown -R opensearch:opensearch ${opensearch_dir}
mkdir -p /var/lib/crucible/logs

echo "network.host: 0.0.0.0" >> /etc/opensearch/opensearch.yml
echo "discovery.type: single-node" >> /etc/opensearch/opensearch.yml
echo "plugins.security.disabled: true" >> /etc/opensearch/opensearch.yml

memtotal_kb=$(grep MemTotal /proc/meminfo | awk '{ print $2 }')
memtotal_mb=$(echo "${memtotal_kb} / 1024" | bc)
heapsize_mb=$(echo "${memtotal_mb} / 2" | bc)
sed -i -e "s/^\(-Xm.\).*/\1${heapsize_mb}m/" /etc/opensearch/jvm.options

su -c "/usr/share/opensearch/bin/opensearch" opensearch 2>&1 \
    | tee /var/lib/crucible/logs/opensearch.log
