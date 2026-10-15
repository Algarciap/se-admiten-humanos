FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY se_admiten_humanos ./se_admiten_humanos
RUN pip install --no-cache-dir .

CMD ["se-admiten-humanos", "run-worker"]
