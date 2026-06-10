# Kubernetes Test Analyzer

A Python tool for downloading, indexing, and analyzing Kubernetes test logs from TestGrid. Features semantic search powered by ChromaDB and LlamaIndex, with both CLI and MCP (Model Context Protocol) server interfaces for AI-assisted test analysis.

## Features

- 📥 Download test logs from TestGrid and GCS (Google Cloud Storage)
- 📊 Parse JUnit XML test results
- 🔍 Semantic search over logs using ChromaDB and LlamaIndex
- 🤖 MCP integration for Claude Desktop and Claude Code and vscode
- 🖥️ CLI that mirrors MCP tools for testing and standalone use
- 🐳 Docker support for easy deployment

## Quick Start (MCP Server)

### Running the MCP Server

The MCP server provides AI assistants with tools to analyze Kubernetes test logs:

```bash
# Run with Docker (recommended)
./generate-env.sh
docker compose build
DOCKER_UID=$(id -u) DOCKER_GID=$(id -g) docker compose up -d

# Shutdown
docker compose down

# Or run directly (requires Python setup - see Development section)
python mcp_server.py
```

Connect via Streamable HTTP: `http://localhost:8978/mcp`

The server uses MCP's **stateless streamable-http** transport by default. Idle clients don't see errors when the server restarts (no persistent stream to break). To opt back into the legacy SSE transport, set `FASTMCP_TRANSPORT=sse` in `.env` — the endpoint then becomes `http://localhost:8978/sse`.

### Configuring Claude Code

To use the MCP server with Claude Code CLI:

1. Ensure the MCP server is running (see above)

2. Add the server to Claude Code:
```bash
claude mcp add --scope user --transport http k8s-test-analyzer http://localhost:8978/mcp
```

3. Verify the connection:
```bash
claude mcp list
```

You should see `k8s-test-analyzer` listed and connected.

4. Restart your Claude Code session if needed to load the new MCP tools.

**Note**: If running on a remote server, replace `localhost` with your server's IP address or hostname.

### Configuring Claude Desktop

To use the MCP server with Claude Desktop, add this to your Claude Desktop configuration file:

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "k8s-test-analyzer": {
      "url": "http://localhost:8978/mcp",
      "transport": "http"
    }
  }
}
```

Then restart Claude Desktop.

### Configuring VS Code / GitHub Copilot

VS Code supports MCP servers for use with GitHub Copilot agent mode. To add this MCP server:

1. Open VS Code Settings (JSON) or create/edit `~/.vscode-server/data/User/mcp.json` (remote) or the equivalent local path

2. Add the server configuration:
```json
{
  "servers": {
    "k8s-test-analyzer": {
      "url": "http://localhost:8978/mcp",
      "type": "http"
    }
  }
}
```

3. Reload VS Code to connect to the MCP server

For more options including workspace-level configuration, see the [VS Code MCP documentation](https://code.visualstudio.com/docs/copilot/customization/mcp-servers#_other-options-to-add-an-mcp-server).

**Note**: MCP support in VS Code/Copilot is evolving. Check the documentation for the latest configuration options.

### MCP Tools Available

All MCP tools are **read-only** and do not trigger downloads or indexing. Data must be downloaded via CLI or the scheduled background task.

| Tool | Description |
|------|-------------|
| `list_recent_builds` | List recent builds for a tab |
| `list_dashboard_tabs` | List tabs in the configured dashboard |
| `get_tab_status` | Get test results for latest build of tabs specified |
| `search_log` | Semantic search over indexed logs (filters by build_id, defaults to latest) |
| `compare_build_logs` | Compare logs between two builds (same-job or cross-job) |
| `find_regression` | Find and compare last pass with first fail from cached builds |
| `get_index_status` | Get indexing status (all tabs, specific tab, or specific build) |
| `get_test_failures` | Get parsed JUnit test failures with SIG/Feature grouping |
| `list_log_files` | List log files in a build (requires tab and build_id) |
| `get_log_file` | Get specific log file content (requires tab, build_id, and filename) |

**Note:** To download and index new builds, use the CLI commands (`download`, `download-all`) or let the scheduled background task handle it automatically.

### Environment Variables

#### Core configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `FASTMCP_PORT` | `8978` | MCP server port |
| `FASTMCP_TRANSPORT` | `streamable-http` | Transport: `streamable-http` (default) or `sse` (legacy) |
| `FASTMCP_STATELESS_HTTP` | `true` | Stateless mode — no session IDs, survives restarts cleanly |
| `PROJECTS_ROOT` | `${HOME}/.k8s-test-analyzer/cache` | Root directory for projects to index |
| `DEFAULT_DASHBOARD` | `sig-windows-signal` | Default TestGrid dashboard |
| `FOLDERS_TO_INDEX` | (auto-discover) | Comma-separated folders to index |
| `ADDITIONAL_IGNORE_DIRS` | | Extra directories to ignore |
| `ADDITIONAL_IGNORE_FILES` | `containerd.log` | Extra filenames to skip during indexing |
| `MAX_INDEX_FILE_SIZE_MB` | `20` | Skip log files larger than this during indexing (OOM guard) |
| `SCHEDULE_INTERVAL_SECONDS` | `3600` | Scheduled download/index/cleanup interval (0 to disable) |
| `CLEANUP_KEEP_BUILDS` | `3` | Builds to keep per job during cleanup (0 to disable) |
| `HEALTHCHECK_MAX_AGE` | `300` | Heartbeat staleness threshold for Docker healthcheck (seconds) |

#### Lifecycle / restart triggers

ChromaDB 1.x doesn't unload collections from RAM once touched, so the long-lived MCP server accumulates resident state over time. These knobs let the process exit cleanly so Docker (with `restart: unless-stopped`) respawns it with a fresh memory state:

| Variable | Default | Description |
|----------|---------|-------------|
| `TZ` | `America/Los_Angeles` | Timezone used for the daily restart-hour check |
| `RESTART_AT_HOUR` | `2` | Hour-of-day (0–23, local time) for the daily restart. -1 disables. |
| `MAX_LIFETIME_SECONDS` | `604800` (7d) | Backstop: exit if the process has been running this long. 0 disables. |

#### Memory diagnostics (default off)

Enable these temporarily to instrument the running container. They are env-controlled so you don't have to edit code or rebuild the image:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_MONITOR_INTERVAL_SECONDS` | `0` (off) | Log RSS every N seconds. Try `10` while investigating. |
| `TRACE_MEMORY` | `false` | Also enable `tracemalloc`; log top Python allocators at each phase. |

---

## Development

This section covers installation, CLI usage, configuration, and architecture for developers.

### Installation

#### Using a Virtual Environment (Recommended)

```bash
# Create and activate a Python 3.11+ virtual environment
python3.11 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Upgrade pip and setuptools
pip install --upgrade pip setuptools

# Install the package in editable mode
pip install -e .

# Install dependencies for the MCP server and indexing
pip install -r requirements.txt
```

#### Dependency Management

The `requirements.txt` file contains pinned versions of direct dependencies:
- ✅ **Exact versions** for reproducible builds
- ✅ **Direct dependencies only** (pip resolves transitive dependencies automatically)
- ✅ **Readable and maintainable** organization by category
- ✅ **Docker-friendly** (avoids system packages)

To regenerate with all transitive dependencies pinned:
```bash
pip freeze > requirements.txt
```

#### Managing the Virtual Environment

```bash
# Activate the virtual environment
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Verify you're using the correct Python
python --version  # Should show Python 3.11.x

# Deactivate when done
deactivate
```

### CLI Commands

The CLI provides commands that mirror the MCP server tools, allowing you to test functionality without running the MCP server.

#### Command Reference

##### Indexing Commands (Mirror MCP Tools)

| Command | Description | MCP Tool Equivalent |
|---------|-------------|---------------------|
| `download` | Download and index test logs | `download_test` |
| `download-all` | Download and index all tabs | `download_all_latest` |
| `search` | Semantic search over indexed logs | `search_log` |
| `compare` | Compare logs between two builds | `compare_build_logs` |
| `find-regression` | Find and compare last pass with first fail | `find_regression` |
| `failures` | Get parsed JUnit test failures | `get_test_failures` |
| `list-logs` | List log files in a build | `list_log_files` |
| `get-log` | Get specific log file content | `get_log_file` |
| `reindex` | Re-index a specific project | `reindex_folder` |
| `reindex-all` | Re-index all cached folders | `reindex_all` |
| `index-stats` | Get indexing status for builds | `get_index_status` |
| `cleanup` | Clean up old builds, keeping N most recent | (scheduled task) |

##### Data Retrieval Commands

| Command | Description |
|---------|-------------|
| `fetch` | Fetch test data from a tab (without indexing) |
| `list-tabs` | List available tabs for a dashboard |
| `list-builds` | List recent builds for a job |
| `summary` | Get dashboard summary (passing/failing/flaky tabs) |
| `status` | Get test results for latest build of each tab |

#### Usage Examples

```bash
# Download and index test logs from a specific tab (latest build)
k8s-test-analyzer download --tab capz-windows-1-33-serial-slow

# Download and index a specific build
k8s-test-analyzer download --tab capz-windows-1-33-serial-slow --build 1234567890123

# Download and index all tabs from a dashboard
k8s-test-analyzer download-all

# Search indexed logs (searches latest cached build by default)
k8s-test-analyzer search "timeout error" --tab capz-windows-1-33-serial-slow

# Search within a specific build
k8s-test-analyzer search "timeout error" --tab capz-windows-1-33-serial-slow --build-id 1234567890123

# Check index status of latest build for a specific tab
k8s-test-analyzer index-stats --tab capz-windows-1-33-serial-slow

# Check index status of a specific build
k8s-test-analyzer index-stats --tab capz-windows-1-33-serial-slow --build 2009123456789

# Check index status for latest build of all tabs (default)
k8s-test-analyzer index-stats

# List available tabs for a dashboard
k8s-test-analyzer list-tabs

# List recent builds for a tab
k8s-test-analyzer list-builds --tab capz-windows-1-33-serial-slow

# Get dashboard summary (shows passing/failing tabs)
k8s-test-analyzer summary

# Get test status for all tabs (fetches latest build for each)
k8s-test-analyzer status

# Clean up old builds (dry run to preview)
k8s-test-analyzer cleanup --dry-run

# Clean up old builds, keeping only 5 most recent per job
k8s-test-analyzer cleanup --keep 5

# Compare logs between two builds of the same job
k8s-test-analyzer compare --tab capz-windows-1-33-serial-slow --build-a 123456 --build-b 789012

# Compare latest builds between two different jobs
k8s-test-analyzer compare --tab-a capz-windows-1-33-serial-slow --tab-b capz-windows-1-34-serial-slow

# Find regression point (last pass vs first fail) from cached builds
k8s-test-analyzer find-regression --tab capz-windows-1-33-serial-slow

# Find regression with more builds and custom filter
k8s-test-analyzer find-regression --tab capz-windows-1-33-serial-slow --max-builds 20 --filter errors

# Get parsed JUnit test failures grouped by SIG
k8s-test-analyzer failures --tab capz-windows-1-33-serial-slow

# Get failures for a specific build
k8s-test-analyzer failures --tab capz-windows-1-33-serial-slow --build 1234567890

# Re-index a specific project (required after schema changes)
k8s-test-analyzer reindex ci-kubernetes-e2e-capz-1-33-windows-serial-slow

# Re-index all cached projects
k8s-test-analyzer reindex-all
```

### Docker Deployment

```bash
# Generate .env file from template (expands ${HOME} and other variables)
./generate-env.sh

# Build and run
docker compose build
DOCKER_UID=$(id -u) DOCKER_GID=$(id -g) docker compose up -d

# View logs
docker compose logs -f
```

### Maintenance

#### Periodic SQLite VACUUM

The chromadb cleanup pass `DELETE`s old build rows from `chroma.sqlite3` but SQLite doesn't shrink the file on its own — free pages accumulate. After a few months of churn the file is typically ~20–30% bloat. To reclaim that disk space:

```bash
./vacuum-chromadb.sh
```

The script stops the container, reports the live-vs-free byte breakdown, runs `VACUUM`, restarts the container, and prints the size delta. Run it monthly or whenever disk usage gets uncomfortable.

VACUUM is atomic — safe to interrupt; the original file stays intact. It needs roughly **2× the current DB size** in free disk during the operation (rewrites a temp copy alongside the original).

#### Container restart behavior

By default the MCP server exits cleanly at **02:00 local time** (`RESTART_AT_HOUR=2`, `TZ=America/Los_Angeles`) and Docker respawns it. This drops the resident state that chromadb accumulates and acts as a safety net even though the cleanup subprocess (see Architecture) already returns most of the memory between cycles. To change or disable:

```bash
# In .env
RESTART_AT_HOUR=2                  # 0–23 local hour, or -1 to disable
TZ=America/Los_Angeles             # timezone for the hour check
MAX_LIFETIME_SECONDS=604800        # 7-day backstop in case the hourly check misses
```

The 12 GiB Docker memory cap in `docker-compose.yml` enforces a hard ceiling. If something allocates past it, only the container is OOM-killed (and Docker restarts it) — the host stays healthy.

### Configuration

#### Environment Setup

The project uses `.env.template` with variable placeholders (e.g., `${HOME}`) that get expanded by `generate-env.sh`:

```bash
# Generate .env from template
./generate-env.sh
```

This creates a `.env` file with expanded paths. You can also manually create/edit `.env`:

```bash
DEFAULT_DASHBOARD=sig-windows-signal
PROJECTS_ROOT=/home/yourusername/.k8s-test-analyzer/cache
FASTMCP_PORT=8978
```

**Note**: `.env.template` is tracked in git, but `.env` is gitignored (contains user-specific paths).

#### ChromaDB Storage

The ChromaDB database is stored at `${PROJECTS_ROOT}/chroma_db` (default: `~/.k8s-test-analyzer/cache/chroma_db`). This location is shared between the CLI and Docker container, ensuring both use the same indexed data.

### Architecture

The project uses a modular architecture with shared business logic:

```
k8s-test-analyzer/
├── core.py                     # Shared business logic (used by both CLI and MCP)
├── mcp_server.py               # MCP server - tool wrappers calling core.py
├── cli.py                      # CLI entry point - mirrors MCP tools
├── local_indexing.py           # ChromaDB initialization and indexing logic
├── cleanup_worker.py           # Subprocess invoked by the MCP server for cleanup
├── Dockerfile                  # Docker image definition
├── docker-compose.yml          # Docker Compose configuration
├── generate-env.sh             # Script to generate .env from .env.template
├── vacuum-chromadb.sh          # Operator script: stop, VACUUM, restart
├── .env.template               # Environment template (tracked in git)
├── k8s_testlog_downloader/     # Data collection library
│   ├── __init__.py
│   ├── data_collector.py       # Main data collection logic
│   ├── gcs_client.py           # GCS download client
│   ├── testgrid_client.py      # TestGrid API client
│   ├── junit_parser.py         # JUnit XML parser
│   └── models.py               # Data models
├── chroma_db/                  # ChromaDB vector database (generated)
├── pyproject.toml              # Package configuration
├── requirements.txt            # Dependencies
└── README.md
```

#### Key Components

- **core.py**: Contains all business logic for downloading, indexing, and searching. Both the CLI and MCP server call these functions.
- **mcp_server.py**: Thin wrapper that exposes core.py functions as MCP tools via FastMCP.
- **cli.py**: CLI application that exposes core.py functions as command-line commands.
- **local_indexing.py**: ChromaDB/LlamaIndex integration for vector storage and semantic search.
- **cleanup_worker.py**: Run as a subprocess by the MCP server each cleanup pass. ChromaDB 1.x keeps HNSW segments resident in process memory once a collection is touched; running cleanup in a transient process means the kernel reclaims that memory on the worker's exit, keeping the long-lived MCP server's RSS flat.

This architecture ensures that:
1. CLI and MCP server have identical behavior (they use the same code)
2. You can test MCP functionality via the CLI without running an MCP server
3. Business logic is centralized and easy to maintain

## License

Apache License 2.0
