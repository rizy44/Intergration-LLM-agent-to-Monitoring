FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY metrics-service/ ./metrics-service/

# Run as non-root user
RUN useradd -m appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "metrics-service.app:app", "--host", "0.0.0.0", "--port", "8000"]
