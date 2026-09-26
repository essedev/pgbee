# The pgbee worker: `docker run pgbee-worker` runs the queue, any other command is passed to pgbee
# (install, status, extension-files). Configuration comes from the environment only.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/essedev/pgbee" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.description="pgbee worker: AI-derived columns for PostgreSQL"
RUN useradd --system --uid 10001 pgbee
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER pgbee
ENTRYPOINT ["pgbee"]
CMD ["run"]
