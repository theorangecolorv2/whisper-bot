FROM python:3.11-slim

WORKDIR /app

# Install ffmpeg, curl and other dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install deno for yt-dlp JavaScript runtime
RUN curl -fsSL https://deno.land/install.sh | sh
ENV PATH="/root/.deno/bin:${PATH}"

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Always get latest yt-dlp (YouTube changes API frequently)
# curl_cffi enables browser impersonation for TikTok
RUN pip install --no-cache-dir --upgrade yt-dlp curl_cffi

# Copy bot code
COPY bot.py .

# Create data directory
RUN mkdir -p /app/data/downloads

CMD ["python", "bot.py"]
