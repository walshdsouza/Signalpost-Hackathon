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

# Copy project specifications and the pinned dependency set
COPY pyproject.toml requirements.lock ./
COPY uv.lock* ./

# Upgrade pip and install dependencies at the exact verified versions
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -c requirements.lock .

# Copy remaining project source files
COPY . .

# Usage (the batch may be .txt, .jsonl, .json or .csv; out.html is written next to out.json):
#   docker build -t signalpost .
#   docker run --rm -e GEMINI_API_KEY=<key> -v <dir>:/data signalpost /data/input_orgs.jsonl --output /data/out.json
# Without GEMINI_API_KEY the run still returns Brreg facts, verified websites and the
# news/jobs crawl (records flagged "fallback"). With no arguments it prints usage.
ENTRYPOINT ["python", "main.py"]
CMD ["--help"]