# Paperlane — production image (web, worker and backup all use it)
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PAPERLANE_DEBUG=0 PAPERLANE_SCHEDULER=0 PAPERLANE_CACHE=db
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client libcairo2 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 paperlane
WORKDIR /app
COPY requirements.txt requirements-prod.txt ./
RUN pip install -r requirements-prod.txt
COPY . .
RUN PAPERLANE_SECRET_KEY=build-only python manage.py collectstatic --noinput \
    && mkdir -p /app/media /backups && chown -R paperlane /app/media /backups /app/staticfiles
USER paperlane
EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
