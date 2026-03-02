#!/bin/sh
set -e

API_KEY="${PROMETHEUS_API_KEY}"

if [ -z "$API_KEY" ]; then
  echo "ERROR: PROMETHEUS_API_KEY not set"
  exit 1
fi

# Use "|" as sed delimiter — API keys (base64) can contain "/"
sed "s|__API_KEY__|${API_KEY}|g" /etc/prometheus/prometheus.yml.tmpl > /tmp/prometheus.yml

# Restrict access — config contains the Bearer token
chmod 600 /tmp/prometheus.yml

echo "Prometheus config generated successfully"

exec /bin/prometheus --config.file=/tmp/prometheus.yml --storage.tsdb.path=/prometheus
