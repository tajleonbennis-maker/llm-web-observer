FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LWO_DATABASE=/data/observer.db

WORKDIR /app
COPY pyproject.toml README.md ./
COPY services/collector/llm_web_observer ./services/collector/llm_web_observer
RUN pip install --no-cache-dir .

RUN useradd --system --uid 10001 observer && mkdir -p /data && chown observer:observer /data
USER observer
EXPOSE 8080
VOLUME ["/data"]

CMD ["uvicorn", "llm_web_observer.app:app", "--host", "0.0.0.0", "--port", "8080"]

