FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    UPLOAD_DIR=/data/uploads

WORKDIR /app

RUN groupadd --system dassiedrop \
    && useradd --system --gid dassiedrop --create-home --home-dir /home/dassiedrop dassiedrop \
    && mkdir -p /app /data/uploads \
    && chown -R dassiedrop:dassiedrop /app /data

COPY app.py VERSION ./
COPY assets ./assets
COPY templates ./templates

VOLUME ["/data"]
EXPOSE 8000

USER dassiedrop

CMD ["python3", "app.py"]
