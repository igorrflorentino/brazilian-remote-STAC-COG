FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# GDAL tuning for remote COGs read through HTTP range requests.
# CPL_MACHINE_IS_GCE makes /vsigs/ take credentials from the metadata server
# without relying on GDAL's GCE hardware detection inside the Cloud Run sandbox.
ENV CPL_MACHINE_IS_GCE=YES \
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif \
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES=YES \
    GDAL_HTTP_MULTIPLEX=YES \
    VSI_CACHE=TRUE \
    VSI_CACHE_SIZE=50000000 \
    GDAL_CACHEMAX=256

WORKDIR /app

# rasterio wheels link against the system libexpat, which the slim image lacks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py .
# Fail the build, not the Cloud Run revision, if a native library is missing.
RUN python -c "import app"

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8080

# Shell form so ${PORT} expands; exec so uvicorn receives SIGTERM from Cloud Run.
# --proxy-headers makes WMTS tile URLs use https behind the Cloud Run proxy.
CMD exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips="*"
