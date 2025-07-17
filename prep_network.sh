#!/bin/bash

# Generate the testnet
uvx --from git+https://github.com/legleux/generate_ledger@test_github gen

# build the images
docker build sidecar -t sidecar
docker build . -f Dockerfile.workload -t workload

# update the generated compose file
sed -i '1i \
include:\
  - sidecar/compose.yaml\
  - workload/compose.yaml\
' test_network/docker-compose.yml

sed -i '/^\s*hostname:\s*rippled\s*$/c\
    healthcheck:\
      test: ["CMD", "/usr/bin/curl", "--insecure", "https://localhost:51235/health"]\
      interval: 10s\
      start_period: 45s' test_network/docker-compose.yml

# install the workload and sidecar compose files

test_network_dir="test_network"

for i in sidecar workload; do
    target_dir="${test_network_dir}/${i}"
    mkdir "${target_dir}"
    cp "${i}/compose.yaml" "${target_dir}/compose.yaml"
done
