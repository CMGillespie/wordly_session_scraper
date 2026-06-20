FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV HEADLESS=true
CMD ["python", "-u", "wordly_session_scraper_v1_0_CLOUD.py"]
