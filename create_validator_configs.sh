#!/usr/bin/env bash

set -o errexit
set +o xtrace
set -o nounset

num_keys=${1:-5}
num_services=$((num_keys + 1))
rippled_image="rippled_antithesis:latest"
confs_dir="$PWD/config/volumes"
conf_file="rippled.cfg"
validator_name="atval"
rippled_name="atrippled"
network_name="${rippled_name}-net"
peer_port=51235
# Only need to define "network" on macOS. You may need to change this if it
# conflicts with your local network.
macos="true" # antithesis for now
# if [ $(uname -s) = "Darwin" ]; then
#     macos="true"
    network="10.20.20"
# fi
set +e
#################################################
# NOTE: The [features] list will need to be     #
# modified to suit the version of rippled used  #
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
  { "command": "log_level", "severity": "warning" }

[features]
  AMM
  CheckCashMakesTrustLine
  Checks
  Clawback
  DeepFreeze
  DeletableAccounts
  DepositAuth
  DepositPreauth
  DID
  DisallowIncoming
  EnforceInvariants
  Escrow
  Flow
  FlowCross
  FlowSortStrands
  HardenedValidations
  MultiSignReserve
  NegativeUNL
  NonFungibleTokensV1
  PayChan
  PriceOracle
  RequireFullyCanonicalSig
  SortedDirectories
  TicketBatch
  TickSize
  XChainBridge
  XRPFees

  fix1201
  fix1368
  fix1373
  fix1512
  fix1513
  fix1515
  fix1523
  fix1528
  fix1543
  fix1571
  fix1578
  fix1623
  fix1781
  fixAmendmentMajorityCalc
  fixCheckThreading
  fixInnerObjTemplate
  fixMasterKeyAsRegularKey
  fixNFTokenNegOffer
  fixNFTokenReserve
  fixNonFungibleTokensV1_2
  fixPayChanRecipientOwnerDir
  fixQualityUpperBound
  fixRmSmallIncreasedQOffers
  fixSTAmountCanonicalize
  fixTakerDryOfferRemoval
  fixUniversalNumber

[ssl_verify]
  0

[compression]
  0

[peer_private]
  0

#[validation_quorum]
#

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
for i in $(seq $num_keys); do
    valname="${validator_name}${i}"
    gen=""
    if [ "$i" -eq "1" ]; then
        gen=', "--start"'
        extra=${healthcheck}
    else
        extra="${depends_on}"
    fi

    if [ $macos = "true" ]; then
        macos_address="ipv4_address: ${network}.$((i + 1))"
    fi
    tee -a "${compose_file}" << EOF
  ${valname}:
    image: ${rippled_image}
    container_name: ${valname}
    hostname: ${valname}
    entrypoint: ["rippled"${gen}]
    init: true
    ${extra}
    volumes:
      - ./volumes/${valname}:/etc/opt/ripple
    networks:
      ${network_name}:
        ${macos_address}

EOF
done
rippled_ip_address=""
if [ "$macos" = "true" ]; then
    k=$((num_keys + 2))
    rippled_ip_address="ipv4_address: ${network}.$((k))"
fi

tee -a "${compose_file}" <<-EOF
  ${rippled_name}:
    hostname: ${rippled_name}
    init: true
    image: ${rippled_image}
    container_name: ${rippled_name}
    entrypoint: ["rippled"]
    ${depends_on}
    volumes:
      - ./volumes/${rippled_name}:/etc/opt/ripple
    networks:
      ${network_name}:
        ${rippled_ip_address}
EOF
echo -e "    ${rippled_ports}\n" >> "${compose_file}"

## Add the workload service
tee -a "${compose_file}" <<-EOF
  workload:
    hostname: workload
    init: true
    image: workload:latest
    container_name: workload
    volumes:
      - ./volumes/tc:/opt/antithesis/test/v1/
    environment:
      RIPPLED_NAME: ${rippled_name}
    networks:
      ${network_name}:
        ipv4_address: ${network}.$((num_keys + 3))
EOF

## Define the network
tee -a "${compose_file}" <<-EOF

networks:
  ${network_name}:
    name: ${network_name}
EOF

if [ $macos == "true" ]; then
    tee -a "${compose_file}" <<-EOF
    # driver: bridge should be the default?
    driver: bridge
    ipam:
      config:
        - subnet: "${network}.0/24"
          # gateway: ${network}.1
EOF
fi
