FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Bake admin/static assets into the image for WhiteNoise to serve.
RUN python manage.py collectstatic --noinput

# Run as a non-root user; pre-create the volume mount points it must write to.
RUN useradd --create-home appuser \
    && mkdir -p /app/data /app/media \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENTRYPOINT ["sh", "docker-entrypoint.sh"]
# Shell form so ${PORT} (injected by the host, e.g. Sevalla) expands at runtime;
# falls back to 8000 for local docker. The entrypoint runs this via exec "$@".
CMD gunicorn config.wsgi:application --bind "0.0.0.0:${PORT:-8000}" --workers 3
