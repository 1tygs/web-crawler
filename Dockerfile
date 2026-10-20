# syntax=docker/dockerfile:1
#
# The crawler without a local Python: docker build -t web-crawler . && docker run --rm
# web-crawler https://example.com/ > out.txt
#
# Two stages: uv installs the locked dependencies and the crawler into a virtual
# environment, and the image that runs is the same Python image with that environment and
# nothing else, uv included, as a user who isn't root. The base images are pinned by digest;
# Dependabot keeps them current.

FROM ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b AS uv

FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS build
COPY --from=uv /uv /bin/uv
# The image's own Python, bytecode compiled ahead, and files copied out of uv's cache, which
# is a mount of its own.
ENV UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
# The dependencies first, in a layer of their own that a change to the code doesn't rebuild.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# The crawler itself, as a package in the environment rather than a link to the source.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
LABEL org.opencontainers.image.title="web-crawler" \
      org.opencontainers.image.description="A fast, accurate and polite single-host web crawler" \
      org.opencontainers.image.source="https://github.com/1tygs/web-crawler" \
      org.opencontainers.image.licenses="MIT"
# The home is the working directory, and any user the image runs as, with docker run --user,
# must be able to enter it: each parser process starts by changing to the crawler's
# working directory, and dies if it can't. Debian makes new homes 0700.
RUN useradd --system --create-home --key HOME_MODE=0755 --uid 10001 crawler
# The environment's interpreter is a link to the image's, which is the same in both stages.
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
USER crawler
WORKDIR /home/crawler
# The crawler is process 1, and handles SIGINT and SIGTERM itself (DESIGN.md §5.9).
ENTRYPOINT ["crawl"]
