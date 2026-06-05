#!/usr/bin/env bash
# Race: Multiple sends to same receiver — inbox accumulation race
curl --silent http://workload:8000/confidential/send/random &
curl --silent http://workload:8000/confidential/send/random &
curl --silent http://workload:8000/confidential/send/random &
wait
