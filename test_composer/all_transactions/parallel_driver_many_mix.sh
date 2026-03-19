#!/usr/bin/env bash

for i in $(seq 50); do
    curl --silent http://workload:8000/txn/random >/dev/null 2>&1 &
done
