FROM python:3.11-slim

# System deps: FFmpeg with full filter support (drawtext, libfreetype, fontconfig)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    fonts-liberation fonts-noto \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy font downloader and run it during build
# This downloads all 28 Google Fonts used by ReelForge templates
COPY download_fonts.py .
RUN python download_fonts.py

COPY . .

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
