#!/bin/bash
set -x

TEST_NETWORK_DIR="test_network"
mv accounts.json $TEST_NETWORK_DIR/
# update the generated compose file
sed -i '1i \
include:\
  - sidecar/docker-compose.yml\
  - workload/docker-compose.yml\
' ${TEST_NETWORK_DIR}/docker-compose.yml

sed -i '/^\s*hostname:\s*rippled\s*$/a\
    healthcheck:\
      test: ["CMD", "/usr/bin/curl", "--insecure", "https://localhost:51235/health"]\
      interval: 10s\
      start_period: 45s' ${TEST_NETWORK_DIR}/docker-compose.yml

sed -i "s|^\s*image:\s*rippled:latest|    image: ${RIPPLED_IMAGE}|" ${TEST_NETWORK_DIR}/docker-compose.yml
sed -i "s|^\s*image:\s*\$SIDECAR_IMAGE|    image: ${SIDECAR_IMAGE}|" ${TEST_NETWORK_DIR}/sidecar/docker-compose.yml
sed -i "s|^\s*image:\s*\$WORKLOAD_IMAGE|    image: ${WORKLOAD_IMAGE}|" ${TEST_NETWORK_DIR}/workload/docker-compose.yml

# install the workload and sidecar compose files
for i in sidecar workload; do
    target_dir="${TEST_NETWORK_DIR}/${i}"
    echo "making ${target_dir}"
    mkdir "${target_dir}"
    echo "copying ${i}/docker-compose.yml to ${target_dir}/docker-compose.yml"
    cp "${i}/docker-compose.yml" "${target_dir}/docker-compose.yml"
done

ls -l test_network
