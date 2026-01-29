"""Data collector for Kubernetes CI tests - fetches data from TestGrid and GCS."""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .gcs_client import GCSClient
from .testgrid_client import TestGridClient
from .junit_parser import JUnitParser
from .models import TestStatus

logger = logging.getLogger(__name__)

DEFAULT_DASHBOARD = "sig-windows-signal"
DEFAULT_CACHE_DIR = "~/.k8s-test-analyzer/cache"


def load_config() -> dict:
    """Load config from environment variables and .env file.

    Environment variables take precedence over .env file values.
    This allows Docker containers to override settings via the environment.
    """
    paths = [
        os.environ.get('K8S_TEST_ANALYZER_CONFIG'),
        Path.cwd() / '.env',
        Path(__file__).parent.parent / '.env',
    ]
    config = {}
    # First, load from .env file
    for p in paths:
        if p and Path(p).exists():
            try:
                for line in Path(p).read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        config[key.strip()] = value.strip()
                break
            except Exception:
                pass

    # Then, override with environment variables (higher priority)
    for key in ['PROJECTS_ROOT', 'DEFAULT_DASHBOARD', 'FASTMCP_PORT',
                'SCHEDULE_INTERVAL_SECONDS', 'CLEANUP_KEEP_BUILDS']:
        env_value = os.environ.get(key)
        if env_value is not None:
            config[key] = env_value

    return config


def get_default_dashboard() -> str:
    return load_config().get('DEFAULT_DASHBOARD', DEFAULT_DASHBOARD)


def get_cache_dir() -> Path:
    cache_dir = load_config().get('PROJECTS_ROOT', DEFAULT_CACHE_DIR)
    return Path(cache_dir).expanduser()


class DataCollector:
    """Collects test data from TestGrid and GCS."""
    
    def __init__(self, cache_dir: Optional[Path] = None):
        self.gcs = GCSClient(cache_dir=cache_dir or get_cache_dir())
        self.testgrid = TestGridClient()
        self.junit = JUnitParser()
    
    def list_tabs(self, dashboard: Optional[str] = None) -> list[str]:
        """List available tabs for a dashboard."""
        return self.testgrid.list_dashboard_tabs(dashboard or get_default_dashboard())
    
    def collect_from_tab(self, tab: str, dashboard: Optional[str] = None,
                         build_id: Optional[str] = None, allow_unfinished: bool = False) -> dict:
        """Collect test data from a TestGrid tab name.
        
        Args:
            tab: Tab name (e.g., "capz-windows-1-33-serial-slow")
            dashboard: Dashboard name, uses default if not provided
            build_id: Specific build ID, or None for latest finished build
            allow_unfinished: If True, allow fetching builds that haven't finished
        """
        dashboard = dashboard or get_default_dashboard()
        job_name = self._get_gcs_job_name(dashboard, tab)
        data = self._collect_from_job(job_name, build_id, allow_unfinished=allow_unfinished)
        data["source"] = {"dashboard": dashboard, "tab": tab, "gcs_job_name": job_name}
        return data
    
    def _collect_from_job(self, job_name: str, build_id: Optional[str] = None, 
                         allow_unfinished: bool = False) -> dict:
        """Internal: Collect test data for a job.
        
        Args:
            job_name: Name of the CI job
            build_id: Specific build ID, or None for latest finished build
            allow_unfinished: If True, allow fetching builds that haven't finished
        """
        if not build_id:
            build_id = self.gcs.get_latest_build(job_name, finished_only=not allow_unfinished)
            if not build_id:
                return {"error": f"No finished builds found for: {job_name}"}
        elif not allow_unfinished and not self.gcs.is_build_finished(job_name, build_id):
            return {"error": f"Build {build_id} has not finished yet. Use --allow-unfinished to fetch anyway."}
        
        downloaded = self.gcs.download_build_logs(job_name, build_id, include_artifacts=True)
        
        return {
            "job_name": job_name,
            "build_id": build_id,
            "build_url": f"https://gcsweb.k8s.io/gcs/kubernetes-ci-logs/logs/{job_name}/{build_id}/",
            "collected_at": datetime.utcnow().isoformat(),
            "build_info": self._parse_build_info(downloaded),
            "test_results": self._parse_test_results(downloaded),
            "log_snippets": self._extract_log_snippets(downloaded),
        }
    
    def _get_gcs_job_name(self, dashboard: str, tab: str) -> str:
        """Map TestGrid tab to GCS job name."""
        prowjob = self.testgrid.get_prowjob_name(dashboard, tab)
        if prowjob:
            return prowjob
        
        # Fallback patterns for capz-windows tabs
        # Handle version tabs like "capz-windows-1.32" -> "ci-kubernetes-e2e-capz-master-windows-1-32"
        match = re.match(r'capz-windows-(\d+)\.(\d+)$', tab)
        if match:
            major, minor = match.groups()
            return f"ci-kubernetes-e2e-capz-master-windows-{major}-{minor}"

        # Handle tabs like "capz-windows-1-33-serial-slow" -> "ci-kubernetes-e2e-capz-1-33-windows-serial-slow"
        match = re.match(r'capz-windows-(\d+)-(\d+)-(.+)', tab)
        if match:
            return f"ci-kubernetes-e2e-capz-{match.group(1)}-{match.group(2)}-windows-{match.group(3)}"

        # Handle tabs like "capz-windows-master" or "capz-windows-master-suffix"
        # -> "ci-kubernetes-e2e-capz-master-windows" or "ci-kubernetes-e2e-capz-master-windows-suffix"
        match = re.match(r'capz-windows-(master|main)(?:-(.+))?$', tab)
        if match:
            branch = match.group(1)
            suffix = match.group(2)
            if suffix:
                return f"ci-kubernetes-e2e-capz-{branch}-windows-{suffix}"
            return f"ci-kubernetes-e2e-capz-{branch}-windows"

        # Handle tabs like "capz-windows-containerd-nightly-master"
        # -> "ci-kubernetes-e2e-capz-master-containerd-nightly-windows"
        match = re.match(r'capz-windows-containerd-nightly-(master|main)$', tab)
        if match:
            branch = match.group(1)
            return f"ci-kubernetes-e2e-capz-{branch}-containerd-nightly-windows"

        return f"ci-kubernetes-e2e-{tab}"
    
    def _parse_build_info(self, downloaded: dict[str, Path]) -> dict:
        """Parse build info from downloaded files."""
        info = {"status": "unknown", "started": None, "finished": None, "duration_minutes": None}
        
        started = downloaded.get('started.json')
        if started and started.exists():
            try:
                data = json.loads(started.read_text())
                info["started"] = datetime.fromtimestamp(data.get('timestamp', 0)).isoformat() if data.get('timestamp') else None
            except Exception:
                pass
        
        finished = downloaded.get('finished.json')
        if finished and finished.exists():
            try:
                data = json.loads(finished.read_text())
                info["finished"] = datetime.fromtimestamp(data.get('timestamp', 0)).isoformat() if data.get('timestamp') else None
                info["status"] = "passed" if data.get('passed') else "failed"
            except Exception:
                pass
        
        if info["started"] and info["finished"]:
            try:
                info["duration_minutes"] = (datetime.fromisoformat(info["finished"]) - datetime.fromisoformat(info["started"])).total_seconds() / 60
            except Exception:
                pass
        
        return info
    
    def _parse_test_results(self, downloaded: dict[str, Path]) -> dict:
        """Parse JUnit test results."""
        results = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "pass_rate": 0.0, "failed_tests": []}
        
        junit_files = [p for f, p in downloaded.items() if 'junit' in f.lower() and f.endswith('.xml')]
        
        for path in junit_files:
            try:
                for suite in self.junit.parse_file(path):
                    results["total"] += suite.tests
                    results["passed"] += suite.passed
                    results["failed"] += suite.failures
                    results["skipped"] += suite.skipped
                    
                    for test in suite.test_cases:
                        if test.status == TestStatus.FAILED:
                            results["failed_tests"].append({
                                "name": test.name,
                                "failure_message": test.failure_message,
                                "stack_trace": (test.stack_trace[:2000] + "...") if test.stack_trace and len(test.stack_trace) > 2000 else test.stack_trace
                            })
            except Exception:
                pass
        
        # Calculate pass rate excluding skipped tests
        executed = results["total"] - results["skipped"]
        if executed > 0:
            results["pass_rate"] = (results["passed"] / executed) * 100
        
        return results
    
    def _extract_log_snippets(self, downloaded: dict[str, Path]) -> dict:
        """Extract relevant log snippets."""
        snippets = {}
        build_log = downloaded.get('build-log.txt')
        if build_log and build_log.exists():
            try:
                lines = build_log.read_text(errors='replace').split('\n')
                snippets["build_log_tail"] = '\n'.join(lines[-200:])
                snippets["total_lines"] = len(lines)
            except Exception:
                pass
        return snippets
    
    def list_builds(self, tab: str, dashboard: Optional[str] = None, limit: int = 10) -> list[dict]:
        """List recent builds for a tab.
        
        Args:
            tab: TestGrid tab name (e.g., "capz-windows-1-33-serial-slow")
            dashboard: Dashboard name, uses default if not provided
            limit: Maximum number of builds to return (default: 10)
        """
        dashboard = dashboard or get_default_dashboard()
        job_name = self._get_gcs_job_name(dashboard, tab)
        return self._list_builds(job_name, limit)
    
    def _list_builds(self, job_name: str, limit: int = 10) -> list[dict]:
        """Internal: List recent builds by job name."""
        return [{"build_id": b, "url": f"https://gcsweb.k8s.io/gcs/kubernetes-ci-logs/logs/{job_name}/{b}/"} 
                for b in self.gcs.get_job_builds(job_name, limit)]
    
    def get_testgrid_summary(self, dashboard: Optional[str] = None, tab: Optional[str] = None) -> dict:
        """Get TestGrid summary."""
        dashboard = dashboard or get_default_dashboard()
        if tab:
            return {"dashboard": dashboard, "tab": tab}
        return self.testgrid.get_dashboard_summary(dashboard)
    
    def collect_all_tabs(self, dashboard: Optional[str] = None, limit: Optional[int] = None) -> dict:
        """Collect data for all tabs in a dashboard."""
        dashboard = dashboard or get_default_dashboard()
        tabs = self.testgrid.list_dashboard_tabs(dashboard)
        if limit:
            tabs = tabs[:limit]
        
        results = {"dashboard": dashboard, "tabs": {}}
        for tab in tabs:
            try:
                job = self._get_gcs_job_name(dashboard, tab)
                data = self._collect_from_job(job)
                results["tabs"][tab] = {
                    "gcs_job_name": job,
                    "build_id": data.get("build_id"),
                    "status": data.get("build_info", {}).get("status"),
                    "failed": data.get("test_results", {}).get("failed", 0),
                    "total": data.get("test_results", {}).get("total", 0),
                    "error": data.get("error")
                }
            except Exception as e:
                results["tabs"][tab] = {"error": str(e)}
        
        return results
