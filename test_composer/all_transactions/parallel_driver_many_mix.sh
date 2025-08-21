#!/usr/bin/env bash

for i in $(seq 50); do
    curl --silent http://workload:8000/mix >/dev/null 2>&1 &
done
