FROM ghcr.io/astral-sh/uv:0.9-python3.13-trixie AS base

ENV PYTHONUNBUFFERED=1
WORKDIR /workload
# Some convenient things
RUN apt-get update && apt-get install -y curl vim jq
# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1
# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy
# Ensure installed tools can be executed out of the box
ENV UV_TOOL_BIN_DIR=/usr/local/bin

# Install dependencies in a separate layer
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=workload/uv.lock,target=uv.lock \
    --mount=type=bind,source=workload/pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Copy the project into the image
COPY workload/ /workload

EXPOSE 8000

# Dev target (used by devcontainer)
FROM base AS dev
CMD ["sleep", "infinity"]

# Run target (used in Antithesis)
FROM base AS run
# Sync the project
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev
CMD ["uv", "run", "uvicorn", "workload_app.main:app", "--host", "0.0.0.0", "--port", "8000"]
