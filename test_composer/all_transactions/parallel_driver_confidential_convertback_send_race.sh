#!/usr/bin/env bash
# Race: ConvertBack entire balance then Send — stale balance reference
curl --silent http://workload:8000/confidential/convert_back/random &
curl --silent http://workload:8000/confidential/send/random &
wait
