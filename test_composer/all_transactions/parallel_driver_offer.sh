#!/usr/bin/env bash

cd /opt/antithesis/catalog/tc_commands || exit
default_offers=4
num_offers=${1:-default_offers}
python3 -m create_offer ${offers}
