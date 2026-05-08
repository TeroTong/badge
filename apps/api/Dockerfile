FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=300

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    libsndfile1 \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY src ./src
COPY tests ./tests

RUN uv sync --frozen --group dev

ENV PATH="/app/.venv/bin:${PATH}"

RUN ASR_RUNTIME_DIR=/opt/asr_runtime \
    THREED_SPEAKER_REPO_PATH=/opt/asr_runtime/3D-Speaker \
    THREED_SPEAKER_MODEL_CACHE_DIR=/opt/asr_runtime/modelscope \
    bash scripts/install_sensevoice_3dspeaker_runtime.sh && \
    rm -rf /opt/asr_runtime/modelscope

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "smart_badge_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
