FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./server/

VOLUME ["/app/data"]
EXPOSE 25774

CMD ["python", "server/app.py", "--host", "0.0.0.0", "--port", "25774", "--db", "/app/data/bigcat.db"]
