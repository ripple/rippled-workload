#!/usr/bin/env bash
# Race: Clawback during MergeInbox — TOCTOU on inbox vs spending balance
curl --silent http://workload:8000/confidential/clawback/random &
curl --silent http://workload:8000/confidential/merge_inbox/random &
wait
