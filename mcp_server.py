#!/usr/bin/env python3
"""
MCP Server for k8s-test-analyzer.
Provides tools for downloading K8s CI test logs and searching indexed content.
"""

import os
import logging
import json
import asyncio
import yaml
from fastmcp import FastMCP

# Local imports
from local_indexing import initialize_chromadb, perform_initial_indexing, write_heartbeat
import core

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# FastMCP server
mcp = FastMCP("k8s-test-analyzer")


@mcp.tool(
    name="search_log",
    description="""Search indexed logs using natural language queries.
        Args:
            query: Natural language query about the logs/codebase
            tab: TestGrid tab name (e.g., "capz-windows-1-33-serial-slow")
            build_id: Build ID to search within (uses latest cached build if not specified)
            n_results: Number of results to return (default: 5)
            threshold: Minimum relevance percentage to include results (default: 30.0)
    """
)
async def search_log(
    query: str,
    tab: str,
    build_id: str = None,
    n_results: int = 5,
    threshold: float = 30.0
) -> str:
    try:
        result = await core.search_logs(query, tab, None, n_results, threshold, build_id)
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error in search_log: {str(e)}")
        return json.dumps({"error": str(e), "results": [], "total_results": 0})


@mcp.tool(
    name="list_recent_builds",
    description="""List recent builds for a tab.
    Args:
        tab: TestGrid tab name (e.g., "capz-windows-1-33-serial-slow")
        limit: Maximum number of builds (default: 10)
    """
)
async def list_recent_builds(tab: str, limit: int = 10) -> str:
    result = core.list_builds(tab, None, limit)
    return json.dumps(result, indent=2, default=str)


@mcp.tool(
    name="get_testgrid_summary",
    description="""Get TestGrid dashboard summary.
    Args:
        tab: Tab name (optional, returns full dashboard summary if not specified)
    """
)
async def get_testgrid_summary(tab: str = None) -> str:
    result = core.get_testgrid_summary(None, tab)
    return json.dumps(result, indent=2, default=str)


@mcp.tool(
    name="get_tab_status",
    description="""Get test results status for latest build of each tab.

    Fetches the latest finished build for each tab and returns test pass/fail counts.
    Status is PASS if failed==0 and total>0, FAIL if failed>0 or total==0, ERROR if fetch failed.

    Args:
        tabs: Comma-separated tab names to check (optional, defaults to all tabs)
    """
)
async def get_tab_status(tabs: str = None) -> str:
    result = await core.get_tab_status(None, tabs)
    return json.dumps(result, indent=2, default=str)


@mcp.tool(
    name="list_dashboard_tabs",
    description="""List available tabs for the configured dashboard."""
)
async def list_dashboard_tabs() -> str:
    result = core.list_tabs(None)
    return json.dumps(result, indent=2)


@mcp.tool(
    name="get_index_status",
    description="""Get the indexing status for builds.

    Check whether builds are cached on disk, indexed in ChromaDB, and how many chunks they have.
    Useful for debugging indexing issues or verifying builds are ready for search/comparison.

    Usage patterns:
    - No tab specified: Get status for latest build of ALL tabs in the dashboard
    - Tab specified, no build_id: Get status for latest build of that specific tab
    - Tab and build_id specified: Get status for that specific build

    Args:
        tab: TestGrid tab name (e.g., "capz-windows-1-33-serial-slow"). If not specified, returns status for all tabs.
        build_id: Build ID to check (optional, uses latest cached build if not specified)

    Returns:
        Status information including:
        - cached: whether build folder exists on disk
        - indexed: whether build is in the completion marker
        - chunk_count: number of chunks in ChromaDB for this build
        - status: summary ("indexed", "cached_not_indexed", "not_cached", "unknown")
        - summary: (when checking all tabs) counts of indexed, cached_not_indexed, not_cached, and errors
    """
)
async def get_index_status(
    tab: str = None,
    build_id: str = None
) -> str:
    try:
        # No tab specified - get status for all tabs
        if tab is None:
            result = await core.get_all_latest_build_index_status(
                dashboard=None,
                tabs=None
            )
            return json.dumps(result, indent=2)

        # Tab specified but no build_id - get status for latest build of this tab
        if build_id is None:
            result = await core.get_all_latest_build_index_status(
                dashboard=None,
                tabs=[tab]
            )
            # Return just the tab result for single-tab query
            if result.get("tabs"):
                return json.dumps(result["tabs"][0], indent=2)
            return json.dumps(result, indent=2)

        # Both tab and build_id specified - get status for specific build
        result = await core.get_build_index_status(
            tab=tab,
            build_id=build_id,
            dashboard=None
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in get_index_status: {str(e)}")
        return json.dumps({"error": str(e)})


@mcp.tool(
    name="get_test_failures",
    description="""Get parsed JUnit test failures for a build, grouped by SIG.

    Parses JUnit XML files from cached builds and returns structured test failure
    information grouped by SIG (Special Interest Group). Much more useful than raw
    log chunks for understanding what tests failed and why.

    DEBUGGING TIP: This should be your FIRST tool when investigating test failures.
    It gives you structured data showing exactly which tests failed, their error
    messages, and stack traces - all organized by Kubernetes SIG ownership.

    Args:
        tab: TestGrid tab name (e.g., "capz-windows-1-33-serial-slow")
        build_id: Build ID (optional, uses latest cached build if not specified)
    """
)
async def get_test_failures(
    tab: str,
    build_id: str = None
) -> str:
    try:
        result = await core.get_test_failures(
            tab=tab,
            build_id=build_id,
            dashboard=None
        )
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error in get_test_failures: {str(e)}")
        return json.dumps({"error": str(e)})


@mcp.tool(
    name="compare_build_logs",
    description="""Compare logs between two Kubernetes CI builds.

    Retrieves indexed log chunks from two builds organized by category (errors, failures, versions, timing).
    Use this to find differences between builds - what errors are new, what got fixed, version changes, etc.

    Supports same-job comparison (e.g., build 123 vs build 456 of same tab) or cross-job comparison
    (e.g., comparing 1.33 tab vs 1.34 tab).

    Args:
        tab_a: TestGrid tab name for build A (e.g., "capz-windows-1-33-serial-slow")
        build_id_a: Build ID for build A (optional, uses latest if not specified)
        tab_b: TestGrid tab name for build B (optional, defaults to tab_a for same-job comparison)
        build_id_b: Build ID for build B (optional, uses latest if not specified)
        filter_type: Filter chunks - "all", "interesting" (default), "errors", "failures"
        max_chunks_per_build: Maximum chunks per category per build (default: 100)
    """
)
async def compare_build_logs(
    tab_a: str,
    build_id_a: str = None,
    tab_b: str = None,
    build_id_b: str = None,
    filter_type: str = "interesting",
    max_chunks_per_build: int = 100
) -> str:
    try:
        result = await core.compare_indexed_builds(
            tab_a=tab_a,
            build_id_a=build_id_a,
            tab_b=tab_b,
            build_id_b=build_id_b,
            dashboard=None,
            filter_type=filter_type,
            max_chunks_per_build=max_chunks_per_build
        )
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error in compare_build_logs: {str(e)}")
        return json.dumps({"error": str(e)})


@mcp.tool(
    name="find_regression",
    description="""Find and compare the last successful build with the first failed build.

    Automatically finds the regression point by scanning CACHED builds (already downloaded).
    Returns early if the most recent cached build is passing (no regression to find).

    Use this tool when a test starts failing and you want to identify what changed.
    The tool will:
    1. Find the last passing build and first failing build in the cached builds
    2. Ensure both builds are indexed
    3. Compare them to show differences (errors, failures, versions, timing)

    NOTE: Only checks builds that are already downloaded. Use download_test first to cache builds.

    Args:
        tab: TestGrid tab name (e.g., "capz-windows-1-33-serial-slow")
        max_builds: Maximum number of cached builds to check (default: 10)
        filter_type: Filter chunks - "all", "interesting" (default), "errors", "failures"
        max_chunks_per_build: Maximum chunks per category per build (default: 100)
    """
)
async def find_regression(
    tab: str,
    max_builds: int = 10,
    filter_type: str = "interesting",
    max_chunks_per_build: int = 100
) -> str:
    try:
        result = await core.find_regression(
            tab=tab,
            dashboard=None,
            max_builds=max_builds,
            filter_type=filter_type,
            max_chunks_per_build=max_chunks_per_build
        )
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error in find_regression: {str(e)}")
        return json.dumps({"error": str(e)})


def _load_debugging_guides() -> dict:
    """Load debugging guides from YAML file. Reads fresh on each call for live updates."""
    import yaml
    guides_file = os.path.join(os.path.dirname(__file__), "debugging_guides.yaml")

    try:
        with open(guides_file, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.warning(f"Failed to load debugging_guides.yaml: {e}, using defaults")
        return {
            "overview": "# K8s Test Debugging Guide\n\nGuide file not found. Please create debugging_guides.yaml",
            "failures": "# Debugging Test Failures\n\nGuide not available.",
            "regression": "# Finding Regression Causes\n\nGuide not available.",
            "flaky": "# Analyzing Flaky Tests\n\nGuide not available.",
        }


@mcp.tool(
    name="get_debugging_guide",
    description="""Get the K8s test debugging guide with workflows and common patterns.

    Returns comprehensive debugging knowledge including:
    - Step-by-step debugging workflows
    - Common failure patterns and their causes
    - Windows-specific troubleshooting tips
    - Which tools to use for different scenarios
    - Version info and changelog

    The guide is loaded from debugging_guides.yaml and can be updated without restarting the server.

    Call this tool when you need guidance on how to debug K8s test failures.
    The guide will help you choose the right tools and interpret results.

    Args:
        topic: Optional topic ("failures", "regression", "flaky", "changelog", or None for full guide)
    """
)
async def get_debugging_guide(topic: str = None) -> str:
    """Return debugging guide as accumulated knowledge/skills. Loads fresh from file each time."""

    # Load guides fresh each time (enables live updates without restart)
    guides = _load_debugging_guides()

    # Extract version info
    version = guides.get("version", "unknown")
    last_updated = guides.get("last_updated", "unknown")
    version_header = f"**Debugging Guide v{version}** (Last updated: {last_updated})\n\n"

    # Handle changelog request
    if topic and topic.lower() == "changelog":
        changelog = guides.get("changelog", "No changelog available.")
        return version_header + changelog

    # Handle specific topic request
    if topic and topic.lower() in guides:
        return version_header + guides[topic.lower()]

    # Return full guide (overview + all sections)
    sections = [version_header + guides.get("overview", "")]
    for key in ["failures", "regression", "flaky"]:
        if key in guides:
            sections.append(guides[key])

    return "\n\n---\n\n".join(sections)


# ============== PROMPTS ==============
# Guided workflows for K8s test troubleshooting
# These provide step-by-step guidance and suggest tools to use

@mcp.prompt(
    name="debug_test_failure",
    description="Systematic workflow to debug K8s test failures"
)
async def prompt_debug_test_failure(tab: str, build_id: str = None):
    """Guide through debugging test failures step by step."""
    build_info = f"build `{build_id}`" if build_id else "the latest build"

    return f"""# Debug Test Failures: {tab}

I'll guide you through debugging test failures for **{tab}** ({build_info}).

## Step 1: Understand What Failed
Use the `get_test_failures` tool to see structured failure information:
- Failures are grouped by SIG (Special Interest Group)
- Each failure includes the test name, error message, and stack trace

## Step 2: Search for Error Details
Use `search_log` to find more context. Good search patterns:
- `"panic"` or `"fatal"` - For crashes
- `"timeout"` or `"timed out"` - For timing issues
- `"failed to"` - For specific operation failures
- The exact test name from Step 1

## Step 3: Compare with Passing Build
If you have a known-good build, use `compare_build_logs` to see what changed.
Or use `find_regression` to automatically find the last passing build.

## Common K8s Test Failure Patterns

| Pattern | Likely Cause | What to Check |
|---------|--------------|---------------|
| `timeout waiting for pod` | Slow provisioning | Node resources, image pull time |
| `connection refused` | Service not ready | Pod status, network policies |
| `context deadline exceeded` | Operation too slow | Cluster load, API server health |
| `permission denied` | RBAC or security | ServiceAccount, PodSecurityPolicy |
| `ImagePullBackOff` | Registry issues | Image name, credentials, network |

## Windows-Specific Issues
- HNS networking failures -> Check Windows node logs
- Container runtime errors -> Verify containerd/Docker version
- Path issues -> Windows vs Linux path separators

---
Ready to start? I recommend beginning with `get_test_failures` for tab `{tab}`.
"""


@mcp.prompt(
    name="find_regression_cause",
    description="Identify what change caused tests to start failing"
)
async def prompt_find_regression_cause(tab: str):
    """Guide through regression analysis workflow."""
    return f"""# Find Regression Cause: {tab}

I'll help you identify what change caused tests to start failing for **{tab}**.

## Step 1: Find the Regression Point
Use `find_regression` tool with tab="{tab}" to:
- Scan cached builds to find the pass->fail transition
- Identify the last passing build and first failing build
- Compare the two builds automatically

## Step 2: Analyze the Comparison
Look for these key differences in the comparison output:

### Version Changes
- Kubernetes version bumps
- Container runtime version changes
- Test image version updates
- Dependency updates

### New Error Messages
- Errors that appear in the failing build but not in passing
- Stack traces with new failure modes

### Configuration Differences
- Changed test parameters
- Different cluster configurations
- Modified resource limits

## Step 3: Cross-Reference
Once you identify suspicious changes:
1. Check Kubernetes release notes for breaking changes
2. Look at recent PRs merged around the regression time
3. Search for related issues in kubernetes/kubernetes

## Common Regression Causes

| Category | Examples |
|----------|----------|
| API Changes | Deprecated APIs removed, new required fields |
| Behavior Changes | Timing changes, default value changes |
| Security | New restrictions, RBAC requirements |
| Dependencies | Incompatible versions, removed features |

---
Start with `find_regression` to locate the regression point.
"""


@mcp.prompt(
    name="analyze_flaky_test",
    description="Investigate intermittent test failures"
)
async def prompt_analyze_flaky_test(tab: str, test_name: str = None):
    """Guide through flaky test analysis."""
    test_info = f"test `{test_name}`" if test_name else "flaky tests"

    return f"""# Analyze Flaky Test: {tab}

I'll help you investigate {test_info} in **{tab}**.

## Step 1: Gather Build History
Use `list_recent_builds` to see recent builds and their pass/fail status.
Look for patterns:
- Does it fail at specific times?
- Does it fail in clusters?
- What's the failure rate?

## Step 2: Compare Passing vs Failing Runs
Use `search_log` to search for the test name across multiple builds:
- Search in a passing build
- Search in a failing build
- Compare the output differences

## Step 3: Identify Flakiness Patterns

### Timing-Related
- `timeout` or `deadline exceeded` -> Test assumes faster execution
- Race conditions -> Order-dependent assertions
- Resource cleanup -> Previous test artifacts interfering

### Resource-Related
- Memory pressure -> OOM kills, slow garbage collection
- CPU throttling -> Timeouts under load
- Disk I/O -> Slow writes affecting test assertions

### Environment-Related
- Network variability -> Connection timeouts, DNS issues
- Node conditions -> Taints, resource availability
- External dependencies -> APIs, registries

## Common Flaky Test Fixes

| Pattern | Solution |
|---------|----------|
| Timeout too short | Increase timeout or use Eventually() |
| Race condition | Add proper synchronization |
| Resource leak | Ensure cleanup in AfterEach |
| Hard-coded delays | Use polling with timeout instead |
| Shared state | Isolate test namespaces |

## Windows-Specific Flakiness
- Container startup time -> Windows containers are slower to start
- HNS networking -> Occasional network initialization delays
- File locking -> Windows file handles held longer

---
Start by using `list_recent_builds` to see the recent history for `{tab}`.
{f'Then search for `{test_name}` to see where it fails.' if test_name else ''}
"""


# Default schedule interval in seconds (1 hour)
SCHEDULE_INTERVAL_SECONDS = int(os.getenv("SCHEDULE_INTERVAL_SECONDS", "3600"))

# Number of builds to keep per job during cleanup (default: 10)
CLEANUP_KEEP_BUILDS = int(os.getenv("CLEANUP_KEEP_BUILDS", "10"))


async def run_download_and_cleanup():
    """Execute the download, index, and cleanup operations once."""
    write_heartbeat()  # Start of task
    logger.info("Starting scheduled download and reindex...")

    # Download and index new builds
    try:
        result = await core.download_all_and_index(skip_indexing=False)
        write_heartbeat()  # After download/index

        if "error" in result:
            logger.error(f"Download failed: {result.get('error')}")
            return False

        tabs_count = len(result.get("tabs", {}))
        indexed_count = result.get("indexing_summary", {}).get("total_indexed", 0)
        logger.info(f"Download complete: {tabs_count} tabs, indexed {indexed_count} projects")

    except Exception as e:
        logger.error(f"Error during download/index: {e}", exc_info=True)
        write_heartbeat()  # Update even on error
        return False

    # Clean up old builds
    if CLEANUP_KEEP_BUILDS > 0:
        try:
            logger.info(f"Starting cleanup, keeping {CLEANUP_KEEP_BUILDS} builds per job...")
            cleanup_result = await core.cleanup_old_builds(keep_builds=CLEANUP_KEEP_BUILDS)
            builds_deleted = cleanup_result.get("total_builds_deleted", 0)
            chunks_deleted = cleanup_result.get("total_chunks_deleted", 0)
            space_freed = cleanup_result.get("total_space_freed_mb", 0)
            logger.info(f"Cleanup complete: {builds_deleted} builds deleted, {chunks_deleted} chunks removed, {space_freed:.2f} MB freed")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}", exc_info=True)
    else:
        logger.info("Cleanup disabled (CLEANUP_KEEP_BUILDS=0)")

    write_heartbeat()  # End of task
    logger.info("Scheduled task complete")
    return True


async def scheduled_download_and_reindex():
    """Background task that periodically downloads all latest builds, reindexes, and cleans up old builds."""
    # Run immediately on startup
    await run_download_and_cleanup()

    # Then run periodically
    while True:
        try:
            logger.info(f"Scheduled task: sleeping for {SCHEDULE_INTERVAL_SECONDS} seconds until next run...")
            await asyncio.sleep(SCHEDULE_INTERVAL_SECONDS)
            await run_download_and_cleanup()

        except Exception as e:
            logger.error(f"Error in scheduled task loop: {e}", exc_info=True)
            # Continue running even after errors


# Run initialization before starting MCP
async def main():
    # Initialize ChromaDB before starting MCP
    success = await initialize_chromadb()

    if success:
        logger.info("ChromaDB initialized successfully")
        # Perform initial indexing of existing folders
        await perform_initial_indexing()
    else:
        logger.warning("ChromaDB initialization had issues, some features may not work")

    # Start background scheduled task
    if SCHEDULE_INTERVAL_SECONDS > 0:
        logger.info(f"Starting scheduled download/reindex task (interval: {SCHEDULE_INTERVAL_SECONDS}s)")
        asyncio.create_task(scheduled_download_and_reindex())
    else:
        logger.info("Scheduled task disabled (SCHEDULE_INTERVAL_SECONDS=0)")

    port = int(os.getenv("FASTMCP_PORT", "8978"))
    await mcp.run_async(transport="sse", host="0.0.0.0", port=port)


if __name__ == "__main__":
    asyncio.run(main())
