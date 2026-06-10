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
COPY cleanup_worker.py .
COPY healthcheck.py .

# Pre-download the embedding model to /app/.cache (accessible by any UID)
ENV HF_HOME=/app/.cache/huggingface
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" \
    && chmod -R a+rX /app/.cache

# Allow non-root users to add passwd entries (sentence-transformers calls getpwuid)
RUN chmod a+w /etc/passwd

# Expose the FastMCP port
EXPOSE ${FASTMCP_PORT}

# Entrypoint: ensure current UID has a passwd entry (sentence-transformers calls getpwuid)
# then run the MCP server
CMD ["sh", "-c", "if ! getent passwd $(id -u) >/dev/null 2>&1; then echo \"appuser:x:$(id -u):$(id -g)::/app:/bin/sh\" >> /etc/passwd; fi && python mcp_server.py"]
