FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv/orbit

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY migrations migrations
COPY app app
COPY core core
COPY aggregators aggregators
COPY paystubs paystubs
COPY property property
COPY jobs jobs

RUN useradd --uid 1000 --create-home orbit \
    && mkdir -p /srv/orbit/data \
    && chown orbit:orbit /srv/orbit/data
USER orbit
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["sh", "-c", "alembic upgrade head && exec python -m app.serve"]
