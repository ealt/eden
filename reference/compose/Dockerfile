# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.12.7
ARG UV_VERSION=0.5.4

FROM python:${PYTHON_VERSION}-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
# git is required by eden_git's subprocess wrapper (orchestrator,
# implementer, integrator, web-ui). curl is used by container
# healthchecks; ca-certificates supports HTTPS pulls in setup
# scripts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 1000 --create-home --shell /bin/bash eden

FROM base AS builder
ARG UV_VERSION
RUN pip install --no-cache-dir uv==${UV_VERSION}
WORKDIR /app
# Copy the workspace layout first so dependency layers cache before
# source code. hatchling needs the per-member READMEs at build
# time, so we copy the full workspace tree.
COPY pyproject.toml uv.lock ./
COPY reference/packages reference/packages
COPY reference/services reference/services
# `--all-packages` installs every workspace member; without it the
# eden-* packages would be skipped because the root `pyproject.toml`
# is non-installable (`tool.uv.package = false`) and the workspace
# members are pulled in only via the `dev` group, which `--no-dev`
# excludes.
RUN uv sync --frozen --no-dev --all-packages

FROM base AS runtime
WORKDIR /app
COPY --from=builder /app /app
# Pre-create the volume mount points with eden ownership. Docker
# initializes a fresh named volume from whatever is at the mount
# path in the image, so directories that don't exist here would
# get root-owned by the volume driver and fail every subsequent
# write the eden user attempts.
RUN mkdir -p /var/lib/eden/repo /var/lib/eden/artifacts \
    && chown -R eden:eden /var/lib/eden
# Make uv-installed venv binaries discoverable.
ENV PATH="/app/.venv/bin:${PATH}"
USER eden
# No ENTRYPOINT — compose.yaml's `command:` selects the service
# module per-container.
