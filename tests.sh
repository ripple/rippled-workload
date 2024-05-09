#!/usr/bin/env bash

if [ $# -eq 0 ]; then
  TESTS=rippled_automation/rippled_end_to_end_scenarios/end_to_end_tests/payment_test.py::test_xrp_simple_payment
else
  TESTS=$@
fi

docker run --rm -it \
  -v $(realpath config/volumes/workload):/root \
  -w /root/auto \
  --network config_rippled-net \
  --name tmp_workload \
  workload:antithesis /usr/local/bin/pytest --hostname rippled --port 5005 $TESTS
