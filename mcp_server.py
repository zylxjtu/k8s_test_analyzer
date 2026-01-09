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
            dashboard: Dashboard name (optional, uses default if not specified)
            n_results: Number of results to return (default: 5)
            threshold: Minimum relevance percentage to include results (default: 30.0)
    """
)
async def search_log(
    query: str,
    tab: str,
    dashboard: str = None,
    n_results: int = 5,
    threshold: float = 30.0
) -> str:
    try:
        result = await core.search_logs(query, tab, dashboard, n_results, threshold)
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error in search_log: {str(e)}")
        return json.dumps({"error": str(e), "results": [], "total_results": 0})


@mcp.tool(
    name="download_test",
    description="""Download Kubernetes CI test logs from TestGrid/GCS and index them for semantic search.
    Downloads are cached - files already downloaded will be skipped.
    Indexing is also cached - already indexed projects will be skipped unless force_reindex=True.
    Args:
        tab: TestGrid tab name (e.g., "capz-windows-1-33-serial-slow")
        dashboard: Dashboard name (optional, uses default if not specified)
        build_id: Build ID (optional, fetches latest if not specified)
        skip_indexing: Skip indexing after download (default: False)
        force_reindex: Force re-index even if already indexed (default: False)
    """
)
async def download_test(
    tab: str,
    dashboard: str = None,
    build_id: str = None,
    skip_indexing: bool = False,
    force_reindex: bool = False
) -> str:
    result = await core.download_and_index(tab, dashboard, build_id, skip_indexing, force_reindex)
    return json.dumps(result, indent=2, default=str)


@mcp.tool(
    name="list_recent_builds",
    description="""List recent builds for a tab.
    Args:
        tab: TestGrid tab name (e.g., "capz-windows-1-33-serial-slow")
        dashboard: Dashboard name (optional, uses default if not specified)
        limit: Maximum number of builds (default: 10)
    """
)
async def list_recent_builds(tab: str, dashboard: str = None, limit: int = 10) -> str:
    result = core.list_builds(tab, dashboard, limit)
    return json.dumps(result, indent=2, default=str)


@mcp.tool(
    name="get_testgrid_summary",
    description="""Get TestGrid dashboard summary.
    Args:
        dashboard: Dashboard name
        tab: Tab name (optional)
    """
)
async def get_testgrid_summary(dashboard: str, tab: str = None) -> str:
    result = core.get_testgrid_summary(dashboard, tab)
    return json.dumps(result, indent=2, default=str)


@mcp.tool(
    name="get_tab_status",
    description="""Get test results status for latest build of each tab.
    
    Fetches the latest finished build for each tab and returns test pass/fail counts.
    Status is PASS if failed==0 and total>0, FAIL if failed>0 or total==0, ERROR if fetch failed.
    
    Args:
        dashboard: Dashboard name (e.g., "sig-windows-signal")
        tabs: Comma-separated tab names to check (optional, defaults to all tabs)
    """
)
async def get_tab_status(dashboard: str = None, tabs: str = None) -> str:
    result = await core.get_tab_status(dashboard, tabs)
    return json.dumps(result, indent=2, default=str)


@mcp.tool(
    name="list_dashboard_tabs",
    description="""List available tabs for a dashboard.
    Args:
        dashboard: Dashboard name
    """
)
async def list_dashboard_tabs(dashboard: str) -> str:
    result = core.list_tabs(dashboard)
    return json.dumps(result, indent=2)


@mcp.tool(
    name="download_all_latest",
    description="""Download and index test data for all tabs in a dashboard.
    Downloads are cached - files already downloaded will be skipped.
    Indexing is also cached - already indexed projects will be skipped unless force_reindex=True.
    Args:
        dashboard: Dashboard name (optional)
        limit: Maximum tabs to fetch (optional)
        skip_indexing: Skip indexing after download (default: False)
        force_reindex: Force re-index even if already indexed (default: False)
    """
)
async def download_all_latest(
    dashboard: str = None,
    limit: int = None,
    skip_indexing: bool = False,
    force_reindex: bool = False
) -> str:
    result = await core.download_all_and_index(dashboard, limit, skip_indexing, force_reindex)
    return json.dumps(result, indent=2, default=str)


@mcp.tool(
    name="reindex_folder",
    description="""Force re-index a specific project/folder.
    Args:
        project_name: The project/folder name to re-index (e.g., "ci-kubernetes-e2e-capz-1-33-windows-serial-slow")
    """
)
async def reindex_folder(project_name: str) -> str:
    result = await core.reindex_project(project_name)
    return json.dumps(result, indent=2, default=str)


@mcp.tool(
    name="reindex_all",
    description="""Force re-index all cached project folders.
    Re-indexes every folder in the cache directory, useful after manual changes or to refresh all indexes.
    """
)
async def reindex_all() -> str:
    try:
        result = await core.reindex_all_projects()
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error during reindex_all: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool(
    name="get_index_stats",
    description="""Get indexing statistics for all collections.
    Returns information about indexed collections including document counts.
    """
)
async def get_index_stats() -> str:
    try:
        result = await core.get_index_stats()
        return json.dumps(result, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# Default schedule interval in seconds (1 hour)
SCHEDULE_INTERVAL_SECONDS = int(os.getenv("SCHEDULE_INTERVAL_SECONDS", "3600"))

# Number of builds to keep per job during cleanup (default: 10)
CLEANUP_KEEP_BUILDS = int(os.getenv("CLEANUP_KEEP_BUILDS", "10"))


async def scheduled_download_and_reindex():
    """Background task that periodically downloads all latest builds, reindexes, and cleans up old builds."""
    while True:
        try:
            # Wait for the configured interval
            logger.info(f"Scheduled task sleeping for {SCHEDULE_INTERVAL_SECONDS} seconds...")
            await asyncio.sleep(SCHEDULE_INTERVAL_SECONDS)

            logger.info("Starting scheduled download and reindex...")

            # Download and index new builds
            result = await core.download_all_and_index(skip_indexing=False)

            tabs_count = len(result.get("tabs", {}))
            indexed_count = result.get("indexing_summary", {}).get("total_indexed", 0)

            logger.info(f"Download complete: {tabs_count} tabs, indexed {indexed_count} projects")

            # Clean up old builds
            if CLEANUP_KEEP_BUILDS > 0:
                logger.info(f"Starting cleanup, keeping {CLEANUP_KEEP_BUILDS} builds per job...")
                cleanup_result = await core.cleanup_old_builds(keep_builds=CLEANUP_KEEP_BUILDS)
                builds_deleted = cleanup_result.get("total_builds_deleted", 0)
                chunks_deleted = cleanup_result.get("total_chunks_deleted", 0)
                space_freed = cleanup_result.get("total_space_freed_mb", 0)
                logger.info(f"Cleanup complete: {builds_deleted} builds deleted, {chunks_deleted} chunks removed, {space_freed:.2f} MB freed")
            else:
                logger.info("Cleanup disabled (CLEANUP_KEEP_BUILDS=0)")

            logger.info("Scheduled task complete")

        except Exception as e:
            logger.error(f"Error in scheduled task: {e}")
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
