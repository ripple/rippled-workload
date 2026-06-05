#!/usr/bin/env bash
# Race: Send → Clawback → MergeInbox — 3-way state dependency chain
# Tests post-clawback inbox validity and complex state interactions
curl --silent http://workload:8000/confidential/send/random
curl --silent http://workload:8000/confidential/clawback/random
curl --silent http://workload:8000/confidential/merge_inbox/random
