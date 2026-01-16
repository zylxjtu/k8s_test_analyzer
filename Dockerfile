FROM python:3.11-slim

# Accept port as build argument
ARG FASTMCP_PORT=8978

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (for better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY k8s_testlog_downloader/ ./k8s_testlog_downloader/
COPY mcp_server.py .
COPY core.py .
COPY local_indexing.py .
COPY healthcheck.py .

# Expose the FastMCP port
EXPOSE ${FASTMCP_PORT}

# Default command - run integrated server
CMD ["python", "mcp_server.py"]
