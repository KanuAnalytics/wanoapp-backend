FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y \\
    gcc \\
    libffi-dev \\
    libssl-dev \\
    build-essential \\
    ffmpeg \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first (this line seems to be missing in your slim version)
COPY requirements.txt .

# Install Python dependencies
RUN --mount=type=cache,target=/root/.cache/pip \\
    pip install -r requirements.txt

# Copy the rest of the application
COPY . .

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \\
  CMD wget -qO- http://127.0.0.1:8000/health || exit 1

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]