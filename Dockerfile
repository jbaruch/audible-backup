FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir audible-cli

WORKDIR /app
COPY backup.py /app/

ENTRYPOINT ["python3", "/app/backup.py"]
