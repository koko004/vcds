FROM python:3.13-slim

# System deps: tesseract, ghostscript, chromium deps, Xvfb, xdotool
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-spa \
    ghostscript \
    xvfb \
    chromium \
    xdotool \
    xsel \
    xclip \
    fonts-liberation \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browsers
RUN playwright install chromium

# Copy app
COPY . .

# Create needed dirs
RUN mkdir -p uploads originales chrome_profile extension

# Expose
EXPOSE 8000

# Start with Xvfb for Playwright
CMD ["python3", "start_app.py"]
