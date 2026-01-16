# CLAUDE.md - Project Guide for AI Assistants

## Project Overview

**k8s-test-analyzer** is a Python tool for downloading, indexing, and analyzing Kubernetes test logs from TestGrid. It provides semantic search capabilities powered by ChromaDB and LlamaIndex, with both CLI and MCP (Model Context Protocol) server interfaces for AI-assisted test analysis.

### Primary Use Case
The tool is designed for **debugging Kubernetes test failures**. Users can download test logs from TestGrid/GCS, index them using semantic embeddings, and perform natural language searches to quickly locate relevant error messages, stack traces, and log patterns across large test outputs.

### Target Audience
General developers who need to analyze Kubernetes test results, particularly those debugging CI failures or investigating test flakiness in Kubernetes-related projects.

## Key Features
- Download test logs from TestGrid and Google Cloud Storage (GCS)
- Parse JUnit XML test results
- Semantic search over logs using ChromaDB vector database
- MCP integration for Claude Desktop and Claude Code
- CLI interface that mirrors all MCP tools
- Docker deployment support
- Automatic periodic syncing of latest test data

## Architecture

### Core Design Pattern
The project uses a **shared business logic pattern**:

```
User Request
    ↓
┌─────────────┬──────────────┐
│   CLI       │  MCP Server  │  ← Interface Layer (thin wrappers)
│  (cli.py)   │(mcp_server.py)│
└──────┬──────┴──────┬───────┘
       └──────┬──────┘
              ↓
         core.py          ← Business Logic Layer (all operations)
              ↓
    ┌─────────┴─────────┐
    ↓                   ↓
local_indexing.py   k8s_testlog_downloader/  ← Data Layer
(ChromaDB/LlamaIndex)    (TestGrid/GCS clients)
```

**Key Files:**
- [core.py](core.py): Contains ALL business logic for downloading, indexing, and searching. Both CLI and MCP server call these functions.
- [mcp_server.py](mcp_server.py): Thin wrapper that exposes core.py functions as MCP tools via FastMCP.
- [cli.py](cli.py): CLI application that exposes core.py functions as command-line commands.
- [local_indexing.py](local_indexing.py): ChromaDB/LlamaIndex integration for vector storage and semantic search.

### Data Collection Library (k8s_testlog_downloader/)
- [data_collector.py](k8s_testlog_downloader/data_collector.py): Main orchestration logic for fetching test data
- [testgrid_client.py](k8s_testlog_downloader/testgrid_client.py): TestGrid API client for dashboard/tab metadata
- [gcs_client.py](k8s_testlog_downloader/gcs_client.py): GCS download client for test artifacts
- [junit_parser.py](k8s_testlog_downloader/junit_parser.py): JUnit XML parser
- [models.py](k8s_testlog_downloader/models.py): Data models

### Why This Architecture?
1. **Consistency**: CLI and MCP server have identical behavior (same code path)
2. **Testability**: You can test MCP functionality via CLI without running an MCP server
3. **Maintainability**: Business logic is centralized in core.py

## Working with This Codebase

### Making Changes
When modifying functionality:
1. **Always update [core.py](core.py) first** - this is where the business logic lives
2. The CLI and MCP wrappers should rarely need changes unless you're:
   - Adding a new tool/command
   - Changing input parameters
   - Changing output format

### Adding New Features
To add a new tool:
1. Add the business logic function to [core.py](core.py)
2. Add a CLI command in [cli.py](cli.py) that calls the core function
3. Add an MCP tool in [mcp_server.py](mcp_server.py) that calls the core function
4. Update both README.md and this CLAUDE.md

### Common Operations

#### Running Tests Locally
```bash
# Activate virtual environment
source .venv/bin/activate

# Test download functionality
k8s-test-analyzer download --tab capz-windows-1-33-serial-slow

# Test search functionality
k8s-test-analyzer search "timeout error" --tab capz-windows-1-33-serial-slow

# Get index stats
k8s-test-analyzer index-stats
```

#### Running the MCP Server
```bash
# Direct execution
python mcp_server.py

# Or with Docker
cd docker && docker-compose up -d
```

The MCP server runs on port 8978 by default (SSE endpoint: `http://localhost:8978/sse`).

### Data Storage

#### Cache Directory
Downloaded test logs are cached in `~/.k8s_testlog_cache/` by default. The structure is:
```
~/.k8s_testlog_cache/
└── <gcs-job-name>/
    └── <build-id>/
        ├── started.json
        ├── finished.json
        ├── artifacts/
        │   └── junit_*.xml
        └── build-log.txt
```

#### ChromaDB Storage
The vector database is stored at `./chroma_db/` (relative to where you run the script). Collections are named after the GCS job name (e.g., `ci-kubernetes-e2e-capz-1-33-windows-serial-slow`).

### Environment Variables

Key environment variables (can be set in `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_DASHBOARD` | `sig-windows-signal` | Default TestGrid dashboard |
| `PROJECTS_ROOT` | `~/.k8s_testlog_cache` | Root directory for cached test logs |
| `FASTMCP_PORT` | `8978` | MCP server port |
| `SCHEDULE_INTERVAL_SECONDS` | `3600` | Auto-sync interval (0 to disable) |
| `CLEANUP_KEEP_BUILDS` | `10` | Builds to keep per job during cleanup (0 to disable) |
| `HEALTHCHECK_MAX_AGE` | `300` | Heartbeat staleness threshold for Docker healthcheck (seconds) |

## MCP Tools Available

The MCP server exposes these tools (all mirror CLI commands). All tools use the dashboard configured via the `DEFAULT_DASHBOARD` environment variable.

| MCP Tool | CLI Command | Description |
|----------|-------------|-------------|
| `download_test` | `download` | Download and index test logs for a specific tab |
| `download_all_latest` | `download-all` | Download and index all tabs from the configured dashboard |
| `search_log` | `search` | Semantic search over indexed logs (filters by build_id, defaults to latest) |
| `compare_build_logs` | `compare` | Compare logs between two builds (same-job or cross-job) |
| `find_regression` | `find-regression` | Find and compare last pass with first fail from cached builds |
| `list_recent_builds` | `list-builds` | List recent builds for a tab |
| `list_dashboard_tabs` | `list-tabs` | List available tabs in the configured dashboard |
| `get_testgrid_summary` | `summary` | Get dashboard health summary (passing/failing/flaky) |
| `get_tab_status` | `status` | Get test results for latest build of each tab |
| `reindex_folder` | `reindex` | Force re-index a specific project |
| `reindex_all` | `reindex-all` | Force re-index all cached projects |
| `get_index_status` | `index-stats` | Get indexing status (all tabs by default, or specific tab/build) |

## Important Concepts

### TestGrid Terminology
- **Dashboard**: A collection of related test jobs (e.g., `sig-windows-signal`)
- **Tab**: A specific test job shown as a tab in the dashboard (e.g., `capz-windows-1-33-serial-slow`)
- **Build**: A single CI run/execution of a test job (identified by build ID)
- **GCS Job Name**: The Google Cloud Storage path for the job's artifacts (may differ from tab name)

### Indexing Behavior
- **Caching**: Downloads are cached locally. Re-running download will use cached files.
- **Indexing**: Projects are indexed once by default. Use `force_reindex=True` to re-index.
- **Search Scope**: Searches are scoped to a specific tab/project. You must specify which tab's logs to search.

### TestGrid vs Test Results
- **TestGrid Summary** (`get_testgrid_summary`): Fast, uses TestGrid's aggregated status (PASSING/FAILING/FLAKY)
- **Tab Status** (`get_tab_status`): Slower, downloads actual test results and counts pass/fail

## Common Tasks

### Debugging a Specific Test Failure
1. Download the logs: `download_test(tab="capz-windows-1-33-serial-slow")`
2. Search for error patterns: `search_log(query="timeout waiting for pod", tab="capz-windows-1-33-serial-slow")` (searches latest build by default)
3. Search a specific build: `search_log(query="timeout", tab="capz-windows-1-33-serial-slow", build_id="1234567890")`
4. Review search results for relevant log sections

### Monitoring Dashboard Health
1. Get quick status: `get_testgrid_summary()` (uses DEFAULT_DASHBOARD from env)
2. For detailed results: `get_tab_status()` or `get_tab_status(tabs="specific-tab")`

### Investigating Flaky Tests
1. Download multiple builds: Use `download_test()` with different `build_id` parameters
2. Search for common patterns: `search_log(query="race condition OR intermittent")`

### Comparing Two Builds
1. Download both builds: `download_test(tab="capz-windows-1-33-serial-slow", build_id="123")` and `download_test(tab="capz-windows-1-33-serial-slow", build_id="456")`
2. Compare the logs: `compare_build_logs(tab_a="capz-windows-1-33-serial-slow", build_id_a="123", build_id_b="456")`
3. The result contains categorized log chunks from both builds - analyze to find new/fixed errors, version changes, etc.

### Finding When a Test Started Failing (Regression Analysis)
1. First, download several recent builds: `download_all_latest()` or multiple `download_test()` calls
2. Use `find_regression(tab="capz-windows-1-33-serial-slow")` to automatically find the regression point
3. The tool scans cached builds, finds the last passing and first failing build, and compares them
4. Returns early if the most recent build is passing (no regression to investigate)
5. If all cached builds are failing, download older builds to find the regression point

## Dependencies

Key dependencies:
- **FastMCP**: MCP server framework
- **ChromaDB**: Vector database for semantic search
- **LlamaIndex**: Document indexing and retrieval
- **sentence-transformers**: Embedding model for semantic search
- **aiohttp**: Async HTTP client for TestGrid/GCS
- **beautifulsoup4**: HTML parsing for TestGrid

See [requirements.txt](requirements.txt) for complete list.

## Development Setup

```bash
# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install --upgrade pip setuptools
pip install -e .
pip install -r requirements.txt

# Test CLI
k8s-test-analyzer --help

# Test MCP server
python mcp_server.py
```

## Testing Changes

Since CLI and MCP use the same core logic, you can test changes via CLI:

```bash
# Test download
k8s-test-analyzer download --tab capz-windows-1-33-serial-slow

# Test search
k8s-test-analyzer search "error message" --tab capz-windows-1-33-serial-slow

# Test status
k8s-test-analyzer status --dashboard sig-windows-signal
```

Once CLI works, MCP will work identically.

## Troubleshooting

### ChromaDB Not Initialized
- Ensure `chroma_db/` directory exists and has write permissions
- Check that embedding model downloaded successfully
- Look for initialization errors in logs

### No Search Results
- Verify the project is indexed: `k8s-test-analyzer index-stats`
- Check collection name matches tab/project: Collections use GCS job name, not tab name
- Try re-indexing: `k8s-test-analyzer reindex <project-name>`

### Download Failures
- Check network connectivity to TestGrid and GCS
- Verify dashboard and tab names are correct: `k8s-test-analyzer list-tabs`
- Check if build exists: `k8s-test-analyzer list-builds --tab <tab-name>`

## Code Style and Conventions

- **Async/Await**: Core functions use async for I/O operations
- **Logging**: Use Python's `logging` module (configured in cli.py and mcp_server.py)
- **Error Handling**: Return error dicts rather than raising exceptions (for tool compatibility)
- **Type Hints**: Use type hints for function parameters and return types
- **Docstrings**: Use Google-style docstrings for functions

## Questions or Issues?

When something isn't clear:
1. Check [README.md](README.md) for user-facing documentation
2. Look at [core.py](core.py) for implementation details
3. Review [cli.py](cli.py) for usage examples
4. Check recent commits for context on recent changes

## License

Apache License 2.0
