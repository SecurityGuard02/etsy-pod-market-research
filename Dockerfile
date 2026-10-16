FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    JARVIS_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=secret,id=build_ca \
    if [ -s /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi \
    && pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 jarvis \
    && useradd --uid 10001 --gid jarvis --no-create-home jarvis \
    && mkdir /data \
    && chown jarvis:jarvis /data

COPY --chown=jarvis:jarvis jarvis ./jarvis
RUN cat > /usr/local/bin/jarvis-start.py <<'PY'
import os
from pathlib import Path

uid, gid = 10001, 10001
data = Path(os.environ.get("JARVIS_DATA_DIR", "/data")).resolve()
data.mkdir(parents=True, exist_ok=True)
if os.geteuid() == 0:
    # Hosted disks are mounted after the image is built and may start root-owned.
    # Change ownership only inside this data directory, never through symlinks.
    os.chown(data, uid, gid)
    for directory, subdirectories, filenames in os.walk(data, followlinks=False):
        for name in subdirectories + filenames:
            entry = Path(directory) / name
            if entry.is_symlink():
                continue
            current = entry.stat()
            if (current.st_uid, current.st_gid) != (uid, gid):
                os.chown(entry, uid, gid, follow_symlinks=False)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
os.umask(0o077)
if not os.access(data, os.W_OK):
    raise SystemExit("JARVIS_DATA_DIR must be writable by uid 10001.")
os.execvp("uvicorn", ["uvicorn", "jarvis.app:app", "--host", "0.0.0.0",
                     "--port", os.environ.get("PORT", "8080"), "--proxy-headers"])
PY
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c 'import os, urllib.request; urllib.request.urlopen("http://127.0.0.1:" + os.environ.get("PORT", "8080") + "/healthz", timeout=4).close()'
CMD ["python", "/usr/local/bin/jarvis-start.py"]
