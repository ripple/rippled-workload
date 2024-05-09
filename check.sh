#!/usr/bin/env bash

address=$(docker network inspect config_rippled-net | jq -r '.[0].IPAM.Config[0].Subnet')
address=$(echo "${address%${address##*.}}")
curl ${address}{3,4,5,6,7,8,9}:5005 \
    --silent \
    --data '{"method": "server_info"}' | jq -r '.result.info | {hostid, build_version, complete_ledgers, server_state, uptime, peers, validated_ledger, last_close, closed_ledger}'
