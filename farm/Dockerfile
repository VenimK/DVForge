FROM python:3.11-slim

WORKDIR /app

COPY farm/ /app/
COPY web/ /app/web/
COPY builder/config_gen.py /app/builder/config_gen.py

RUN mkdir -p /app/farm

EXPOSE 8766

CMD ["python3", "queue.py", "--host", "0.0.0.0", "--port", "8766", "--farm", "/app/farm"]