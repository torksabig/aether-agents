# Aether agents — run on AWS (App Runner / ECS Fargate / EC2)
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip && pip install .

# Default: hosted chat API (aether-web). Override CMD for the queue worker.
EXPOSE 3847
ENV PORT=3847

# Secrets at runtime via AWS: NOTION_TOKEN, OPENROUTER_API_KEY or Bedrock creds,
# plus NOTION_*_DATA_SOURCE_ID values from config/notion_command_center.json
CMD ["aether-web"]
