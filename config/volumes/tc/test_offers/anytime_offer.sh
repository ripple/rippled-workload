#!/usr/bin/env bash

cd /opt/antithesis/test/v1 || exit
default_offers=20
num_offers=${1:-default_offers}
python3 -m create_offer ${offers}
