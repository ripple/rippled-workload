#!/bin/bash
set -x

TEST_NETWORK_DIR="test_network"
mv ../$TEST_NETWORK_DIR .
mv ../accounts.json $TEST_NETWORK_DIR/

# update the generated compose file
sed -i '1i \
include:\
  - sidecar-compose.yml\
  - workload-compose.yml\
' ${TEST_NETWORK_DIR}/docker-compose.yml

sed -i '/^\s*hostname:\s*rippled\s*$/a\
    healthcheck:\
      test: ["CMD", "/usr/bin/curl", "--insecure", "https://localhost:51235/health"]\
      interval: 10s\
      start_period: 45s' ${TEST_NETWORK_DIR}/docker-compose.yml


# install the workload and sidecar compose files
for i in sidecar workload; do
    echo "copying ${i}/docker-compose.yml to ${TEST_NETWORK_DIR}/${i}-compose.yml"
    cp "${i}/docker-compose.yml" "${TEST_NETWORK_DIR}/${i}-compose.yml"
done

sed -i "s|^\s*image:\s*rippled:latest|    image: ${RIPPLED_IMAGE}|" ${TEST_NETWORK_DIR}/docker-compose.yml
sed -i "s|^\s*image:\s*\$SIDECAR_IMAGE|    image: ${SIDECAR_IMAGE}|" ${TEST_NETWORK_DIR}/sidecar-compose.yml
sed -i "s|^\s*image:\s*\$WORKLOAD_IMAGE|    image: ${WORKLOAD_IMAGE}|" ${TEST_NETWORK_DIR}/workload-compose.yml

ls -l "${TEST_NETWORK_DIR}"
cat "${TEST_NETWORK_DIR}/docker-compose.yml"
cat "${TEST_NETWORK_DIR}/sidecar-compose.yml"
cat "${TEST_NETWORK_DIR}/workload-compose.yml"
