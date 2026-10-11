# Container image for the FastAPI service only.
# The EC2/systemd deployment (deploy/) is unchanged; this is an additional way to run the service.
FROM python:3.12-slim

# Skip .pyc files and flush logs immediately so `docker logs` is current.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so this layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser

# Copy only the application code the service needs.
COPY --chown=appuser:appuser service/ ./service/

USER appuser

EXPOSE 8000

# Python is already in the image, so no curl is needed for the health check.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

# Bind to 0.0.0.0 inside the container; the host decides what to publish (e.g. -p 127.0.0.1:18000:8000).
CMD ["python", "-m", "uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8000"]
