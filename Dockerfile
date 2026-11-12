FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app
RUN useradd --create-home --uid 10001 w36

COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code stays root-owned and read-only for the runtime user.
COPY alembic.ini pyproject.toml ./
COPY migrations ./migrations
COPY window36 ./window36
COPY app ./app
COPY .streamlit ./.streamlit

USER w36
EXPOSE 8501
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=6 \
  CMD python -c "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4); sys.exit(0 if r.read() == b'ok' else 1)"
CMD ["streamlit", "run", "app/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
