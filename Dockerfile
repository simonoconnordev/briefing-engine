# One image for both processes: `briefing api` (the default) and `briefing worker`.
# The same image runs locally under compose and, in stage 2's AWS step, on ECS Fargate.

FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim
RUN useradd --system --uid 10001 --no-create-home briefing
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    API_HOST=0.0.0.0 \
    LOG_FORMAT=json
USER briefing
EXPOSE 8080
ENTRYPOINT ["briefing"]
CMD ["api"]
