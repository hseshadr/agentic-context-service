FROM ghcr.io/astral-sh/uv:0.8.22 AS uv

FROM python:3.13.7-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md LICENSE ./
ENV PATH="/app/.venv/bin:$PATH"

FROM runtime AS demo-runtime
RUN uv sync --frozen --no-dev --extra demo --no-install-project
ENV HF_HOME=/opt/huggingface \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1
RUN HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 python -c \
    "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" \
    && chmod -R a+rX /opt/huggingface

FROM demo-runtime AS api
RUN uv sync --frozen --no-dev --extra cdc --extra demo --extra showcase --extra agent --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra cdc --extra demo --extra showcase --extra agent --no-editable
USER 65532:65532
EXPOSE 8080
CMD ["uvicorn", "agentic_context_service.bootstrap:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]

FROM demo-runtime AS indexer
RUN uv sync --frozen --no-dev --extra cdc --extra demo --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra cdc --extra demo --no-editable
USER 65532:65532
CMD ["python", "-m", "agentic_context_service.indexer"]
