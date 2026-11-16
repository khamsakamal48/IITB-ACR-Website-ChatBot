FROM python:3.12-slim

WORKDIR /app

# curl: compose healthcheck · git: version history of the knowledge bundle (KB_GIT=1)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl git \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory /data/knowledge   # volume owned by host uid

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py knowledge.py okf.py ./

# /data/knowledge = the OKF bundle (mounted volume) · /data/kb-index.sqlite = search index
ENV KB_DIR=/data/knowledge \
    KB_INDEX_DB=/data/kb-index.sqlite \
    KB_GIT=1 \
    HOME=/tmp
# Run as the host user that owns the mounted /data (compose sets user: KB_UID:KB_GID),
# so files written by the service stay editable/rsync-able on the host.
USER 1000:1000
VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
