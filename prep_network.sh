#!/bin/bash
set -x

test_network_dir="${TEST_NETWORK_DIR:-testnet}"
mv "../${test_network_dir}" .

sed -i '/^\s*hostname:\s*rippled\s*$/a\
    healthcheck:\
      test: ["CMD", "/usr/bin/curl", "--insecure", "https://localhost:51235/health"]\
      interval: 10s\
      start_period: 45s' ${test_network_dir}/docker-compose.yml


# install the workload and sidecar compose files
for i in sidecar workload; do
    echo "copying ${i}/docker-compose.yml to ${test_network_dir}/${i}-compose.yml"
    cp "${i}/docker-compose.yml" "${test_network_dir}/${i}-compose.yml"
done

sed -i "s|^\s*image:\s*rippled:latest|    image: ${RIPPLED_IMAGE}|" ${test_network_dir}/docker-compose.yml
sed -Ei '1,/^[[:space:]]*entrypoint:[[:space:]]*\["rippled"\]/! s|^[[:space:]]*entrypoint:[[:space:]]*\["rippled"\]|    entrypoint: ["sh", "-c", "sleep 15 \&\& rippled --net"]|' ${test_network_dir}/docker-compose.yml
sed -i "s|^\s*image:\s*\$SIDECAR_IMAGE|    image: ${SIDECAR_IMAGE}|" ${test_network_dir}/sidecar-compose.yml
sed -i "s|^\s*image:\s*\$WORKLOAD_IMAGE|    image: ${WORKLOAD_IMAGE}|" ${test_network_dir}/workload-compose.yml

ls -l "${test_network_dir}"
cat "${test_network_dir}/docker-compose.yml"
cat "${test_network_dir}/sidecar-compose.yml"
cat "${test_network_dir}/workload-compose.yml"
