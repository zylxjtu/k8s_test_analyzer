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
    delete_build_from_index,
    sanitize_collection_name,
    get_build_chunks,
    get_indexing_lock,
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

    Indexing is incremental - only new builds will be indexed.
    Already indexed builds are skipped unless force_reindex=True.

    Args:
        tab: TestGrid tab name
        dashboard: Dashboard name (uses default if not specified)
        build_id: Specific build ID (fetches latest if not specified)
        skip_indexing: Skip indexing after download
        force_reindex: Force delete collection and re-index all builds

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

    Indexing is incremental - only new builds will be indexed.
    Already indexed builds are skipped unless force_reindex=True.

    Args:
        dashboard: Dashboard name (uses default if not specified)
        limit: Maximum number of tabs to fetch
        skip_indexing: Skip indexing after download
        force_reindex: Force delete collections and re-index all builds

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


async def get_all_latest_build_index_status(
    dashboard: str = None,
    tabs: list = None
) -> dict:
    """
    Get the indexing status for the latest build of each tab.

    Args:
        dashboard: Dashboard name (optional)
        tabs: List of tab names to check (optional, defaults to all tabs)

    Returns:
        dict with index status for each tab's latest build
    """
    c = get_collector()
    dashboard = dashboard or get_default_dashboard()

    # Get all tabs if not specified
    if tabs is None:
        tabs = c.list_tabs(dashboard)

    results = []
    for tab in tabs:
        try:
            job_name = c._get_gcs_job_name(dashboard, tab)
            latest_build_id = _get_latest_build_id(job_name)

            if not latest_build_id:
                results.append({
                    "tab": tab,
                    "job_name": job_name,
                    "status": "no_cached_builds",
                    "error": "No cached builds found"
                })
                continue

            # Get full status for this build
            status = await get_build_index_status(tab, latest_build_id, dashboard)
            results.append(status)
        except Exception as e:
            results.append({
                "tab": tab,
                "status": "error",
                "error": str(e)
            })

    # Summary counts
    indexed_count = sum(1 for r in results if r.get("status") == "indexed")
    cached_not_indexed = sum(1 for r in results if r.get("status") == "cached_not_indexed")
    not_cached = sum(1 for r in results if r.get("status") in ("not_cached", "no_cached_builds"))
    errors = sum(1 for r in results if r.get("status") == "error")

    return {
        "dashboard": dashboard,
        "summary": {
            "indexed": indexed_count,
            "cached_not_indexed": cached_not_indexed,
            "not_cached": not_cached,
            "errors": errors,
            "total": len(results)
        },
        "tabs": results
    }


async def get_build_index_status(
    tab: str,
    build_id: str,
    dashboard: str = None
) -> dict:
    """
    Get the indexing status of a specific build.

    Args:
        tab: TestGrid tab name
        build_id: Build ID to check
        dashboard: Dashboard name (optional)

    Returns:
        dict with build indexing status including:
        - indexed: whether the build is fully indexed
        - chunk_count: number of chunks for this build
        - collection: the collection name
        - cached: whether the build is cached on disk
    """
    from local_indexing import (
        _get_completed_builds,
        get_build_chunks,
        sanitize_collection_name,
        get_config
    )

    c = get_collector()
    dashboard = dashboard or get_default_dashboard()
    job_name = c._get_gcs_job_name(dashboard, tab)
    collection_name = sanitize_collection_name(job_name)
    config = get_config()

    result = {
        "tab": tab,
        "build_id": build_id,
        "job_name": job_name,
        "collection": collection_name,
        "dashboard": dashboard
    }

    # Check if build is cached on disk
    build_path = os.path.join(config["projects_root"], job_name, build_id)
    result["cached"] = os.path.exists(build_path)

    # Check if build is in the completed builds marker
    completed_builds = _get_completed_builds(collection_name)
    result["indexed"] = build_id in completed_builds

    # Get chunk count for this build
    chunks_result = get_build_chunks(collection_name, build_id)
    if chunks_result.get("success"):
        result["chunk_count"] = chunks_result.get("total", 0)
    else:
        result["chunk_count"] = 0
        if chunks_result.get("error"):
            result["chunks_error"] = chunks_result["error"]

    # Add status summary
    if result["indexed"] and result["chunk_count"] > 0:
        result["status"] = "indexed"
    elif result["cached"] and not result["indexed"]:
        result["status"] = "cached_not_indexed"
    elif not result["cached"]:
        result["status"] = "not_cached"
    else:
        result["status"] = "unknown"

    return result


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


import shutil


async def cleanup_old_builds(keep_builds: int = 10, dry_run: bool = False) -> dict:
    """
    Clean up old builds, keeping only the most recent N builds per job.
    Removes both the build folders and their corresponding ChromaDB index entries.

    Args:
        keep_builds: Number of most recent builds to keep per job (default: 10)
        dry_run: If True, only report what would be deleted without actually deleting

    Returns:
        dict with cleanup results
    """
    config = get_config()
    projects_root = config["projects_root"]

    if not os.path.exists(projects_root):
        return {"error": f"Projects root does not exist: {projects_root}"}

    # Get all job folders (excluding chroma_db and other system folders)
    ignore_dirs = set(config.get("ignore_dirs", []))
    ignore_dirs.add("chroma_db")
    ignore_dirs.add("container_cache")

    job_folders = []
    for item in os.listdir(projects_root):
        item_path = os.path.join(projects_root, item)
        if os.path.isdir(item_path) and item not in ignore_dirs and not item.startswith('.'):
            job_folders.append(item)

    if not job_folders:
        return {"message": "No job folders found", "jobs_processed": 0}

    results = {
        "jobs_processed": 0,
        "total_builds_deleted": 0,
        "total_chunks_deleted": 0,
        "total_space_freed_mb": 0,
        "dry_run": dry_run,
        "details": []
    }

    for job_name in job_folders:
        job_path = os.path.join(projects_root, job_name)
        job_result = await _cleanup_job_builds(job_name, job_path, keep_builds, dry_run)
        results["details"].append(job_result)
        results["jobs_processed"] += 1
        results["total_builds_deleted"] += job_result.get("builds_deleted", 0)
        results["total_chunks_deleted"] += job_result.get("chunks_deleted", 0)
        results["total_space_freed_mb"] += job_result.get("space_freed_mb", 0)

    logger.info(
        f"Cleanup complete: {results['total_builds_deleted']} builds deleted, "
        f"{results['total_chunks_deleted']} chunks removed, "
        f"{results['total_space_freed_mb']:.2f} MB freed"
    )

    return results


async def _cleanup_job_builds(job_name: str, job_path: str, keep_builds: int, dry_run: bool) -> dict:
    """
    Clean up old builds for a single job.

    Args:
        job_name: Name of the job (collection name)
        job_path: Path to the job folder
        keep_builds: Number of builds to keep
        dry_run: If True, only report without deleting

    Returns:
        dict with cleanup results for this job
    """
    result = {
        "job": job_name,
        "builds_deleted": 0,
        "chunks_deleted": 0,
        "space_freed_mb": 0,
        "builds_kept": [],
        "builds_removed": []
    }

    try:
        # List all build folders (they are numeric build IDs) with their timestamps
        builds_with_timestamps = []
        for item in os.listdir(job_path):
            item_path = os.path.join(job_path, item)
            if os.path.isdir(item_path) and item.isdigit():
                # Get timestamp from started.json for proper ordering
                started_file = os.path.join(item_path, "started.json")
                timestamp = 0
                if os.path.exists(started_file):
                    try:
                        with open(started_file, 'r') as f:
                            started_data = json.load(f)
                            timestamp = started_data.get("timestamp", 0)
                    except (json.JSONDecodeError, IOError):
                        pass
                builds_with_timestamps.append((item, timestamp))

        if not builds_with_timestamps:
            result["message"] = "No build folders found"
            return result

        # Sort by timestamp (descending - newest first)
        builds_with_timestamps.sort(key=lambda x: x[1], reverse=True)
        build_folders = [b[0] for b in builds_with_timestamps]

        # Split into keeps and deletes
        builds_to_keep = build_folders[:keep_builds]
        builds_to_delete = build_folders[keep_builds:]

        result["builds_kept"] = builds_to_keep

        if not builds_to_delete:
            result["message"] = f"Only {len(build_folders)} builds found, nothing to delete"
            return result

        collection_name = sanitize_collection_name(job_name)

        for build_id in builds_to_delete:
            build_path = os.path.join(job_path, build_id)

            # Calculate folder size before deletion
            folder_size_mb = _get_folder_size_mb(build_path)

            if dry_run:
                result["builds_removed"].append({
                    "build_id": build_id,
                    "size_mb": folder_size_mb,
                    "status": "would_delete"
                })
                result["builds_deleted"] += 1
                result["space_freed_mb"] += folder_size_mb
            else:
                # Acquire indexing lock to prevent concurrent ChromaDB writes
                # This prevents HNSW index corruption during cleanup
                async with get_indexing_lock():
                    # Delete from ChromaDB index first
                    index_result = delete_build_from_index(collection_name, build_id)
                    chunks_deleted = index_result.get("deleted_chunks", 0)

                # Delete the folder (outside the lock - doesn't need ChromaDB protection)
                try:
                    shutil.rmtree(build_path)
                    result["builds_removed"].append({
                        "build_id": build_id,
                        "size_mb": folder_size_mb,
                        "chunks_deleted": chunks_deleted,
                        "status": "deleted"
                    })
                    result["builds_deleted"] += 1
                    result["chunks_deleted"] += chunks_deleted
                    result["space_freed_mb"] += folder_size_mb
                    logger.info(f"Deleted build {build_id} from {job_name} ({folder_size_mb:.2f} MB, {chunks_deleted} chunks)")
                except Exception as e:
                    result["builds_removed"].append({
                        "build_id": build_id,
                        "status": "error",
                        "error": str(e)
                    })
                    logger.error(f"Failed to delete build folder {build_path}: {e}")

        return result

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Error cleaning up job {job_name}: {e}")
        return result


def _get_folder_size_mb(folder_path: str) -> float:
    """Calculate the total size of a folder in megabytes."""
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(folder_path):
        for filename in filenames:
            filepath = os.path.join(dirpath, filename)
            try:
                total_size += os.path.getsize(filepath)
            except (OSError, IOError):
                pass
    return total_size / (1024 * 1024)


import re


def _get_latest_build_id(job_name: str) -> str:
    """
    Get the latest (most recent) build ID for a job from the cache folder.
    Uses the timestamp from started.json to determine the newest build.

    Args:
        job_name: The GCS job name

    Returns:
        The latest build ID as a string, or None if no builds found
    """
    config = get_config()
    job_path = os.path.join(config["projects_root"], job_name)

    if not os.path.exists(job_path):
        return None

    # List all build folders and get their timestamps
    builds_with_timestamps = []
    for item in os.listdir(job_path):
        item_path = os.path.join(job_path, item)
        if os.path.isdir(item_path) and item.isdigit():
            # Try to read timestamp from started.json
            started_file = os.path.join(item_path, "started.json")
            timestamp = 0
            if os.path.exists(started_file):
                try:
                    with open(started_file, 'r') as f:
                        started_data = json.load(f)
                        timestamp = started_data.get("timestamp", 0)
                except (json.JSONDecodeError, IOError):
                    pass
            builds_with_timestamps.append((item, timestamp))

    if not builds_with_timestamps:
        return None

    # Sort by timestamp (descending) and return the latest
    builds_with_timestamps.sort(key=lambda x: x[1], reverse=True)
    return builds_with_timestamps[0][0]


async def get_indexed_build_logs(
    tab: str,
    build_id: str = None,
    dashboard: str = None,
    filter_type: str = "interesting",
    max_chunks: int = 50
) -> dict:
    """
    Get indexed/chunked log data for a specific build from ChromaDB.

    Args:
        tab: TestGrid tab name
        build_id: Build ID to retrieve (if None or "latest", uses most recent build)
        dashboard: Dashboard name (uses default if not specified)
        filter_type: "all", "interesting" (errors/failures), "errors", "failures"
        max_chunks: Maximum number of chunks to return per category (default 50)

    Returns:
        dict with build info and chunks organized by category
    """
    c = get_collector()
    dashboard = dashboard or get_default_dashboard()

    job_name = c._get_gcs_job_name(dashboard, tab)
    collection_name = sanitize_collection_name(job_name)

    # Resolve build_id if not specified or "latest"
    if build_id is None or build_id.lower() == "latest":
        build_id = _get_latest_build_id(job_name)
        if not build_id:
            return {"error": f"No builds found for job {job_name}"}
        logger.info(f"Using latest build: {build_id}")

    logger.info(f"Getting indexed logs for build {build_id} from {job_name}")

    # Get chunks (no embeddings needed)
    chunks_result = get_build_chunks(collection_name, build_id, include_embeddings=False)
    if not chunks_result.get("success"):
        return {"error": f"Failed to get chunks: {chunks_result.get('error')}"}

    all_chunks = chunks_result.get("chunks", [])
    if not all_chunks:
        return {"error": f"No indexed chunks found for build {build_id} in {job_name}. Make sure the build has been downloaded and indexed."}

    # Categorize and filter chunks
    categorized = _categorize_chunks(all_chunks, filter_type, max_chunks)

    return {
        "tab": tab,
        "build_id": build_id,
        "job": job_name,
        "total_indexed_chunks": len(all_chunks),
        "filter_type": filter_type,
        "chunks": categorized
    }


async def compare_indexed_builds(
    tab_a: str,
    build_id_a: str = None,
    tab_b: str = None,
    build_id_b: str = None,
    dashboard: str = None,
    filter_type: str = "interesting",
    max_chunks_per_build: int = 100
) -> dict:
    """
    Get indexed/chunked log data from two builds for comparison.

    This function retrieves and organizes chunks from both builds,
    filtering for interesting content (errors, failures, versions).
    The actual semantic comparison should be done by Claude LLM.

    Args:
        tab_a: TestGrid tab name for build A
        build_id_a: Build ID for build A (if None or "latest", uses most recent)
        tab_b: TestGrid tab name for build B (defaults to tab_a for same-job comparison)
        build_id_b: Build ID for build B (if None or "latest", uses most recent)
        dashboard: Dashboard name (uses default if not specified)
        filter_type: "all", "interesting", "errors", "failures"
        max_chunks_per_build: Max chunks to return per category per build (default 30)

    Returns:
        dict with indexed chunks from both builds organized for comparison
    """
    c = get_collector()
    dashboard = dashboard or get_default_dashboard()

    # Default tab_b to tab_a for same-job comparison
    if tab_b is None:
        tab_b = tab_a

    # Get job names
    job_a = c._get_gcs_job_name(dashboard, tab_a)
    job_b = c._get_gcs_job_name(dashboard, tab_b)

    collection_a = sanitize_collection_name(job_a)
    collection_b = sanitize_collection_name(job_b)

    # Resolve build IDs if not specified or "latest"
    if build_id_a is None or build_id_a.lower() == "latest":
        build_id_a = _get_latest_build_id(job_a)
        if not build_id_a:
            return {"error": f"No builds found for job {job_a}"}
        logger.info(f"Using latest build for A: {build_id_a}")

    if build_id_b is None or build_id_b.lower() == "latest":
        build_id_b = _get_latest_build_id(job_b)
        if not build_id_b:
            return {"error": f"No builds found for job {job_b}"}
        logger.info(f"Using latest build for B: {build_id_b}")

    logger.info(f"Comparing indexed builds: {job_a}/{build_id_a} vs {job_b}/{build_id_b}")

    # Get chunks for both builds
    chunks_a_result = get_build_chunks(collection_a, build_id_a, include_embeddings=False)
    if not chunks_a_result.get("success"):
        return {"error": f"Failed to get chunks for build A: {chunks_a_result.get('error')}"}

    chunks_b_result = get_build_chunks(collection_b, build_id_b, include_embeddings=False)
    if not chunks_b_result.get("success"):
        return {"error": f"Failed to get chunks for build B: {chunks_b_result.get('error')}"}

    chunks_a = chunks_a_result.get("chunks", [])
    chunks_b = chunks_b_result.get("chunks", [])

    if not chunks_a:
        return {"error": f"No indexed chunks found for build {build_id_a} in {job_a}. Make sure the build has been downloaded and indexed."}
    if not chunks_b:
        return {"error": f"No indexed chunks found for build {build_id_b} in {job_b}. Make sure the build has been downloaded and indexed."}

    # Categorize chunks
    categorized_a = _categorize_chunks(chunks_a, filter_type, max_chunks_per_build)
    categorized_b = _categorize_chunks(chunks_b, filter_type, max_chunks_per_build)

    return {
        "build_a": {
            "tab": tab_a,
            "build_id": build_id_a,
            "job": job_a,
            "total_indexed_chunks": len(chunks_a),
            "chunks": categorized_a
        },
        "build_b": {
            "tab": tab_b,
            "build_id": build_id_b,
            "job": job_b,
            "total_indexed_chunks": len(chunks_b),
            "chunks": categorized_b
        },
        "filter_type": filter_type,
        "instructions": "Compare the chunks from build_a and build_b. Identify: 1) New errors/failures in build_b that weren't in build_a, 2) Issues fixed in build_b that were in build_a, 3) Version/image changes, 4) Timing differences or timeouts"
    }


def _get_cached_builds_with_status(job_name: str, max_builds: int = 10) -> list:
    """
    Get cached builds with their status from finished.json.

    Args:
        job_name: The GCS job name
        max_builds: Maximum number of builds to return

    Returns:
        List of dicts with build_id, timestamp, and passed status, sorted by timestamp (newest first)
    """
    config = get_config()
    job_path = os.path.join(config["projects_root"], job_name)

    if not os.path.exists(job_path):
        return []

    builds = []
    for item in os.listdir(job_path):
        item_path = os.path.join(job_path, item)
        if os.path.isdir(item_path) and item.isdigit():
            build_info = {"build_id": item, "timestamp": 0, "passed": None}

            # Read timestamp from started.json
            started_file = os.path.join(item_path, "started.json")
            if os.path.exists(started_file):
                try:
                    with open(started_file, 'r') as f:
                        started_data = json.load(f)
                        build_info["timestamp"] = started_data.get("timestamp", 0)
                except (json.JSONDecodeError, IOError):
                    pass

            # Read status from finished.json
            finished_file = os.path.join(item_path, "finished.json")
            if os.path.exists(finished_file):
                try:
                    with open(finished_file, 'r') as f:
                        finished_data = json.load(f)
                        build_info["passed"] = finished_data.get("passed", None)
                except (json.JSONDecodeError, IOError):
                    pass

            builds.append(build_info)

    # Sort by timestamp (newest first) and limit
    builds.sort(key=lambda x: x["timestamp"], reverse=True)
    return builds[:max_builds]


def _find_regression_builds(
    tab: str,
    dashboard: str = None,
    max_builds: int = 10
) -> dict:
    """
    Find the last successful build and first failed build from cached builds.

    Only checks builds that are already downloaded - does NOT download new builds.
    Returns early if the most recent cached build is passing (no regression).
    Errors out if the latest finished build from TestGrid has not been downloaded.

    Args:
        tab: TestGrid tab name
        dashboard: Dashboard name (optional)
        max_builds: Maximum number of cached builds to check (default: 10)

    Returns:
        dict with regression_found, last_pass, first_fail, builds_checked, and reason
    """
    c = get_collector()
    dashboard = dashboard or get_default_dashboard()
    job_name = c._get_gcs_job_name(dashboard, tab)

    # Get cached builds with status
    builds = _get_cached_builds_with_status(job_name, max_builds)

    if not builds:
        return {
            "error": True,
            "regression_found": False,
            "reason": "No cached builds found. Use download_test to cache builds first.",
            "builds_checked": 0,
            "job_name": job_name
        }

    # Check if the latest finished build from TestGrid has been downloaded
    try:
        remote_builds = c.list_builds(tab, dashboard=dashboard, limit=1)
        if remote_builds:
            latest_remote_build_id = remote_builds[0]["build_id"]
            cached_build_ids = {b["build_id"] for b in builds}
            if latest_remote_build_id not in cached_build_ids:
                return {
                    "error": True,
                    "regression_found": False,
                    "reason": f"Latest build {latest_remote_build_id} has not been downloaded. Run download_test first.",
                    "latest_remote_build": latest_remote_build_id,
                    "cached_builds": list(cached_build_ids)[:5],
                    "suggestion": f"k8s-test-analyzer download --tab {tab}",
                    "job_name": job_name
                }
    except Exception as e:
        logger.warning(f"Could not check latest remote build: {e}")

    # Early exit if most recent build is passing
    most_recent = builds[0]
    if most_recent.get("passed") is True:
        return {
            "regression_found": False,
            "reason": "Most recent cached build is passing",
            "most_recent_build": {
                "build_id": most_recent["build_id"],
                "timestamp": most_recent["timestamp"],
                "status": "passed"
            },
            "builds_checked": 1,
            "job_name": job_name
        }

    # Find the pass→fail transition (scan from newest to oldest)
    first_fail = None
    last_pass = None

    for build in builds:
        passed = build.get("passed")

        if passed is False:
            # This is a failed build - potential first_fail
            first_fail = build
        elif passed is True and first_fail is not None:
            # Found a pass after seeing a fail - this is the transition point
            last_pass = build
            break

    if first_fail is None:
        # All builds are passing (shouldn't happen given early exit above)
        return {
            "regression_found": False,
            "reason": "All cached builds are passing",
            "builds_checked": len(builds),
            "job_name": job_name
        }

    if last_pass is None:
        # All cached builds are failing - regression happened before cached range
        return {
            "regression_found": False,
            "reason": "All cached builds are failing - regression happened before cached range",
            "builds_checked": len(builds),
            "oldest_cached_build": builds[-1]["build_id"] if builds else None,
            "suggestion": f"Download older builds with: k8s-test-analyzer download --tab {tab} --build <older-build-id>",
            "job_name": job_name
        }

    return {
        "regression_found": True,
        "last_pass": {
            "build_id": last_pass["build_id"],
            "timestamp": last_pass["timestamp"],
            "status": "passed"
        },
        "first_fail": {
            "build_id": first_fail["build_id"],
            "timestamp": first_fail["timestamp"],
            "status": "failed"
        },
        "builds_checked": len(builds),
        "job_name": job_name
    }


async def find_regression(
    tab: str,
    dashboard: str = None,
    max_builds: int = 10,
    filter_type: str = "interesting",
    max_chunks_per_build: int = 100
) -> dict:
    """
    Find and compare the last successful build with the first failed build.

    Only checks CACHED builds (already downloaded). Returns early if most recent
    build is passing (no regression to find).

    This function:
    1. Scans cached builds to find the pass→fail transition
    2. Returns early if most recent build is passing (no regression)
    3. Ensures both builds are indexed (calls index_project if needed)
    4. Compares them using compare_indexed_builds()

    Args:
        tab: TestGrid tab name
        dashboard: Dashboard name (optional)
        max_builds: Maximum number of cached builds to check (default: 10)
        filter_type: "all", "interesting", "errors", "failures"
        max_chunks_per_build: Max chunks to return per category per build

    Returns:
        dict with regression context and comparison results
    """
    from local_indexing import _get_completed_builds

    # Find the regression point
    regression_info = _find_regression_builds(tab, dashboard, max_builds)

    if not regression_info.get("regression_found"):
        return regression_info

    # We found a regression - get build IDs
    last_pass_id = regression_info["last_pass"]["build_id"]
    first_fail_id = regression_info["first_fail"]["build_id"]
    job_name = regression_info["job_name"]
    collection_name = sanitize_collection_name(job_name)

    # Check if builds are indexed
    completed_builds = _get_completed_builds(collection_name)
    last_pass_indexed = last_pass_id in completed_builds
    first_fail_indexed = first_fail_id in completed_builds

    # Index if needed (incremental - only indexes unindexed builds)
    if not last_pass_indexed or not first_fail_indexed:
        logger.info(f"Indexing builds for regression comparison (pass indexed: {last_pass_indexed}, fail indexed: {first_fail_indexed})")
        await index_project(job_name, force=False)

    # Now compare the builds
    comparison = await compare_indexed_builds(
        tab_a=tab,
        build_id_a=last_pass_id,
        tab_b=tab,
        build_id_b=first_fail_id,
        dashboard=dashboard,
        filter_type=filter_type,
        max_chunks_per_build=max_chunks_per_build
    )

    if "error" in comparison:
        return {
            "regression_found": True,
            "last_pass": regression_info["last_pass"],
            "first_fail": regression_info["first_fail"],
            "builds_checked": regression_info["builds_checked"],
            "comparison_error": comparison["error"]
        }

    return {
        "regression_found": True,
        "last_pass": regression_info["last_pass"],
        "first_fail": regression_info["first_fail"],
        "builds_checked": regression_info["builds_checked"],
        "comparison": comparison
    }


def _categorize_chunks(chunks: list, filter_type: str, max_per_category: int) -> dict:
    """
    Categorize chunks by content type.

    Args:
        chunks: List of chunk dicts with 'text' and 'metadata'
        filter_type: "all", "interesting", "errors", "failures"
        max_per_category: Maximum chunks per category

    Returns:
        dict with categorized chunks and counts
    """
    # Patterns for categorization
    error_pattern = re.compile(r'(error|exception|panic|fatal|traceback|stderr)', re.IGNORECASE)
    failure_pattern = re.compile(r'(fail|failed|failure|\bFAIL\b|FAILED)', re.IGNORECASE)
    version_pattern = re.compile(r'(v\d+\.\d+|version[:\s]+\d|kubernetes[:\s]+v?\d+\.\d+|image[:\s])', re.IGNORECASE)
    timing_pattern = re.compile(r'(timeout|timed out|duration|elapsed|took \d+|seconds|minutes)', re.IGNORECASE)

    categories = {
        "errors": [],
        "failures": [],
        "versions": [],
        "timing": [],
        "other": []
    }

    for chunk in chunks:
        text = chunk.get("text", "")
        metadata = chunk.get("metadata", {})

        chunk_summary = {
            "file": metadata.get("file_path", "unknown"),
            "lines": f"{metadata.get('start_line', '?')}-{metadata.get('end_line', '?')}",
            "text": text[:1000] if len(text) > 1000 else text  # Truncate for readability
        }

        # Categorize based on content
        if error_pattern.search(text):
            categories["errors"].append(chunk_summary)
        elif failure_pattern.search(text):
            categories["failures"].append(chunk_summary)
        elif version_pattern.search(text):
            categories["versions"].append(chunk_summary)
        elif timing_pattern.search(text):
            categories["timing"].append(chunk_summary)
        else:
            categories["other"].append(chunk_summary)

    # Apply filter
    if filter_type == "all":
        pass  # Return all categories
    elif filter_type == "interesting":
        # Remove "other" category for interesting filter
        categories["other"] = []
    elif filter_type == "errors":
        categories = {"errors": categories.get("errors", []), "failures": [], "versions": [], "timing": [], "other": []}
    elif filter_type == "failures":
        categories = {"errors": [], "failures": categories.get("failures", []), "versions": [], "timing": [], "other": []}

    # Limit chunks per category and add counts
    result = {}
    for cat, items in categories.items():
        result[cat] = items[:max_per_category]
        result[f"{cat}_count"] = len(items)

    return result
