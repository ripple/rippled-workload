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
      test: ["CMD", "rippled", "--silent", "ping"]
      start_period: 10s
      interval: 10s
    volumes:
    % for v in volumes:
      - ${v}
    % endfor
    networks:
      - ${network_name}
