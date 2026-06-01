#!/usr/bin/env bash
# Race: Double MergeInbox back-to-back — idempotency check (inbox double-counted?)
curl --silent http://workload:8000/confidential/merge_inbox/random
curl --silent http://workload:8000/confidential/merge_inbox/random
