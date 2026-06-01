#!/usr/bin/env bash
# Race: Send uses spending balance that hasn't been merged yet (stale CB_S_Version)
curl --silent http://workload:8000/confidential/send/random
curl --silent http://workload:8000/confidential/merge_inbox/random
