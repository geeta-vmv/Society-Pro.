FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY app.py .
COPY static/ ./static/

# Data + logs dir
RUN mkdir -p /data /app/logs

ENV PYTHONUNBUFFERED=1
ENV DB_PATH=/data/societypro.db
ENV CONFIG_PATH=/app/config.yaml

EXPOSE 5000

HEALTHCHECK --interval=20s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/')" || exit 1

CMD ["python3", "app.py"]
