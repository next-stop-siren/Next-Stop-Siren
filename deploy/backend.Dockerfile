FROM ghcr.io/astral-sh/uv:0.11.19 AS uv
FROM python:3.13.15-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-managed-python
COPY backend/app ./app
COPY deploy/backend-entrypoint.py /usr/local/bin/backend-entrypoint.py
RUN useradd --uid 10001 --create-home app
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app"
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "/usr/local/bin/backend-entrypoint.py"]
# Do not trust arbitrary forwarded headers; #74 remains a separate acceptance gate.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers", "--no-access-log", "--timeout-graceful-shutdown", "10"]
