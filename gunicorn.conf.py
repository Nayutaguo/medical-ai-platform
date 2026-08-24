"""Gunicorn defaults for the local product WSGI service."""

import os


bind = os.getenv("GUNICORN_BIND", "127.0.0.1:8000")
workers = int(os.getenv("GUNICORN_WORKERS", "2"))
timeout = int(os.getenv("GUNICORN_TIMEOUT_SECONDS", "150"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT_SECONDS", "30"))
keepalive = int(os.getenv("GUNICORN_KEEPALIVE_SECONDS", "5"))
accesslog = "-"
errorlog = "-"
