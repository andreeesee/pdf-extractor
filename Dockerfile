FROM python:3.11-slim

# Install Poppler and Tesseract
RUN apt-get update && apt-get install -y \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-eng \
    curl \
 && rm -rf /var/lib/apt/lists/*
# Set workdir
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY app.py .

# Expose port
EXPOSE 7860

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:7860", "--workers", "2", "--worker-class", "sync", "--timeout", "90", "--log-level", "info", "--access-logfile", "-", "--error-logfile", "-", "--capture-output"]
