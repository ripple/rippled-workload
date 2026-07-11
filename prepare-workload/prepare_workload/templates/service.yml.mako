  ${service_name}:
    image: ${image}
    container_name: ${container_name}
    hostname: ${hostname}
    init: true
    # Enable core dumps so a SIGSEGV/SIGABRT leaves a post-mortem core. Validators
    # and the tracking-node peer launch xrpld directly (no entrypoint script), so
    # the ulimit must be set here rather than in fuzzer-entrypoint.sh.
    ulimits:
      core:
        soft: -1
        hard: -1
    % if command:
    command: ${command}
    % endif
    ports:
    % for p in ports:
      - ${p}
    % endfor
    healthcheck:
      test: ["CMD-SHELL", "curl -sf -X POST http://localhost:5005 -H 'Content-Type: application/json' -d '{\"method\":\"ping\"}' -o /dev/null || exit 1"]
      start_period: 10s
      interval: 10s
      timeout: 5s
      retries: 3
    volumes:
    % for v in volumes:
      - ${v}
    % endfor
    networks:
      - ${network_name}
