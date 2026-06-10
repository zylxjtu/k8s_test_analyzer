#!/usr/bin/env bash
#
# Vacuum the chromadb SQLite file to reclaim space from deleted rows.
#
# When to run:    periodic maintenance (~monthly) or when free disk gets tight.
#                 The chromadb cleanup pass deletes rows but doesn't shrink the
#                 SQLite file; free pages accumulate. VACUUM rewrites the file
#                 to pack live data only.
#
# What it does:   1. Pre-flight checks (sqlite3 present, DB readable, disk free)
#                 2. Reports current size + free-page ratio
#                 3. Stops the MCP server container (writes must be quiesced)
#                 4. Runs SQLite VACUUM (can take 30-60 min on multi-GB files)
#                 5. Reports new size and time elapsed
#                 6. Restarts the container
#
# Safety:         VACUUM is atomic. If killed mid-run, the original DB stays
#                 intact and you can re-run the script.
#
# Override paths via env vars:
#   CHROMADB_PATH     full path to chroma.sqlite3
#   CONTAINER_NAME    docker container name (default: k8s-test-analyzer)
#   YES               set to 1 to skip the confirmation prompt

set -euo pipefail

DB="${CHROMADB_PATH:-/home/azureuser/.k8s-test-analyzer/cache/chroma_db/chroma.sqlite3}"
CONTAINER="${CONTAINER_NAME:-k8s-test-analyzer}"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

color()  { printf "\033[%sm%s\033[0m\n" "$1" "${*:2}"; }
red()    { color "31" "$@"; }
green()  { color "32" "$@"; }
yellow() { color "33" "$@"; }
cyan()   { color "36" "$@"; }

cyan "=== ChromaDB SQLite VACUUM ==="
echo "DB:        $DB"
echo "Container: $CONTAINER"
echo "Project:   $PROJECT_DIR"
echo

# ---------------- pre-flight checks ----------------

if ! command -v sqlite3 >/dev/null 2>&1; then
    red "sqlite3 CLI not installed. On Debian/Ubuntu:"
    echo "  sudo apt-get update && sudo apt-get install -y sqlite3"
    exit 1
fi

if [[ ! -f "$DB" ]]; then
    red "Database file not found: $DB"
    echo "Override with CHROMADB_PATH=/absolute/path/to/chroma.sqlite3"
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    red "docker CLI not found. This script needs to stop/start the container."
    exit 1
fi

# Estimate disk needed: VACUUM rewrites the whole file alongside the original
size_bytes=$(stat -c%s "$DB")
size_gb=$(( size_bytes / 1024 / 1024 / 1024 ))
db_dir=$(dirname "$DB")
free_kb=$(df -k --output=avail "$db_dir" | tail -1)
free_gb=$(( free_kb / 1024 / 1024 ))
need_gb=$(( size_gb + 2 ))   # current + new file + small margin

echo "Current DB size: ${size_gb} GB"
echo "Free disk:       ${free_gb} GB"
echo "Need (approx):   ${need_gb} GB (original + new temp file)"
echo

if [[ "$free_gb" -lt "$need_gb" ]]; then
    red "Not enough free disk for VACUUM. Need ${need_gb} GB, have ${free_gb} GB."
    exit 1
fi

# Report free-page ratio so the user knows how much VACUUM will reclaim
page_size=$(sqlite3 "$DB" "PRAGMA page_size;")
page_count=$(sqlite3 "$DB" "PRAGMA page_count;")
freelist=$(sqlite3 "$DB" "PRAGMA freelist_count;")
pct_free=$(awk "BEGIN { printf \"%.1f\", 100 * $freelist / $page_count }")
free_gb_inside=$(awk "BEGIN { printf \"%.2f\", $freelist * $page_size / 1024 / 1024 / 1024 }")
live_gb_inside=$(awk "BEGIN { printf \"%.2f\", ($page_count - $freelist) * $page_size / 1024 / 1024 / 1024 }")

echo "Free pages inside DB: ${freelist} of ${page_count} (${pct_free}%)"
echo "  Reclaimable:        ${free_gb_inside} GB"
echo "  Live data:          ${live_gb_inside} GB"
echo

# ---------------- confirm ----------------

if [[ "${YES:-0}" != "1" ]]; then
    read -rp "Stop the container and run VACUUM? [y/N] " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        yellow "Aborted."
        exit 0
    fi
fi

# ---------------- run ----------------

cyan ">> Stopping container..."
docker compose --project-directory "$PROJECT_DIR" stop "$CONTAINER"

size_before=$(du -h "$DB" | cut -f1)
cyan ">> Size before: $size_before"

cyan ">> Running VACUUM (do not interrupt — could take 30-60 min)..."
start=$(date +%s)
sqlite3 "$DB" "VACUUM;"
elapsed=$(( $(date +%s) - start ))
mins=$(( elapsed / 60 ))
secs=$(( elapsed % 60 ))

size_after=$(du -h "$DB" | cut -f1)

cyan ">> Restarting container..."
docker compose --project-directory "$PROJECT_DIR" up -d "$CONTAINER"

echo
green "=== Done ==="
echo "Size before: $size_before"
echo "Size after:  $size_after"
echo "Duration:    ${mins}m ${secs}s"
