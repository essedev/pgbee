# The aicol worker: `docker run aicol-worker` runs the queue, any other command is passed to aicol
# (install, status, extension-files). Configuration comes from the environment only.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-bookworm
RUN useradd --system --uid 10001 aicol
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER aicol
ENTRYPOINT ["aicol"]
CMD ["run"]
