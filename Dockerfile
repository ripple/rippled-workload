FROM ghcr.io/astral-sh/uv:0.9-python3.13-trixie

ENV \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/antithesis/catalog/workload/.venv/bin:/opt/antithesis/test/v1/all_transactions:$PATH"

WORKDIR /opt/antithesis/catalog/workload

RUN apt-get update && apt-get install -y curl vim jq

# Install dependencies in a separate layer
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=workload/pyproject.toml,target=pyproject.toml \
    uv sync \
        --no-install-project

# Copy the project into the image
ADD workload /opt/antithesis/catalog/workload

# Sync the project
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync

COPY test_composer/all_transactions /opt/antithesis/test/v1/all_transactions
EXPOSE 8000
# CMD ["workload"]
CMD ["tail", "-f", "/dev/null"]
# CMD ["uvicorn", "workload.app:app", "--host", "0.0.0.0", "--port", "8000",
#      "--reload", "--reload-dir", "/opt/antithesis/catalog/workload/src/workload"]
