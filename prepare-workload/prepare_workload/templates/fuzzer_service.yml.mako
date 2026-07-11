  ${service_name}:
    image: ${image}
    container_name: ${container_name}
    hostname: ${hostname}
    init: true
    # Enable core dumps for the fuzzer + its isolated xrpld (matches service.yml.mako).
    ulimits:
      core:
        soft: -1
        hard: -1
    entrypoint: ["/bin/bash", "/opt/fuzzer/fuzzer-entrypoint.sh"]
    environment:
      - NUM_REAL_PEERS=${num_real_peers}
    cap_add:
      - NET_ADMIN
    healthcheck:
      test: ["CMD-SHELL", "pgrep -x rippled-fuzzer && pgrep -x xrpld || exit 1"]
      start_period: 10s
      interval: 10s
      timeout: 5s
      retries: 3
    volumes:
      - ${fuzzer_config_volume}:/etc/opt/fuzzer
      - ${xrpld_config_volume}:/etc/opt/xrpld
    networks:
      - ${network_name}
