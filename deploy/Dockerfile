FROM python:3.12.14-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY backend ./backend
COPY scenarios ./scenarios
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home c2sim && mkdir -p /data && chown c2sim:c2sim /data
ENV PATH="/app/.venv/bin:$PATH" C2SIM_DATABASE=/data/c2sim.sqlite C2SIM_EXPORT_ROOT=/data/exports
USER c2sim
EXPOSE 8000
CMD ["c2sim", "--database", "/data/c2sim.sqlite", "serve", "--host", "0.0.0.0"]
