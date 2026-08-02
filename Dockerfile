# syntax=docker/dockerfile:1

# Slim rather than Alpine: aiohttp and pydantic-core are compiled, and glibc/manylinux wheels are
# guaranteed where musllinux ones are not. On Alpine this risks building both from source on every
# architecture.
ARG PYTHON_IMAGE=python:3.13-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32

# The dev stage is the toolchain container: every make target runs inside it, so uv, python, pytest,
# ruff and mypy never have to exist on the host.
FROM ${PYTHON_IMAGE} AS dev

# git: the release-version checker's tests exercise it against throwaway repositories, and the
# pre-commit hook it backs needs `git tag` to compare against published releases.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /usr/local/bin/

# The venv deliberately lives outside /app: the source is bind mounted over /app and would
# otherwise hide it.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

CMD ["bash"]


FROM ${PYTHON_IMAGE} AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# --no-editable so the package is copied into the venv rather than linked back at /app/src, which
# lets the runtime stage take the venv and nothing else.
RUN uv sync --frozen --no-dev --no-editable


FROM ${PYTHON_IMAGE} AS runtime

# gosu to drop privileges after the entrypoint has fixed ownership; tzdata so TZ means something.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POLAR_TOKENS=/tokens \
    WATCH_DIR=/watch \
    STATE_DIR=/state \
    HTTP_ADDR=0.0.0.0:8080

RUN mkdir -p /watch /state /tokens

# 8080 is the local listener; 8081 is the conventional WEBHOOK_ADDR port, which stays closed unless set.
EXPOSE 8080 8081

# The connector answers its own healthcheck, so the image needs no curl.
HEALTHCHECK --interval=1m --timeout=10s --start-period=30s --retries=3 \
    CMD ["/opt/venv/bin/dreeve-polar-connector", "healthcheck"]

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["run"]
