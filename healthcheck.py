#!/usr/bin/env python3
"""Docker health check - verifies indexing heartbeat is recent.

This script checks if the .indexing_heartbeat file has been updated recently.
If the heartbeat is stale (older than MAX_AGE seconds), it returns exit code 1,
indicating the container is unhealthy and should be restarted.

Used by Docker's HEALTHCHECK directive to detect hung indexing processes.
"""
import os
import sys
import time

HEARTBEAT_FILE = os.path.join(
    os.getenv("PROJECTS_ROOT", "/projects"),
    ".indexing_heartbeat"
)
MAX_AGE = int(os.getenv("HEALTHCHECK_MAX_AGE", "300"))  # 5 minutes default

try:
    if not os.path.exists(HEARTBEAT_FILE):
        # No heartbeat yet - allow during startup
        sys.exit(0)

    age = time.time() - os.path.getmtime(HEARTBEAT_FILE)
    if age > MAX_AGE:
        print(f"Heartbeat stale: {age:.0f}s old (max: {MAX_AGE}s)")
        sys.exit(1)

    sys.exit(0)
except Exception as e:
    print(f"Health check error: {e}")
    sys.exit(1)
