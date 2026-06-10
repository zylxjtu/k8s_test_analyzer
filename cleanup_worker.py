#!/usr/bin/env python3
"""Cleanup worker subprocess.

Runs ChromaDB cleanup in an isolated child process so the kernel reclaims
the heavy in-memory state (loaded HNSW segments) on exit, instead of
letting it accumulate in the long-lived MCP server.

Invoked by mcp_server.run_download_and_cleanup() via asyncio subprocess.
Stdout: a single JSON line at the end with the cleanup result (or an
        error dict). Parent parses that line.
Stderr: info/error logs. Parent inherits this stream so they appear in
        docker logs without any IPC plumbing.
Exit:   0 on success, non-zero on init / cleanup failure.
"""

import argparse
import asyncio
import json
import logging
import os
import sys


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - [cleanup-worker] %(message)s",
        stream=sys.stderr,
    )


def _init_chromadb_for_cleanup():
    """Initialize chromadb client only — skip the embedding model.

    Cleanup does delete operations only (get(where=...) and delete(ids=...));
    neither needs the sentence-transformers model. Skipping the load saves
    ~5 s of subprocess startup time and ~200 MB of avoidable worker RSS.
    """
    import chromadb
    from chromadb.config import Settings
    import local_indexing

    local_indexing.config = local_indexing.get_config_from_env()

    chroma_db_path = os.path.join(local_indexing.config["projects_root"], "chroma_db")
    os.makedirs(chroma_db_path, exist_ok=True)

    local_indexing.chroma_client = chromadb.PersistentClient(
        path=chroma_db_path,
        settings=Settings(anonymized_telemetry=False),
    )
    # local_indexing.embedding_function intentionally left as None.
    # delete_build_from_index and the file-based completion markers don't touch it.


def main():
    parser = argparse.ArgumentParser(description="Run chromadb cleanup in an isolated subprocess.")
    parser.add_argument("--keep-builds", type=int, required=True,
                        help="Number of most recent builds to retain per job.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be deleted without actually deleting.")
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger(__name__)
    log.info("cleanup worker starting (pid=%d, keep_builds=%d, dry_run=%s)",
             os.getpid(), args.keep_builds, args.dry_run)

    try:
        _init_chromadb_for_cleanup()
    except Exception as e:
        log.error("ChromaDB init failed: %s", e, exc_info=True)
        print(json.dumps({"error": f"chromadb init failed: {e}"}))
        sys.exit(2)

    try:
        import core  # noqa: import after chromadb init so its module-level lookups resolve
        result = asyncio.run(core.cleanup_old_builds(
            keep_builds=args.keep_builds,
            dry_run=args.dry_run,
        ))
    except Exception as e:
        log.error("Cleanup failed: %s", e, exc_info=True)
        print(json.dumps({"error": f"cleanup failed: {e}"}))
        sys.exit(3)

    # Final stdout line: the JSON result (parent reads .strip().split("\n")[-1]).
    print(json.dumps(result, default=str))
    log.info("cleanup worker exiting cleanly")
    sys.exit(0)


if __name__ == "__main__":
    main()
