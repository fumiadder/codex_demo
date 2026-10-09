# syntax=docker/dockerfile:1
FROM python:3.12-slim
ARG SOURCE_SHA=local
ARG SCHEMA_HASH=unknown
LABEL org.opencontainers.image.revision=$SOURCE_SHA io.zhixu.schema=$SCHEMA_HASH
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000 HOST=0.0.0.0 DATA_DIR=/data
WORKDIR /app
RUN useradd --uid 10001 --create-home folio && mkdir /data && chown folio:folio /data
COPY requirements.txt /app/requirements.txt
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt
COPY --chown=folio:folio server.py bedtime.py persistence.py media_storage.py storage_jobs.py stories_data.json /app/
COPY --chown=folio:folio public /app/public
COPY --chown=folio:folio scripts/manage_access.py /app/scripts/manage_access.py
USER folio
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=10s --timeout=4s --start-period=5s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/api/health',timeout=3)"
CMD ["python", "server.py"]
