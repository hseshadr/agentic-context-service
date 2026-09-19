#!/bin/sh
set -eu

url="${ACS_OPENSEARCH_URL:-http://opensearch:9200}"
template="/contracts/index-template.json"
memory_template="/contracts/memory-index-template.json"

curl --fail --silent --show-error -X PUT \
  -H 'Content-Type: application/json' \
  --data-binary "@${template}" \
  "${url}/_index_template/context-institutional-v1" >/dev/null

curl --fail --silent --show-error -X PUT \
  -H 'Content-Type: application/json' \
  --data-binary "@${memory_template}" \
  "${url}/_index_template/context-memory-v1" >/dev/null

if ! curl --fail --silent "${url}/_alias/context-institutional-read" >/dev/null 2>&1; then
  curl --fail --silent --show-error -X PUT \
    -H 'Content-Type: application/json' \
    --data '{"aliases":{"context-institutional-read":{},"context-institutional-write":{"is_write_index":true}}}' \
    "${url}/context-institutional-v1-000001" >/dev/null
fi

if ! curl --fail --silent "${url}/_alias/context-memory-read" >/dev/null 2>&1; then
  curl --fail --silent --show-error -X PUT \
    -H 'Content-Type: application/json' \
    --data '{"aliases":{"context-memory-read":{},"context-memory-write":{"is_write_index":true}}}' \
    "${url}/context-memory-v1-000001" >/dev/null
fi

echo "OpenSearch template and aliases are ready."
