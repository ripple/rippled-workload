FROM ghcr.io/astral-sh/uv:0.9-python3.13-trixie AS base

ENV PYTHONUNBUFFERED=1
WORKDIR /workload
RUN apt-get update && apt-get install -y curl vim jq
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV UV_TOOL_BIN_DIR=/usr/local/bin

# Install dependencies in a separate layer
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=workload/uv.lock,target=uv.lock \
    --mount=type=bind,source=workload/pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Copy the project into the image
COPY workload/ /workload

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "workload.app:app", "--host", "0.0.0.0", "--port", "8000"]
