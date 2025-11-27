  ${name}:
    image: python:3
    container_name: ${name}
    hostname: ${name}
    ports:
      - 80:3001
    entrypoint: ["python3", "/app.py"]
    volumes:
      - ./${unl_file}:/${unl_file}
      - ./app.py:/app.py
    networks:
      - ${network_name}
