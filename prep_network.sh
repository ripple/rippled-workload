#!/bin/bash
set -x

TEST_NETWORK_DIR="test_network"
mv ../$TEST_NETWORK_DIR .
mv ../accounts.json $TEST_NETWORK_DIR/

# install the workload and sidecar compose files
for i in workload; do
    echo "copying ${i}/docker-compose.yml to ${TEST_NETWORK_DIR}/${i}-compose.yml"
    cp "${i}/docker-compose.yml" "${TEST_NETWORK_DIR}/${i}-compose.yml"
done

sed -i "s|^\s*image:\s*\$WORKLOAD_IMAGE|    image: ${WORKLOAD_IMAGE}|" ${TEST_NETWORK_DIR}/workload-compose.yml

mv ${TEST_NETWORK_DIR}/workload-compose.yml ${TEST_NETWORK_DIR}/docker-compose.yml
ls -l "${TEST_NETWORK_DIR}"
cat "${TEST_NETWORK_DIR}/docker-compose.yml"
