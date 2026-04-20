FROM python:3.11-slim

# System deps: FFmpeg with full filter support (drawtext, libfreetype, fontconfig)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    fonts-liberation fonts-noto \
    ca-certificates \
    imagemagick \
    libpango1.0-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all fonts (Google Fonts + licensed fonts) — bundled directly, no runtime download
COPY custom_fonts/ /usr/local/share/fonts/reelforge/
RUN fc-cache -f

COPY . .

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
