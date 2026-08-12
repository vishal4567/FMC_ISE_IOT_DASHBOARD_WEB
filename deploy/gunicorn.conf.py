"""Gunicorn config for the IoT Security Dashboard (RHEL 9)."""
import multiprocessing

bind = "127.0.0.1:8000"
workers = min(4, multiprocessing.cpu_count() * 2 + 1)
threads = 2
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = "info"
