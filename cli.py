#!/usr/bin/env python3
"""CLI for Kubernetes Test Analyzer."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from k8s_testlog_downloader.data_collector import DataCollector, get_default_dashboard


def setup_logging(verbose: bool = False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format='%(levelname)s: %(message)s'
    )


def cmd_fetch(args):
    """Fetch test data."""
    collector = DataCollector(cache_dir=Path(args.cache_dir) if args.cache_dir else None)
    
    if not args.tab:
        print("Error: provide --tab", file=sys.stderr)
        return 1
    
    data = collector.collect_from_tab(args.tab, build_id=args.build,
                                      allow_unfinished=args.allow_unfinished)
    
    # Check for errors
    if "error" in data:
        print(f"Error: {data['error']}", file=sys.stderr)
        return 1
    
    if args.format == 'json':
        print(json.dumps(data, indent=2, default=str))
    else:
        _print_summary(data)
    
    return 0 if data.get("build_info", {}).get("status") == "passed" else 1


def _print_summary(data: dict):
    """Print human-readable summary."""
    print(f"\n{'='*60}")
    print(f"Job: {data.get('job_name', 'N/A')}")
    print(f"Build: {data.get('build_id', 'N/A')}")
    
    build_info = data.get("build_info", {})
    print(f"Status: {build_info.get('status', 'N/A')}")
    print(f"Started: {build_info.get('started', 'N/A')}")
    print(f"Finished: {build_info.get('finished', 'N/A')}")
    if build_info.get('duration_minutes'):
        print(f"Duration: {build_info['duration_minutes']:.1f} minutes")
    
    results = data.get("test_results", {})
    if results:
        total = results.get('total', 0)
        passed = results.get('passed', 0)
        failed = results.get('failed', 0)
        skipped = results.get('skipped', 0)
        pass_rate = results.get('pass_rate', 0)
        
        print(f"\nTest Results:")
        print(f"  Total:   {total}")
        print(f"  Passed:  {passed}")
        print(f"  Failed:  {failed}")
        print(f"  Skipped: {skipped}")
        print(f"  Pass Rate: {pass_rate:.1f}%")
        
        failed_tests = results.get("failed_tests", [])
        if failed_tests:
            print(f"\nFailed Tests ({len(failed_tests)}):")
            for t in failed_tests[:10]:
                name = t.get("name", "")[:70]
                print(f"  - {name}")
            if len(failed_tests) > 10:
                print(f"  ... and {len(failed_tests) - 10} more")
    
    print(f"\nURL: {data.get('build_url', 'N/A')}")
    print(f"{'='*60}\n")


def cmd_list_tabs(_args):
    """List tabs for the configured dashboard."""
    collector = DataCollector()
    tabs = collector.list_tabs(get_default_dashboard())

    print(f"Tabs ({len(tabs)}):")
    for tab in tabs:
        print(f"  - {tab}")
    return 0


def cmd_list_builds(args):
    """List builds for a tab."""
    collector = DataCollector()
    dashboard = get_default_dashboard()
    builds = collector.list_builds(args.tab, dashboard=dashboard, limit=args.limit)
    
    print(f"Builds for {args.tab}:")
    for b in builds:
        print(f"  - {b['build_id']}")
    return 0


def cmd_summary(args):
    """Get dashboard summary."""
    from k8s_testlog_downloader.testgrid_client import TestGridClient

    client = TestGridClient()
    dashboard = get_default_dashboard()
    summary = client.get_dashboard_summary(dashboard)
    
    if args.format == 'json':
        import json
        # Convert dataclass objects to dicts for JSON serialization
        output = {
            "dashboard": summary["name"],
            "jobs": [{"name": j.name, "status": j.status, "overall_status": j.overall_status} 
                     for j in summary["jobs"]]
        }
        print(json.dumps(output, indent=2))
    else:
        passing = [j for j in summary["jobs"] if j.status == "PASSING"]
        failing = [j for j in summary["jobs"] if j.status == "FAILING"]
        flaky = [j for j in summary["jobs"] if j.status == "FLAKY"]
        unknown = [j for j in summary["jobs"] if j.status == "UNKNOWN"]
        
        print(f"Dashboard: {dashboard}")
        print(f"Total tabs: {len(summary['jobs'])}")
        print(f"  ✅ Passing: {len(passing)}")
        print(f"  ❌ Failing: {len(failing)}")
        print(f"  ⚠️  Flaky: {len(flaky)}")
        print(f"  ❓ Unknown: {len(unknown)}")
        
        if not args.quiet:
            if passing:
                print(f"\nPassing tabs:")
                for j in passing:
                    print(f"  - {j.name}")
            
            if failing:
                print(f"\nFailing tabs:")
                for j in failing:
                    print(f"  - {j.name}")
            
            if flaky:
                print(f"\nFlaky tabs:")
                for j in flaky:
                    print(f"  - {j.name}")
            
            if unknown:
                print(f"\nUnknown tabs:")
                for j in unknown:
                    print(f"  - {j.name}")
    return 0


def cmd_status(args):
    """Get test failure status for latest build of each tab."""
    import json as json_mod

    collector = DataCollector()
    dashboard = get_default_dashboard()
    tabs = collector.list_tabs(dashboard)
    
    if args.tab:
        # Filter to specific tabs
        filter_tabs = [t.strip() for t in args.tab.split(',')]
        tabs = [t for t in tabs if t in filter_tabs]
    
    print(f"Fetching status for {len(tabs)} tabs in {dashboard}...")
    print()
    
    results = []
    for tab in tabs:
        try:
            data = collector.collect_from_tab(tab, dashboard=dashboard)
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
    
    if args.format == 'json':
        print(json_mod.dumps({'dashboard': dashboard, 'tabs': results}, indent=2))
    else:
        # Calculate column widths
        max_tab_len = max(len(r['tab']) for r in results) if results else 10
        
        # Print header
        print(f"{'Tab':<{max_tab_len}}  {'Build':<15}  {'Status':<6}  {'Pass':>6}  {'Fail':>6}  {'Skip':>6}  {'Total':>6}")
        print(f"{'-'*max_tab_len}  {'-'*15}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*6}")
        
        for r in results:
            if r['status'] == 'ERROR':
                print(f"{r['tab']:<{max_tab_len}}  {'N/A':<15}  {'ERROR':<6}  {'-':>6}  {'-':>6}  {'-':>6}  {'-':>6}  ({r['error'][:30]}...)")
            else:
                status_icon = '✅' if r['status'] == 'PASS' else '❌'
                print(f"{r['tab']:<{max_tab_len}}  {r['build_id']:<15}  {status_icon:<6}  {r['passed']:>6}  {r['failed']:>6}  {r['skipped']:>6}  {r['total']:>6}")
        
        # Summary
        total_pass = sum(1 for r in results if r['status'] == 'PASS')
        total_fail = sum(1 for r in results if r['status'] == 'FAIL')
        total_error = sum(1 for r in results if r['status'] == 'ERROR')
        
        print()
        print(f"Summary: {total_pass} passing, {total_fail} failing, {total_error} errors")
    
    return 0


# =============================================================================
# MCP Tool Testing Commands (using shared core module)
# =============================================================================

def _run_async(coro):
    """Helper to run async functions from sync CLI."""
    return asyncio.run(coro)


def cmd_download(args):
    """Download and index test logs (mirrors MCP download_test tool)."""
    async def _download():
        from local_indexing import initialize_chromadb
        import core

        await initialize_chromadb()

        print(f"Downloading logs for tab: {args.tab}")

        result = await core.download_and_index(
            tab=args.tab,
            dashboard=None,
            build_id=args.build,
            skip_indexing=args.skip_indexing,
            force_reindex=args.force_reindex
        )
        
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            return 1
        
        print(f"Downloaded build: {result.get('build_id')}")
        
        if result.get("indexing"):
            idx = result["indexing"]
            if idx.get("documents_indexed", 0) > 0:
                print(f"Indexed: {idx.get('documents_indexed')} documents")
            elif idx.get("message") == "Already indexed":
                print("Already indexed (use --force-reindex to re-index)")
        
        if args.format == 'json':
            print(json.dumps(result, indent=2, default=str))
        
        return 0
    
    return _run_async(_download())


def cmd_download_all(args):
    """Download and index all tabs (mirrors MCP download_all_latest tool)."""
    async def _download_all():
        from local_indexing import initialize_chromadb
        import core

        await initialize_chromadb()

        dashboard = get_default_dashboard()
        print(f"Downloading all tabs from dashboard: {dashboard}")
        if args.limit:
            print(f"Limit: {args.limit} tabs")

        result = await core.download_all_and_index(
            dashboard=None,
            limit=args.limit,
            skip_indexing=args.skip_indexing,
            force_reindex=args.force_reindex
        )
        
        tabs_data = result.get("tabs", {})
        print(f"Downloaded {len(tabs_data)} tabs")
        
        if result.get("indexing_summary"):
            summary = result["indexing_summary"]
            print(f"Indexed: {summary.get('total_indexed', 0)} projects")
            print(f"Already indexed: {summary.get('already_indexed', 0)} projects")
        
        if args.format == 'json':
            print(json.dumps(result, indent=2, default=str))
        
        return 0
    
    return _run_async(_download_all())


def cmd_search(args):
    """Search indexed logs (mirrors MCP search_log tool)."""
    async def _search():
        from local_indexing import initialize_chromadb
        import core

        await initialize_chromadb()

        result = await core.search_logs(
            query=args.query,
            tab=args.tab,
            dashboard=None,
            n_results=args.n_results,
            threshold=args.threshold
        )
        
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            if result.get("available_collections"):
                print(f"Available collections: {result['available_collections']}", file=sys.stderr)
            return 1
        
        print(f"Searching in project: {result.get('project_name')}")
        print(f"Query: {args.query}")
        print()
        
        if args.format == 'json':
            print(json.dumps(result, indent=2))
        else:
            results = result.get("results", [])
            if results:
                for r in results:
                    print(f"[{r.get('relevance')}%] {r.get('file_path', 'Unknown')}")
                    print(f"  {r.get('text', '')[:200]}...")
                    print()
                print(f"Found {len(results)} results above {args.threshold}% threshold")
            else:
                print("No results found")
        
        return 0
    
    return _run_async(_search())


def cmd_reindex(args):
    """Reindex a specific folder (mirrors MCP reindex_folder tool)."""
    async def _reindex():
        from local_indexing import initialize_chromadb
        import core
        
        await initialize_chromadb()
        
        print(f"Reindexing project: {args.project}")
        result = await core.reindex_project(args.project)
        
        if result.get("success") or result.get("documents_indexed", 0) > 0:
            print(f"✓ Indexed {result.get('documents_indexed', 0)} documents")
        else:
            print(f"✗ Failed: {result.get('error', 'Unknown error')}")
        
        if args.format == 'json':
            print(json.dumps(result, indent=2, default=str))
        
        return 0 if result.get("success") or result.get("documents_indexed", 0) > 0 else 1
    
    return _run_async(_reindex())


def cmd_reindex_all(args):
    """Reindex all cached folders (mirrors MCP reindex_all tool)."""
    async def _reindex_all():
        from local_indexing import initialize_chromadb
        import core
        
        await initialize_chromadb()
        
        print("Reindexing all cached folders...")
        result = await core.reindex_all_projects()
        
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            return 1
        
        print(f"Total folders: {result.get('total_folders', 0)}")
        print(f"Successful: {result.get('successful', 0)}")
        print(f"Failed: {result.get('failed', 0)}")
        
        if args.format == 'json':
            print(json.dumps(result, indent=2, default=str))
        
        return 0
    
    return _run_async(_reindex_all())


def cmd_index_stats(args):
    """Get index statistics (mirrors MCP get_index_stats tool)."""
    async def _stats():
        from local_indexing import initialize_chromadb
        from k8s_testlog_downloader.data_collector import get_default_dashboard
        import core

        await initialize_chromadb()

        # If build ID is specified, get status for that specific build
        if args.build:
            if not args.tab:
                print("Error: --tab is required when specifying --build", file=sys.stderr)
                return 1

            result = await core.get_build_index_status(
                tab=args.tab,
                build_id=args.build,
                dashboard=None
            )

            if "error" in result:
                print(f"Error: {result['error']}", file=sys.stderr)
                return 1

            if args.format == 'json':
                print(json.dumps(result, indent=2))
            else:
                print(f"Build: {result.get('build_id')}")
                print(f"Tab: {result.get('tab')}")
                print(f"Job: {result.get('job_name')}")
                print(f"Collection: {result.get('collection')}")
                print()
                print(f"Status: {result.get('status')}")
                print(f"  Cached on disk: {result.get('cached')}")
                print(f"  Indexed: {result.get('indexed')}")
                print(f"  Chunk count: {result.get('chunk_count', 0)}")
                if result.get('chunks_error'):
                    print(f"  Chunks error: {result['chunks_error']}")

            return 0

        # If tab is specified but no build ID, get status for latest build of that tab
        if args.tab:
            result = await core.get_all_latest_build_index_status(
                dashboard=None,
                tabs=[args.tab]
            )

            if "error" in result:
                print(f"Error: {result['error']}", file=sys.stderr)
                return 1

            if args.format == 'json':
                print(json.dumps(result, indent=2))
            else:
                # Single tab - print detailed info
                if result.get("tabs"):
                    tab_result = result["tabs"][0]
                    if tab_result.get("status") == "no_cached_builds":
                        print(f"Tab: {tab_result.get('tab')}")
                        print(f"Job: {tab_result.get('job_name')}")
                        print(f"Status: No cached builds found")
                    elif tab_result.get("status") == "error":
                        print(f"Tab: {tab_result.get('tab')}")
                        print(f"Status: Error - {tab_result.get('error')}")
                    else:
                        print(f"Build: {tab_result.get('build_id')} (latest)")
                        print(f"Tab: {tab_result.get('tab')}")
                        print(f"Job: {tab_result.get('job_name')}")
                        print(f"Collection: {tab_result.get('collection')}")
                        print()
                        print(f"Status: {tab_result.get('status')}")
                        print(f"  Cached on disk: {tab_result.get('cached')}")
                        print(f"  Indexed: {tab_result.get('indexed')}")
                        print(f"  Chunk count: {tab_result.get('chunk_count', 0)}")
                        if tab_result.get('chunks_error'):
                            print(f"  Chunks error: {tab_result['chunks_error']}")

            return 0

        # No tab specified - default to --all behavior (get status for all latest builds)
        dashboard = get_default_dashboard()
        print(f"Fetching index status for latest builds in {dashboard}...")
        print()

        result = await core.get_all_latest_build_index_status(dashboard=None)

        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            return 1

        if args.format == 'json':
            print(json.dumps(result, indent=2))
        else:
            # Calculate column widths
            tabs = result.get("tabs", [])
            if not tabs:
                print("No tabs found")
                return 0

            max_tab_len = max(len(r.get('tab', '')) for r in tabs) if tabs else 10

            # Print header
            print(f"{'Tab':<{max_tab_len}}  {'Build':<15}  {'Status':<20}  {'Chunks':>8}  {'Indexed':>8}")
            print(f"{'-'*max_tab_len}  {'-'*15}  {'-'*20}  {'-'*8}  {'-'*8}")

            for r in tabs:
                tab_name = r.get('tab', 'unknown')
                build_id = r.get('build_id', 'N/A') or 'N/A'
                status = r.get('status', 'unknown')
                chunks = r.get('chunk_count', 0) or 0
                indexed = 'Yes' if r.get('indexed') else 'No'

                # Status icons
                if status == 'indexed':
                    status_display = '✅ indexed'
                elif status == 'cached_not_indexed':
                    status_display = '⚠️  cached (not indexed)'
                elif status == 'no_cached_builds':
                    status_display = '❌ no cached builds'
                    indexed = '-'
                    chunks = '-'
                elif status == 'error':
                    status_display = f"❌ error"
                    indexed = '-'
                    chunks = '-'
                else:
                    status_display = status

                print(f"{tab_name:<{max_tab_len}}  {str(build_id):<15}  {status_display:<20}  {str(chunks):>8}  {indexed:>8}")

            # Summary
            summary = result.get("summary", {})
            print()
            print(f"Summary: {summary.get('indexed', 0)} indexed, {summary.get('cached_not_indexed', 0)} cached (not indexed), "
                  f"{summary.get('not_cached', 0)} not cached, {summary.get('errors', 0)} errors")

        return 0

    return _run_async(_stats())


def cmd_cleanup(args):
    """Clean up old builds (mirrors MCP cleanup_builds tool)."""
    async def _cleanup():
        from local_indexing import initialize_chromadb
        import core

        await initialize_chromadb()

        action = "Would delete" if args.dry_run else "Deleting"
        print(f"{action} old builds, keeping {args.keep} most recent per job...")
        if args.dry_run:
            print("(Dry run - no changes will be made)")
        print()

        result = await core.cleanup_old_builds(keep_builds=args.keep, dry_run=args.dry_run)

        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            return 1

        if args.format == 'json':
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"Jobs processed: {result.get('jobs_processed', 0)}")
            print(f"Builds deleted: {result.get('total_builds_deleted', 0)}")
            print(f"Index chunks removed: {result.get('total_chunks_deleted', 0)}")
            print(f"Space freed: {result.get('total_space_freed_mb', 0):.2f} MB")

            if args.verbose:
                print()
                for job in result.get("details", []):
                    print(f"\n{job['job']}:")
                    if job.get("message"):
                        print(f"  {job['message']}")
                    else:
                        print(f"  Kept: {len(job.get('builds_kept', []))} builds")
                        print(f"  Removed: {len(job.get('builds_removed', []))} builds")
                        for removed in job.get("builds_removed", []):
                            status = removed.get("status", "unknown")
                            size = removed.get("size_mb", 0)
                            chunks = removed.get("chunks_deleted", 0)
                            print(f"    - {removed['build_id']}: {status} ({size:.2f} MB, {chunks} chunks)")

        return 0

    return _run_async(_cleanup())


def cmd_compare(args):
    """Compare indexed logs between two builds (mirrors MCP compare_indexed_builds tool)."""
    async def _compare():
        from local_indexing import initialize_chromadb
        import core

        await initialize_chromadb()

        tab_a = args.tab_a or args.tab
        tab_b = args.tab_b or args.tab

        if not tab_a:
            print("Error: Must specify --tab or --tab-a", file=sys.stderr)
            return 1

        result = await core.compare_indexed_builds(
            tab_a=tab_a,
            build_id_a=args.build_a,
            tab_b=tab_b,
            build_id_b=args.build_b,
            dashboard=None,
            filter_type=args.filter,
            max_chunks_per_build=args.max_chunks
        )

        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            return 1

        # Always output JSON - designed for LLM consumption
        print(json.dumps(result, indent=2, default=str))
        return 0

    return _run_async(_compare())


def cmd_find_regression(args):
    """Find and compare last pass with first fail (mirrors MCP find_regression tool)."""
    async def _find_regression():
        from local_indexing import initialize_chromadb
        import core

        await initialize_chromadb()

        print(f"Finding regression point for tab: {args.tab}")
        print(f"Checking up to {args.max_builds} cached builds...")
        print()

        result = await core.find_regression(
            tab=args.tab,
            dashboard=None,
            max_builds=args.max_builds,
            filter_type=args.filter,
            max_chunks_per_build=args.max_chunks
        )

        # Check for error (e.g., latest build not downloaded)
        if result.get("error"):
            print(f"Error: {result.get('reason', 'Unknown error')}")
            if result.get("latest_remote_build"):
                print(f"  Latest remote build: {result['latest_remote_build']}")
            if result.get("suggestion"):
                print(f"  Run: {result['suggestion']}")
            print()
            if args.format == 'json':
                print(json.dumps(result, indent=2, default=str))
            return 1

        if not result.get("regression_found"):
            # No regression found - print reason
            print(f"No regression found: {result.get('reason', 'Unknown')}")
            if result.get("most_recent_build"):
                build = result["most_recent_build"]
                print(f"  Most recent build: {build.get('build_id')} ({build.get('status')})")
            if result.get("suggestion"):
                print(f"  Suggestion: {result['suggestion']}")
            print()
            if args.format == 'json':
                print(json.dumps(result, indent=2, default=str))
            return 0

        # Regression found - print summary
        last_pass = result.get("last_pass", {})
        first_fail = result.get("first_fail", {})
        print(f"Regression found!")
        print(f"  Last passing build: {last_pass.get('build_id')}")
        print(f"  First failing build: {first_fail.get('build_id')}")
        print(f"  Builds checked: {result.get('builds_checked', 0)}")
        print()

        # Always output JSON for comparison data - designed for LLM consumption
        print(json.dumps(result, indent=2, default=str))
        return 0

    return _run_async(_find_regression())


def main():
    parser = argparse.ArgumentParser(description='Kubernetes CI Test Analyzer')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('--cache-dir', help='Cache directory for downloaded logs')
    
    sub = parser.add_subparsers(dest='command')
    
    # fetch (original)
    p = sub.add_parser('fetch', help='Fetch test data')
    p.add_argument('--tab', '-t', required=True, help='TestGrid tab name')
    p.add_argument('--build', '-b', help='Build ID')
    p.add_argument('--allow-unfinished', action='store_true', 
                   help='Allow fetching builds that have not finished yet')
    p.add_argument('--format', '-f', choices=['text', 'json'], default='text')
    
    # list-tabs (original)
    p = sub.add_parser('list-tabs', help='List dashboard tabs')

    # list-builds (original)
    p = sub.add_parser('list-builds', help='List builds for a tab')
    p.add_argument('--tab', '-t', required=True, help='TestGrid tab name')
    p.add_argument('--limit', type=int, default=20)

    # summary (original)
    p = sub.add_parser('summary', help='Get dashboard summary')
    p.add_argument('--format', '-f', choices=['text', 'json'], default='text')
    p.add_argument('--quiet', '-q', action='store_true', help='Only show counts, not failing tabs')

    # status (original)
    p = sub.add_parser('status', help='Get test status for latest build of each tab')
    p.add_argument('--tab', '-t', help='Specific tab(s) to check (comma-separated)')
    p.add_argument('--format', '-f', choices=['text', 'json'], default='text')
    
    # ===================
    # MCP Tool Commands
    # ===================
    
    # download (mirrors download_test)
    p = sub.add_parser('download', help='Download and index test logs (MCP: download_test)')
    p.add_argument('--tab', '-t', required=True, help='TestGrid tab name')
    p.add_argument('--build', '-b', help='Build ID')
    p.add_argument('--skip-indexing', action='store_true', help='Skip indexing after download')
    p.add_argument('--force-reindex', action='store_true', help='Delete collection and re-index all builds')
    p.add_argument('--format', '-f', choices=['text', 'json'], default='text')

    # download-all (mirrors download_all_latest)
    p = sub.add_parser('download-all', help='Download and index all tabs (MCP: download_all_latest)')
    p.add_argument('--limit', type=int, help='Max tabs to fetch')
    p.add_argument('--skip-indexing', action='store_true', help='Skip indexing after download')
    p.add_argument('--force-reindex', action='store_true', help='Delete collections and re-index all builds')
    p.add_argument('--format', '-f', choices=['text', 'json'], default='text')

    # search (mirrors search_log)
    p = sub.add_parser('search', help='Search indexed logs (MCP: search_log)')
    p.add_argument('query', help='Search query')
    p.add_argument('--tab', '-t', required=True, help='TestGrid tab name')
    p.add_argument('--n-results', '-n', type=int, default=5, help='Number of results')
    p.add_argument('--threshold', type=float, default=30.0, help='Minimum relevance percentage')
    p.add_argument('--format', '-f', choices=['text', 'json'], default='text')
    
    # reindex (mirrors reindex_folder)
    p = sub.add_parser('reindex', help='Reindex a specific project (MCP: reindex_folder)')
    p.add_argument('project', help='Project/folder name to reindex')
    p.add_argument('--format', '-f', choices=['text', 'json'], default='text')

    # reindex-all (mirrors reindex_all)
    p = sub.add_parser('reindex-all', help='Reindex all cached folders (MCP: reindex_all)')
    p.add_argument('--format', '-f', choices=['text', 'json'], default='text')
    
    # index-stats (mirrors get_build_index_status / get_all_latest_build_index_status)
    p = sub.add_parser('index-stats', help='Get index status for builds (MCP: get_build_index_status)')
    p.add_argument('--build', '-b', help='Check index status of a specific build ID (requires --tab)')
    p.add_argument('--tab', '-t', help='TestGrid tab name (uses latest build if --build not specified)')
    p.add_argument('--format', '-f', choices=['text', 'json'], default='text')

    # cleanup (mirrors cleanup_builds)
    p = sub.add_parser('cleanup', help='Clean up old builds (MCP: cleanup_builds)')
    p.add_argument('--keep', '-k', type=int, default=10, help='Number of builds to keep per job (default: 10)')
    p.add_argument('--dry-run', action='store_true', help='Show what would be deleted without deleting')
    p.add_argument('--format', '-f', choices=['text', 'json'], default='text')
    p.add_argument('-v', '--verbose', action='store_true', help='Show detailed output per job')

    # compare (mirrors compare_indexed_builds)
    p = sub.add_parser('compare', help='Compare indexed logs between two builds (MCP: compare_indexed_builds)')
    p.add_argument('--tab', '-t', help='TestGrid tab name (used for both builds if tab-a/tab-b not specified)')
    p.add_argument('--tab-a', help='TestGrid tab name for build A')
    p.add_argument('--tab-b', help='TestGrid tab name for build B (defaults to tab-a for same-job comparison)')
    p.add_argument('--build-a', help='Build ID for build A (default: latest)')
    p.add_argument('--build-b', help='Build ID for build B (default: latest)')
    p.add_argument('--filter', choices=['all', 'interesting', 'errors', 'failures'], default='interesting',
                   help='Filter type for chunks (default: interesting)')
    p.add_argument('--max-chunks', type=int, default=100, help='Max chunks per category per build (default: 100)')

    # find-regression (mirrors find_regression)
    p = sub.add_parser('find-regression', help='Find and compare last pass with first fail (MCP: find_regression)')
    p.add_argument('--tab', '-t', required=True, help='TestGrid tab name')
    p.add_argument('--max-builds', type=int, default=10, help='Max cached builds to check (default: 10)')
    p.add_argument('--filter', choices=['all', 'interesting', 'errors', 'failures'], default='interesting',
                   help='Filter type for chunks (default: interesting)')
    p.add_argument('--max-chunks', type=int, default=100, help='Max chunks per category per build (default: 100)')
    p.add_argument('--format', '-f', choices=['text', 'json'], default='text')

    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    setup_logging(args.verbose)
    
    cmds = {
        'fetch': cmd_fetch,
        'list-tabs': cmd_list_tabs,
        'list-builds': cmd_list_builds,
        'summary': cmd_summary,
        'status': cmd_status,
        # MCP tool commands
        'download': cmd_download,
        'download-all': cmd_download_all,
        'search': cmd_search,
        'reindex': cmd_reindex,
        'reindex-all': cmd_reindex_all,
        'index-stats': cmd_index_stats,
        'cleanup': cmd_cleanup,
        'compare': cmd_compare,
        'find-regression': cmd_find_regression,
    }
    return cmds[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
