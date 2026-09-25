FROM python:3.11-slim

WORKDIR /app

COPY . .

EXPOSE 8766

CMD ["python3", "queue.py", "--host", "0.0.0.0", "--port", "8766", "--farm", "/app/farm"]