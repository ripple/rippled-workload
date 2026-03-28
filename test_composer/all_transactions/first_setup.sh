#!/usr/bin/env bash
# Runs once before fault injection begins.
# Seeds the workload with deterministic state (trust lines, MPTs, vaults, NFTs, etc.)
# so that parallel drivers have objects to operate on.
curl --silent http://workload:8000/setup
