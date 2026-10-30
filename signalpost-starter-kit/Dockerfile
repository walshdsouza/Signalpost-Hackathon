# Use an official lightweight Python image
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy project specifications and lockfiles
COPY pyproject.toml .
COPY uv.lock* ./

# Upgrade pip and install package dependencies
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir .

# Copy remaining project source files
COPY . .

# Usage: docker run -v <dir>:/data <image> /data/input_orgs.txt --output /data/out.json
ENTRYPOINT ["python", "main.py"]
CMD ["batch_1000.txt"]