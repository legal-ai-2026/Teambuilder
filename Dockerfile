FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN python -m pip install --upgrade pip

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install ".[infra]"

COPY AGENT.md ./
COPY assets ./assets
COPY docs ./docs
COPY scripts ./scripts

EXPOSE 8000

CMD ["uvicorn", "system2.api:app", "--host", "0.0.0.0", "--port", "8000"]
