  ${service_name}:
    image: sidecar:latest
    container_name: ${container_name}
    hostname: ${hostname}
    init: true
    entrypoint: ${entrypoint}
    environment:
      - PYTHONUNBUFFERED=1
    networks:
      - ${network_name}
