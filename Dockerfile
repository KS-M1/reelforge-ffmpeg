FROM python:3.11-slim

# System deps: FFmpeg (static), fontconfig for fc-cache, liberation fonts as fallback
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget xz-utils ca-certificates \
    fontconfig \
    fonts-liberation fonts-noto \
    && wget -q https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
    && tar xf ffmpeg-release-amd64-static.tar.xz \
    && mv ffmpeg-*-amd64-static/ffmpeg /usr/local/bin/ffmpeg \
    && mv ffmpeg-*-amd64-static/ffprobe /usr/local/bin/ffprobe \
    && rm -rf ffmpeg-*.tar.xz ffmpeg-*-amd64-static \
    && apt-get purge -y wget xz-utils \
    && apt-get autoremove -y \
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
