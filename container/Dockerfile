# syntax=docker/dockerfile:1.6
FROM python:3.11-slim

ENV APP_HOME=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1

WORKDIR $APP_HOME

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
RUN chmod -R a+rX ./src
COPY container/start_review_app.sh /usr/local/bin/start-review-app
RUN chmod +x /usr/local/bin/start-review-app

ENV PYTHONPATH=$APP_HOME/src
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/start-review-app"]
