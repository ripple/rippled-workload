#!/usr/bin/env bash

# Expected parameter is one of: fatal, error, warn, info, debug, trace
# https://xrpl.org/docs/references/http-websocket-apis/admin-api-methods/logging-and-data-management-methods/log_level/

address=$(docker network inspect config_rippled-net | jq -r '.[0].IPAM.Config[0].Subnet')
address=$(echo "${address%${address##*.}}")
curl ${address}{3,4,5,6,7,8,9}:5005 \
    --silent \
    --data "{\"method\": \"log_level\", \"params\": [{\"severity\" : \"$1\"}]}"
