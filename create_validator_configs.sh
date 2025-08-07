#!/usr/bin/env bash

set -o errexit
set +o xtrace
set -o nounset

num_keys=${NUM_VALIDATORS:-5}
num_services=$((num_keys + 1))
rippled_image="${RIPPLED_IMAGE:-rippled:latest}"
workload_image="${WORKLOAD_IMAGE:-workload:latest}"
sidecar_image="${SIDECAR_IMAGE:-sidecar:latest}"
confs_dir="$PWD/config/volumes"
conf_file="rippled.cfg"
validator_name="atval"
rippled_name="atrippled"
network_name="${rippled_name}-net"
peer_port=51235

set +e
#################################################
# NOTE: The [features] list will need to be     #
# modified to suit the version of rippled used  #
#                                               #
# NOTE: This list must to be periodically       #
# updated to match actual enabled amendments    #
#################################################

read -r -d '' config_template <<-EOF
[server]
  port_rpc_admin_local
  port_peer
  port_ws_admin_local

[port_rpc_admin_local]
  port = 5005
  ip = 0.0.0.0
  admin = [0.0.0.0]
  protocol = http

[port_peer]
  port = ${peer_port}
  ip = 0.0.0.0
  protocol = peer

[port_ws_admin_local]
  port = 6005
  ip = 0.0.0.0
  admin = [0.0.0.0]
  protocol = ws

[node_db]
  type = NuDB
  path = /var/lib/rippled/db/nudb

[ledger_history]
  full

[database_path]
  /var/lib/rippled/db

[debug_logfile]
  /var/log/rippled/debug.log

[node_size]
  huge

[beta_rpc_api]
  1

[rpc_startup]
  { "command": "log_level", "severity": "info" }

[features]
  fixReducedOffersV2
  fixNFTokenPageLinks
  AMMClawback
  fixEnforceNFTokenTrustline
  NFTokenMintOffer
  fixInnerObjTemplate2
  PriceOracle
  fixPreviousTxnID
  fixAMMv1_1
  fixEmptyDID
  fixAMMOverflowOffer
  fixNFTokenReserve
  fixInnerObjTemplate
  fixFillOrKill
  fixDisallowIncomingV1
  DID
  AMM
  Clawback
  fixReducedOffersV1
  fixNFTokenRemint
  fixTrustLinesToSelf
  DisallowIncoming
  ImmediateOfferKilled
  XRPFees
  fixNonFungibleTokensV1_2
  fixUniversalNumber
  fixRemoveNFTokenAutoTrustLine
  NonFungibleTokensV1_1
  ExpandedSignerList
  CheckCashMakesTrustLine
  NegativeUNL
  fixRmSmallIncreasedQOffers
  fixSTAmountCanonicalize
  FlowSortStrands
  TicketBatch
  HardenedValidations
  fix1781
  fixAmendmentMajorityCalc
  RequireFullyCanonicalSig
  fixQualityUpperBound
  fixPayChanRecipientOwnerDir
  fixCheckThreading
  DeletableAccounts
  fixMasterKeyAsRegularKey
  fixTakerDryOfferRemoval
  MultiSignReserve
  fix1578
  fix1515
  DepositPreauth
  fix1571
  fix1623
  fix1543
  DepositAuth
  fix1513
  Checks
  FlowCross
  Flow

[ssl_verify]
  0

[compression]
  0

[peer_private]
  0

#[validation_quorum]

EOF
set -x
if [ ! -d "${confs_dir}" ]; then
    mkdir -p "${confs_dir}"
fi

generate_validator_token() {
  docker run --rm rippleci/validator_keys_tool | sed '/^[^[]/s/^/  /' > "${conf_dir}/key_${i}"
}

# Generate the keys
declare -a pkeys

for i in $(seq $num_services); do
    if (($i - $num_services)); then
        dir_name="${validator_name}${i}"
        sig_sup="false"
    else
        dir_name="${rippled_name}"
        sig_sup="true"
    fi
    read -r -d '' signing_support <<-EOF
[signing_support]
  $sig_sup
EOF
    conf_dir="${confs_dir}/${dir_name}"
    mkdir "${conf_dir}"

    if [ $i -lt $num_services ]; then
        generate_validator_token "${conf_dir}" $i
    fi

    conf_file_path="${conf_dir}/${conf_file}"
    echo -e "${config_template}\n" | tee "${conf_file_path}"
    echo -e "${signing_support}\n" | tee -a "${conf_file_path}"

    set -x
    if [ $i -lt $((num_keys + 1)) ]; then
        tail -n+3 "${conf_dir}/key_${i}" >> "${conf_file_path}"
        pkey_string=$(head -n1 config/volumes/${validator_name}${i}/key_${i})
        pkey=${pkey_string##*" "}
        pkeys+=("${pkey}")
        rm "${conf_dir}/key_${i}"
    fi

    printf "[ips_fixed]\n" >> "${conf_dir}/rippled.cfg"
    for j in $(seq "${num_keys}"); do
        if [ $i -ne $j ]; then # Does this matter?
          echo "  ${validator_name}${j} ${peer_port}" >> "${conf_dir}/rippled.cfg"
        fi
    done

done

for i in $(seq $num_keys); do
  dir_name="${validator_name}${i}"
  conf_dir="${confs_dir}/${dir_name}"
  printf "\n[validators]\n" >> "${conf_dir}/rippled.cfg"
  for key in "${pkeys[@]}"; do
        echo "  $key" >> "${conf_dir}/rippled.cfg"
  done
done

conf="${confs_dir}/${rippled_name}/rippled.cfg"
printf "\n[validators]\n" >> "${conf}"
for key in "${pkeys[@]}"; do
      echo "  $key" >> "${conf}"
done

############################
###  Write compose file  ###
############################
compose_file="config/docker-compose.yml"
echo "services:" >> ${compose_file}

set +e
read -r -d '' healthcheck <<-EOF
    healthcheck:
      test: ["CMD", "/usr/bin/curl", "--insecure", "https://localhost:${peer_port}/health"]
      interval: 5s
EOF

read -r -d '' depends_on <<-EOF
    depends_on:
      ${validator_name}1:
        condition: service_healthy
EOF

read -r -d '' rippled_ports << EOF

    ports:
      - 0.0.0.0:5005:5005
      - 0.0.0.0:6005:6005
EOF
set -e

# Write the services to the compose file
sidecar_entrypoint='["python", "sidecar.py", "-t", "3", "-v"'
for i in $(seq $num_keys); do
    valname="${validator_name}${i}"
    sidecar_entrypoint+=", \"${valname}\""
    gen=""
    if [ "$i" -eq "1" ]; then
        gen=', "--start"'
        extra=""
    else
        extra="${depends_on}"
    fi
    tee -a "${compose_file}" << EOF
  ${valname}:
    image: ${rippled_image}
    container_name: ${valname}
    hostname: ${valname}
    entrypoint: ["rippled"${gen}]
    init: true
    ${healthcheck}
    ${extra}
    volumes:
      - ./volumes/${valname}:/etc/opt/ripple
    networks:
      ${network_name}:

EOF
done
rippled_ip_address=""
sidecar_entrypoint+="]"

tee -a "${compose_file}" <<-EOF
  ${rippled_name}:
    hostname: ${rippled_name}
    init: true
    image: ${rippled_image}
    container_name: ${rippled_name}
    entrypoint: ["rippled"]
    ${healthcheck}
    ${depends_on}
    volumes:
      - ./volumes/${rippled_name}:/etc/opt/ripple
    networks:
      ${network_name}:
        # ${rippled_ip_address}
EOF
echo -e "    ${rippled_ports}\n" >> "${compose_file}"

## Add the workload service
tee -a "${compose_file}" <<-EOF
  workload:
    hostname: workload
    init: true
    image: ${workload_image}
    container_name: workload
    environment:
      RIPPLED_NAME: ${rippled_name}
      VALIDATOR_NAME: ${validator_name}
      NUM_VALIDATORS: ${NUM_VALIDATORS}
    depends_on:
      ${validator_name}${NUM_VALIDATORS}:
        condition: service_healthy
      ${rippled_name}:
        condition: service_healthy
    networks:
      ${network_name}:

EOF

## Add the sidecar service
tee -a "${compose_file}" <<-EOF
  sidecar:
    hostname: sidecar
    image: ${sidecar_image}
    container_name: sidecar
    entrypoint: ${sidecar_entrypoint}
    depends_on:
      workload:
        condition: service_started
    networks:
      ${network_name}:
EOF

## Define the network
tee -a "${compose_file}" <<-EOF

networks:
  ${network_name}:
    name: ${network_name}
EOF
