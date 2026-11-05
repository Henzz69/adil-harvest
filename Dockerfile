FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# state dir is a volume on fly.io (see fly.toml) so data survives restarts
ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "adil_bot.py"]
