#!/usr/bin/env python3
"""
MCP Server for k8s-test-analyzer.
Provides tools for downloading K8s CI test logs and searching indexed content.
"""

import os
import logging
import json
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

import asyncio
from fastmcp import FastMCP

# Local imports
from local_indexing import initialize_chromadb, perform_initial_indexing
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
            n_results: Number of results to return (default: 5)
            threshold: Minimum relevance percentage to include results (default: 30.0)
    """
)
async def search_log(
    query: str,
    tab: str,
    n_results: int = 5,
    threshold: float = 30.0
) -> str:
    try:
        result = await core.search_logs(query, tab, None, n_results, threshold)
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error in search_log: {str(e)}")
        return json.dumps({"error": str(e), "results": [], "total_results": 0})


@mcp.tool(
    name="download_test",
    description="""Download Kubernetes CI test logs from TestGrid/GCS and index them for semantic search.
    Downloads are cached - files already downloaded will be skipped.
    Indexing is incremental - only new builds will be indexed, already indexed builds are skipped.
    Use force_reindex=True to delete and re-index everything.
    Args:
        tab: TestGrid tab name (e.g., "capz-windows-1-33-serial-slow")
        build_id: Build ID (optional, fetches latest if not specified)
        skip_indexing: Skip indexing after download (default: False)
        force_reindex: Force delete collection and re-index all builds (default: False)
    """
)
async def download_test(
    tab: str,
    build_id: str = None,
    skip_indexing: bool = False,
    force_reindex: bool = False
) -> str:
    result = await core.download_and_index(tab, None, build_id, skip_indexing, force_reindex)
    return json.dumps(result, indent=2, default=str)


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
    name="download_all_latest",
    description="""Download and index test data for all tabs in the configured dashboard.
    Downloads are cached - files already downloaded will be skipped.
    Indexing is incremental - only new builds will be indexed, already indexed builds are skipped.
    Use force_reindex=True to delete and re-index everything.
    Args:
        limit: Maximum tabs to fetch (optional)
        skip_indexing: Skip indexing after download (default: False)
        force_reindex: Force delete collections and re-index all builds (default: False)
    """
)
async def download_all_latest(
    limit: int = None,
    skip_indexing: bool = False,
    force_reindex: bool = False
) -> str:
    result = await core.download_all_and_index(None, limit, skip_indexing, force_reindex)
    return json.dumps(result, indent=2, default=str)


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


# Default schedule interval in seconds (1 hour)
SCHEDULE_INTERVAL_SECONDS = int(os.getenv("SCHEDULE_INTERVAL_SECONDS", "3600"))

# Number of builds to keep per job during cleanup (default: 10)
CLEANUP_KEEP_BUILDS = int(os.getenv("CLEANUP_KEEP_BUILDS", "10"))


async def run_download_and_cleanup():
    """Execute the download, index, and cleanup operations once."""
    logger.info("Starting scheduled download and reindex...")

    # Download and index new builds
    try:
        result = await core.download_all_and_index(skip_indexing=False)

        if "error" in result:
            logger.error(f"Download failed: {result.get('error')}")
            return False

        tabs_count = len(result.get("tabs", {}))
        indexed_count = result.get("indexing_summary", {}).get("total_indexed", 0)
        logger.info(f"Download complete: {tabs_count} tabs, indexed {indexed_count} projects")

    except Exception as e:
        logger.error(f"Error during download/index: {e}", exc_info=True)
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
