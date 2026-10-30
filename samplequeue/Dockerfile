# Cloud Run serves on $PORT; everything else comes from env vars.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY sources.json seed_demo.py ./

# Cloud Run sets PORT; default for local `docker run`.
ENV PORT=8080
EXPOSE 8080

# One worker, several threads: the workload is IO-bound against Postgres
# and the ingest holds a lot of rows in memory, so more workers would
# multiply footprint without helping.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1
