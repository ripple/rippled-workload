# working on workload in docker
# image="ghcr.io/astral-sh/uv:0.8.9-python3.13-bookworm"
image="workload_test"
docker build . -f Dockerfile.workload.v2 -t workload_test

# ensure the accounts created in the starting ledger are available to the workload #TODO: figure out a better way to do this?
# the workload does not need the ledger.json file (at the moment...)
cd workload

docker run --rm -it \
    --name workload_test \
    -e RIPPLED_NAME=rippled \
    -e VALIDATOR_NAME=val \
    -e NUM_VALIDATORS=5 \
    --network antithesis_net \
    --publish 8000:8000 \
    --volume ./accounts.json:/accounts.json \
    --volume .:/opt/antithesis/catalog/workload \
    --volume /opt/antithesis/catalog/workload/.venv \
    $image bash
