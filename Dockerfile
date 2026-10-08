# syntax=docker/dockerfile:1

FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md LICENSE ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.14-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Tokyo \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=builder /app /app
RUN useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app
ENTRYPOINT ["hb-st-sync"]
CMD ["sync"]
