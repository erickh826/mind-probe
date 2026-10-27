FROM python:3.12-slim

# FFmpeg is required for MediaRecorder chunk remux (spec §10).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements.txt mirrors pyproject.toml runtime dependencies (including
# boto3). uv.lock pins the same set for `uv sync`; the image installs this
# file so a plain pip build can open the S3 client.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import boto3"

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
