FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=America/Guatemala \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "scheduler.py"]
