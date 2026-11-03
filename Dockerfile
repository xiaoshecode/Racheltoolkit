FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home teacher && mkdir -p /data/staticfiles && chown -R teacher /data
COPY app /srv/app
COPY deploy/verify_seafile.py /srv/deploy/verify_seafile.py
USER teacher
ENV DJANGO_SETTINGS_MODULE=config.production TEACH_DATA_DIR=/data PYTHONPATH=/srv/app
CMD ["waitress-serve","--listen=0.0.0.0:8000","--threads=12","--max-request-body-size=23068672","config.wsgi:application"]

