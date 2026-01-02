#!/usr/bin/env python3
"""
Core operations shared between MCP server and CLI.
Contains the business logic for download, index, and search operations.
"""

import os
import json
import logging
from typing import Optional

from k8s_testlog_downloader.data_collector import DataCollector, get_default_dashboard
from local_indexing import (
    initialize_chromadb,
    index_project,
    auto_discover_folders,
    get_chroma_client,
    get_embedding_function,
    get_config,
)

logger = logging.getLogger(__name__)

# Global data collector (singleton)
_collector = None


def get_collector() -> DataCollector:
    """Get or create the DataCollector singleton."""
    global _collector
    if _collector is None:
        _collector = DataCollector()
    return _collector


async def download_and_index(
    tab: str,
    dashboard: str = None,
    build_id: str = None,
    skip_indexing: bool = False,
    force_reindex: bool = False
) -> dict:
    """
    Download test logs and optionally index them.
    
    Args:
        tab: TestGrid tab name
        dashboard: Dashboard name (uses default if not specified)
        build_id: Specific build ID (fetches latest if not specified)
        skip_indexing: Skip indexing after download
        force_reindex: Force re-index even if already indexed
    
    Returns:
        dict with download results and optional indexing results
    """
    c = get_collector()
    result = c.collect_from_tab(tab, dashboard=dashboard, build_id=build_id)
    
    # Index the downloaded logs for search unless skipped
    if not skip_indexing and "error" not in result:
        project_name = result.get("source", {}).get("gcs_job_name") or result.get("job_name")
        if project_name:
            index_result = await index_project(project_name, force=force_reindex)
            result["indexing"] = index_result
            logger.info(f"Indexed project {project_name}: {index_result}")
    
    return result


async def download_all_and_index(
    dashboard: str = None,
    limit: int = None,
    skip_indexing: bool = False,
    force_reindex: bool = False
) -> dict:
    """
    Download test logs for all tabs and optionally index them.
    
    Args:
        dashboard: Dashboard name (uses default if not specified)
        limit: Maximum number of tabs to fetch
        skip_indexing: Skip indexing after download
        force_reindex: Force re-index even if already indexed
    
    Returns:
        dict with download results and indexing summary
    """
    c = get_collector()
    result = c.collect_all_tabs(dashboard, limit)
    
    # Index all downloaded projects unless skipped
    if not skip_indexing and "tabs" in result:
        indexing_results = []
        for tab_name, tab_data in result.get("tabs", {}).items():
            if not tab_data.get("error"):
                project_name = tab_data.get("gcs_job_name")
                if project_name:
                    index_result = await index_project(project_name, force=force_reindex)
                    indexing_results.append({
                        "project": project_name,
                        "indexing": index_result
                    })
                    logger.info(f"Indexed project {project_name}: {index_result}")
        
        result["indexing_summary"] = {
            "total_indexed": len([r for r in indexing_results if r.get("indexing", {}).get("documents_indexed", 0) > 0]),
            "already_indexed": len([r for r in indexing_results if r.get("indexing", {}).get("message") == "Already indexed"]),
            "details": indexing_results
        }
    
    return result


async def search_logs(
    query: str,
    tab: str,
    dashboard: str = None,
    n_results: int = 5,
    threshold: float = 30.0
) -> dict:
    """
    Search indexed logs using semantic search.
    
    Args:
        query: Natural language search query
        tab: TestGrid tab name
        dashboard: Dashboard name (uses default if not specified)
        n_results: Number of results to return
        threshold: Minimum relevance percentage
    
    Returns:
        dict with search results
    """
    chroma_client = get_chroma_client()
    embedding_function = get_embedding_function()
    
    if not chroma_client or not embedding_function:
        logger.error("ChromaDB client or embedding function not initialized")
        return {
            "error": "Search system not properly initialized",
            "results": [],
            "total_results": 0
        }

    # Convert tab name to job/project name
    c = get_collector()
    dashboard = dashboard or get_default_dashboard()
    project_name = c._get_gcs_job_name(dashboard, tab)
    logger.info(f"Searching in project: {project_name} (tab: {tab})")

    # Get all collections
    collections = chroma_client.list_collections()

    # Find matching collections
    matching_collections = []
    project_lower = project_name.lower()
    for collection in collections:
        # Handle both old API (strings) and new API (Collection objects)
        coll_name = collection.name if hasattr(collection, 'name') else str(collection)
        if coll_name.lower() == project_lower:
            matching_collections.append(coll_name)

    if not matching_collections:
        # Get collection names for error message
        available = [c.name if hasattr(c, 'name') else str(c) for c in collections]
        logger.error(f"No collections found matching tab {tab} (project: {project_name})")
        return {
            "error": f"No indexed data found for tab '{tab}'. Run download first to download and index logs.",
            "tab": tab,
            "project_name": project_name,
            "available_collections": available,
            "results": [],
            "total_results": 0
        }

    # Search in all matching collections and combine results
    all_results = []

    for collection_name in matching_collections:
        collection = chroma_client.get_collection(collection_name)

        results = collection.query(
            query_texts=[query],
            n_results=n_results,
            include=["documents", "metadatas", "distances"]
        )

        if results["documents"] and results["documents"][0]:
            for doc, meta, distance in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0]
            ):
                similarity = (1 - distance) * 100
                if similarity >= threshold:
                    all_results.append({
                        "text": doc,
                        "file_path": meta.get("file_path", "Unknown file"),
                        "language": meta.get("language", "text"),
                        "start_line": int(meta.get("start_line", 0)),
                        "end_line": int(meta.get("end_line", 0)),
                        "relevance": round(similarity, 1),
                        "collection": collection.name
                    })

    # Sort results by relevance
    all_results.sort(key=lambda x: x["relevance"], reverse=True)

    # Take top n_results
    final_results = all_results[:n_results]

    return {
        "tab": tab,
        "project_name": project_name,
        "query": query,
        "results": final_results,
        "total_results": len(final_results)
    }


async def reindex_project(project_name: str) -> dict:
    """
    Force re-index a specific project folder.
    
    Args:
        project_name: The project/folder name to re-index
    
    Returns:
        dict with indexing results
    """
    result = await index_project(project_name, force=True)
    return result


async def reindex_all_projects() -> dict:
    """
    Force re-index all cached project folders.
    
    Returns:
        dict with summary of reindexing results
    """
    config = get_config()
    projects_root = config["projects_root"]
    
    if not os.path.exists(projects_root):
        return {"error": f"Projects root does not exist: {projects_root}"}
    
    # Discover all folders in cache
    folders = auto_discover_folders(projects_root, set(config["ignore_dirs"]))
    
    if not folders:
        return {"message": "No folders found to index", "total_indexed": 0}
    
    logger.info(f"Re-indexing {len(folders)} folders...")
    
    results = []
    success_count = 0
    for folder in folders:
        if not folder:
            continue
        result = await index_project(folder, force=True)
        results.append({
            "folder": folder,
            "result": result
        })
        if result.get("success"):
            success_count += 1
            logger.info(f"Re-indexed {folder}: {result.get('documents_indexed', 0)} documents")
        else:
            logger.warning(f"Failed to re-index {folder}: {result.get('error', 'Unknown error')}")
    
    return {
        "total_folders": len(folders),
        "successful": success_count,
        "failed": len(folders) - success_count,
        "details": results
    }


async def get_index_stats() -> dict:
    """
    Get indexing statistics for all collections.
    
    Returns:
        dict with collection statistics
    """
    chroma_client = get_chroma_client()
    embedding_function = get_embedding_function()
    
    if not chroma_client:
        return {"error": "ChromaDB client not initialized"}
    
    collections = chroma_client.list_collections()
    stats = {
        "total_collections": len(collections),
        "collections": []
    }
    
    total_chunks = 0
    for collection in collections:
        # Handle both old API (strings) and new API (Collection objects)
        collection_name = collection.name if hasattr(collection, 'name') else str(collection)
        try:
            coll = chroma_client.get_collection(
                name=collection_name,
                embedding_function=embedding_function
            )
            count = coll.count()
            total_chunks += count
            stats["collections"].append({
                "name": collection_name,
                "chunks": count
            })
        except Exception as e:
            stats["collections"].append({
                "name": collection_name,
                "error": str(e)
            })
    
    stats["total_chunks"] = total_chunks
    return stats


def list_builds(tab: str, dashboard: str = None, limit: int = 10) -> dict:
    """
    List recent builds for a tab.
    
    Args:
        tab: TestGrid tab name
        dashboard: Dashboard name
        limit: Maximum number of builds
    
    Returns:
        dict with builds list
    """
    c = get_collector()
    builds = c.list_builds(tab, dashboard=dashboard, limit=limit)
    return {"builds": builds}


def list_tabs(dashboard: str) -> dict:
    """
    List available tabs for a dashboard.
    
    Args:
        dashboard: Dashboard name
    
    Returns:
        dict with tabs list
    """
    c = get_collector()
    tabs = c.list_tabs(dashboard)
    return {"dashboard": dashboard, "tabs": tabs}


def get_testgrid_summary(dashboard: str, tab: str = None) -> dict:
    """
    Get TestGrid dashboard summary.
    
    Args:
        dashboard: Dashboard name
        tab: Tab name (optional)
    
    Returns:
        dict with summary
    """
    c = get_collector()
    return c.get_testgrid_summary(dashboard, tab)


async def get_tab_status(dashboard: str = None, tabs: str = None) -> dict:
    """
    Get test results status for latest build of each tab.
    
    Args:
        dashboard: Dashboard name
        tabs: Comma-separated tab names to check (optional)
    
    Returns:
        dict with status for each tab
    """
    c = get_collector()
    dashboard = dashboard or os.getenv("DEFAULT_DASHBOARD", "sig-windows-signal")
    
    # Get list of tabs to check
    all_tabs = c.list_tabs(dashboard)
    if tabs:
        filter_tabs = [t.strip() for t in tabs.split(',')]
        all_tabs = [t for t in all_tabs if t in filter_tabs]
    
    results = []
    for tab in all_tabs:
        try:
            data = c.collect_from_tab(tab, dashboard=dashboard)
            build_id = data.get('build_id', 'unknown')
            test_results = data.get('test_results', {})
            failed = test_results.get('failed', 0)
            passed = test_results.get('passed', 0)
            skipped = test_results.get('skipped', 0)
            total = test_results.get('total', 0)
            
            # PASS only if: no failures AND at least one test ran
            if failed == 0 and total > 0:
                status = 'PASS'
            else:
                status = 'FAIL'
            
            results.append({
                'tab': tab,
                'build_id': build_id,
                'passed': passed,
                'failed': failed,
                'skipped': skipped,
                'total': total,
                'status': status,
                'error': None
            })
        except Exception as e:
            results.append({
                'tab': tab,
                'build_id': None,
                'passed': 0,
                'failed': 0,
                'skipped': 0,
                'total': 0,
                'status': 'ERROR',
                'error': str(e)
            })
    
    # Summary counts
    total_pass = sum(1 for r in results if r['status'] == 'PASS')
    total_fail = sum(1 for r in results if r['status'] == 'FAIL')
    total_error = sum(1 for r in results if r['status'] == 'ERROR')
    
    return {
        'dashboard': dashboard,
        'summary': {
            'passing': total_pass,
            'failing': total_fail,
            'errors': total_error,
            'total': len(results)
        },
        'tabs': results
    }
