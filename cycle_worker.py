#!/usr/bin/env python3
"""Per-cycle worker subprocess.

Runs the heavy parts of a scheduled cycle (download + index + cleanup) in
an isolated child process so:

  1. The MCP server's event loop is never starved by sentence-transformers
     holding the GIL during embedding forward passes. Clients stay
     responsive throughout indexing.
  2. The ~10 GiB of ChromaDB HNSW state that builds up during indexing +
     cleanup is reclaimed by the kernel when this worker exits, instead
     of accumulating in the long-lived MCP server.

Invoked by mcp_server.run_download_and_cleanup() via asyncio subprocess.
Stdout: a single JSON line at the end with the cycle result.
Stderr: info/error logs. Parent inherits this stream so they appear in
        docker logs without any IPC plumbing.
Exit:   0 on success, non-zero on init / cycle failure.
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
        format="%(asctime)s - %(levelname)s - [cycle-worker] %(message)s",
        stream=sys.stderr,
    )


async def _init_chromadb_full():
    """Initialize chromadb client AND the embedding model.

    Unlike cleanup_worker which skips the embedding model, the cycle worker
    needs it for indexing (upserts go through embedding_function to produce
    vectors before being stored in the collection).
    """
    import local_indexing
    success = await local_indexing.initialize_chromadb()
    if not success:
        raise RuntimeError("ChromaDB initialization failed")


async def _run_cycle(keep_builds: int, skip_indexing: bool, skip_cleanup: bool) -> dict:
    """Run one full cycle: download (+ optional index) → cleanup."""
    import core

    out: dict = {}

    # Download (and index, unless --skip-indexing).
    out["download_and_index"] = await core.download_all_and_index(
        skip_indexing=skip_indexing,
    )

    # Cleanup old builds — only if requested.
    if not skip_cleanup and keep_builds > 0:
        out["cleanup"] = await core.cleanup_old_builds(keep_builds=keep_builds)
    elif skip_cleanup:
        out["cleanup"] = {"skipped": True, "reason": "--skip-cleanup"}
    else:
        out["cleanup"] = {"skipped": True, "reason": "keep_builds <= 0"}

    return out


def main():
    parser = argparse.ArgumentParser(
        description="Run one download+index+cleanup cycle in an isolated subprocess.",
    )
    parser.add_argument("--keep-builds", type=int, required=True,
                        help="Number of most recent builds to retain per job during cleanup.")
    parser.add_argument("--skip-indexing", action="store_true",
                        help="Download only — do not index new builds.")
    parser.add_argument("--skip-cleanup", action="store_true",
                        help="Index only — do not delete old builds.")
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger(__name__)
    log.info(
        "cycle worker starting (pid=%d, keep_builds=%d, skip_indexing=%s, skip_cleanup=%s)",
        os.getpid(), args.keep_builds, args.skip_indexing, args.skip_cleanup,
    )

    try:
        asyncio.run(_init_chromadb_full())
    except Exception as e:
        log.error("ChromaDB init failed: %s", e, exc_info=True)
        print(json.dumps({"error": f"chromadb init failed: {e}"}))
        sys.exit(2)

    try:
        result = asyncio.run(_run_cycle(
            keep_builds=args.keep_builds,
            skip_indexing=args.skip_indexing,
            skip_cleanup=args.skip_cleanup,
        ))
    except Exception as e:
        log.error("Cycle failed: %s", e, exc_info=True)
        print(json.dumps({"error": f"cycle failed: {e}"}))
        sys.exit(3)

    # Final stdout line: the JSON result. Parent reads .strip().split("\n")[-1].
    print(json.dumps(result, default=str))
    log.info("cycle worker exiting cleanly")
    sys.exit(0)


if __name__ == "__main__":
    main()
