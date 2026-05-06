FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Base image ships Chromium for the image's pinned Playwright version. If the
# Python SDK from pip resolves to a newer release, that release expects a
# different browser build dir. Install whichever Chromium matches the SDK we
# actually have, so launch() finds it regardless of base-image alignment.
RUN playwright install chromium

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
