  ${service_name}:
    image: ${image}
    container_name: ${container_name}
    hostname: ${hostname}
    % if command:
    command: ${command}
    % endif
    ports:
    % for p in ports:
      - ${p}
    % endfor
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:5005 -o /dev/null || exit 1"]
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
