FROM python:3.11-slim

WORKDIR /app

# Không tạo .pyc và log hiện ngay trong terminal
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Tạo nơi lưu data/state/log
RUN mkdir -p /app/data/articles /app/state /app/output \
    && touch /app/output/last_run.json \
    && ln -sf /app/output/last_run.json /app/last_run.json

# Khai báo các folder nên được persist
VOLUME ["/app/data", "/app/state", "/app/output"]

CMD ["python", "main.py"]