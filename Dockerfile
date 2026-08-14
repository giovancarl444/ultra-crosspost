FROM python:3.12-slim

# Unbuffered so `docker compose logs -f` shows output as it happens rather than in bursts.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MEDIA_DIR=/data/media \
    DATABASE_PATH=/data/crosspost.db

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY assets/ ./assets/

# Run as a non-root user; /data is the only thing it needs to write to.
RUN useradd --create-home --uid 10001 crosspost \
    && mkdir -p /data \
    && chown -R crosspost:crosspost /data /srv
USER crosspost

VOLUME ["/data"]

CMD ["python", "-m", "app"]
