FROM python:3.13-slim

# libpq is needed at runtime by psycopg2.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# OSNMAlib (Galileo OSNMA, KU Leuven, EUPL-1.2) is not on PyPI and has no setup.py:
# fetch the pinned release archive and keep only the package and its licence.
ARG OSNMALIB_TAG=v3.2.5
ARG OSNMALIB_SHA256=3aa2cc90b53f47b30441f031bde67ae1ecd4d6b98c093216cfa3b0d7fb1afb01
RUN python - <<EOF
import hashlib, io, tarfile, urllib.request
url = "https://github.com/Algafix/OSNMA/archive/refs/tags/${OSNMALIB_TAG}.tar.gz"
data = urllib.request.urlopen(url, timeout=300).read()
assert hashlib.sha256(data).hexdigest() == "${OSNMALIB_SHA256}", "OSNMAlib archive checksum mismatch"
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    keep = []
    for m in tar.getmembers():
        parts = m.name.split("/", 1)
        if len(parts) == 2 and (parts[1].startswith("osnma/") or parts[1] in ("LICENSE", "Notice.txt")):
            m.name = parts[1]
            keep.append(m)
    tar.extractall("/opt/osnmalib", members=keep, filter="data")
EOF
ENV PYTHONPATH=/opt/osnmalib \
    OSNMA_CACHE_DIR=/var/cache/resqnet-osnma

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
