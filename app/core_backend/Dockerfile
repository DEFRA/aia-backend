# syntax=docker/dockerfile:1
ARG PARENT_VERSION=latest-3.14
ARG PYTHON_VERSION=3.14.5
ARG PORT=8086

# ---------------------------------------------------------------------------
# Development stage — includes uv and dev dependencies
# ---------------------------------------------------------------------------
FROM defradigital/python-development:${PARENT_VERSION} AS development

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*
USER nonroot

ENV PATH="/home/nonroot/.venv/bin:/home/nonroot/.local/bin:${PATH}"

WORKDIR /home/nonroot

COPY --chown=nonroot:nonroot --chmod=0444 pyproject.toml .

RUN --mount=type=cache,target=/home/nonroot/.cache/uv,uid=1000,gid=1000 \
    uv lock && uv sync --locked --no-build

COPY --chown=nonroot:nonroot --chmod=0555 src/ ./src/

ARG PORT=8086
ENV PORT=${PORT} \
    PYTHONPATH=/home/nonroot/src

EXPOSE ${PORT}

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["/bin/sh", "-c", "/home/nonroot/.venv/bin/uvicorn api.main:app --host 0.0.0.0 --port \"${PORT}\""]

# ---------------------------------------------------------------------------
# Production stage — lean image, no dev tools
# ---------------------------------------------------------------------------
FROM defradigital/python:${PARENT_VERSION} AS production

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*
USER nonroot

ENV PATH="/home/nonroot/.venv/bin:${PATH}"

WORKDIR /home/nonroot

COPY --from=development /home/nonroot/pyproject.toml .
COPY --from=development /home/nonroot/uv.lock .
COPY --from=development /home/nonroot/src ./src/

# NOTE: All runtime secrets (DB_*, JWT_SECRET, AWS_*, etc.) are injected
# by the ECS task definition at container start via SSM/Secrets Manager —
# never at image build time.
RUN --mount=type=cache,target=/home/nonroot/.cache/uv,uid=1000,gid=1000 \
    --mount=from=development,source=/home/nonroot/.local/bin/uv,target=/home/nonroot/.local/bin/uv \
    uv sync --locked --compile-bytecode --no-dev --no-build

ARG PORT=8086
ENV PORT=${PORT} \
    PYTHONPATH=/home/nonroot/src

EXPOSE ${PORT}

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["/bin/sh", "-c", "/home/nonroot/.venv/bin/uvicorn api.main:app --host 0.0.0.0 --port \"${PORT}\""]