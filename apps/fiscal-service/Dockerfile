FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
RUN useradd --system --uid 10001 fiscal && mkdir -p /data /run/secrets/fiscal \
    && chown -R fiscal:fiscal /data /run/secrets/fiscal
USER fiscal
ENV PYTHONPATH=/app/src FISCAL_STATE_DB=/data/fiscal-state.sqlite3
EXPOSE 8081
CMD ["uvicorn", "fiscal_service.app:app", "--host", "0.0.0.0", "--port", "8081", "--no-access-log"]
