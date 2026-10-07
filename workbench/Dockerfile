FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000 HOST=0.0.0.0 DATA_DIR=/data
WORKDIR /app
RUN useradd --uid 10001 --create-home folio && mkdir /data && chown folio:folio /data
COPY --chown=folio:folio server.py /app/server.py
COPY --chown=folio:folio public /app/public
USER folio
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/api/health',timeout=2)"
CMD ["python", "server.py"]
