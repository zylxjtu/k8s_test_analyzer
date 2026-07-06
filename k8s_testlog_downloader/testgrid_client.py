"""TestGrid client for fetching Kubernetes CI test dashboard info."""

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

TESTGRID_BASE_URL = "https://testgrid.k8s.io"


@dataclass
class TestGridJob:
    """Represents a job in TestGrid."""
    name: str
    dashboard: str
    status: str
    overall_status: str
    latest_build: Optional[str] = None
    prowjob_name: Optional[str] = None


class TestGridClient:
    """Client for accessing TestGrid API."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "k8s-test-analyzer/0.1.0",
            "Accept": "application/json"
        })

        # Configure retry strategy for transient network errors
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,  # Wait 1s, 2s, 4s between retries
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # Cache of resolved prowjob names keyed by (dashboard, tab) to avoid
        # repeatedly fetching the (relatively heavy) table endpoint.
        self._prowjob_cache: dict[tuple[str, str], str] = {}

    def _request_with_retry(self, url: str, max_retries: int = 3) -> Optional[requests.Response]:
        """Make a GET request with retry logic for connection errors.

        The HTTPAdapter handles HTTP-level retries (429, 5xx), but connection errors
        like RemoteDisconnected need explicit retry logic.
        """
        last_error = None
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, timeout=30)
                return response
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError) as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    logger.warning(f"Connection error (attempt {attempt + 1}/{max_retries}), "
                                   f"retrying in {wait_time}s: {e}")
                    time.sleep(wait_time)
            except requests.RequestException as e:
                # Non-retryable errors
                last_error = e
                break

        if last_error:
            raise last_error
        return None

    def list_dashboard_tabs(self, dashboard: str) -> list[str]:
        """List all available tabs for a dashboard."""
        url = f"{TESTGRID_BASE_URL}/{dashboard}/summary"
        try:
            response = self._request_with_retry(url)
            if response:
                response.raise_for_status()
                return sorted(response.json().keys())
            return []
        except requests.RequestException as e:
            logger.error(f"Failed to list dashboard tabs: {e}")
            return []
    
    def get_dashboard_summary(self, dashboard: str) -> dict:
        """Get summary of all jobs in a dashboard."""
        url = f"{TESTGRID_BASE_URL}/{dashboard}/summary"
        try:
            response = self._request_with_retry(url)
            if not response:
                return {"name": dashboard, "jobs": []}
            response.raise_for_status()
            data = response.json()
            
            jobs = []
            for tab_name, tab_data in data.items():
                overall = tab_data.get("overall_status", "UNKNOWN")
                # API returns strings like "PASSING", "FAILING", "FLAKY"
                if isinstance(overall, str):
                    overall_upper = overall.upper()
                    if overall_upper == "PASSING":
                        status = "PASSING"
                    elif overall_upper == "FAILING":
                        status = "FAILING"
                    elif overall_upper == "FLAKY":
                        status = "FLAKY"
                    else:
                        status = "UNKNOWN"
                else:
                    # Legacy integer format: 1=passing, 2=failing, 3=flaky
                    if overall == 1:
                        status = "PASSING"
                    elif overall == 2:
                        status = "FAILING"
                    elif overall == 3:
                        status = "FLAKY"
                    else:
                        status = "UNKNOWN"
                
                jobs.append(TestGridJob(
                    name=tab_name,
                    dashboard=dashboard,
                    status=status,
                    overall_status=str(overall)
                ))
            
            return {"name": dashboard, "jobs": jobs}
        except requests.RequestException as e:
            logger.error(f"Failed to fetch dashboard summary: {e}")
            return {"name": dashboard, "jobs": []}
    
    def get_tab_details(self, dashboard: str, tab: str) -> Optional[dict]:
        """Get detailed info about a specific tab."""
        url = f"{TESTGRID_BASE_URL}/api/v1/dashboards/{dashboard}/tabs/{tab}/headers"
        try:
            response = self._request_with_retry(url)
            if not response:
                return None
            response.raise_for_status()
            return {"headers": response.json(), "dashboard": dashboard, "tab": tab}
        except requests.RequestException as e:
            logger.error(f"Failed to fetch tab details: {e}")
            return None
    
    def get_recent_builds(self, dashboard: str, tab: str, limit: int = 10) -> list[dict]:
        """Get recent builds for a tab."""
        details = self.get_tab_details(dashboard, tab)
        if not details:
            return []
        
        headers = details.get('headers', {})
        build_ids = headers.get('build_ids', [])[:limit]
        timestamps = headers.get('timestamps', [])
        
        return [
            {"build_id": str(bid), "timestamp": timestamps[i] if i < len(timestamps) else None}
            for i, bid in enumerate(build_ids)
        ]
    
    def extract_job_name_from_url(self, testgrid_url: str) -> tuple[str, str]:
        """Extract dashboard and tab from TestGrid URL.
        
        URL format: https://testgrid.k8s.io/{dashboard}#{tab}
        Example: https://testgrid.k8s.io/sig-windows-signal#capz-windows-1-33-serial-slow
                                        └─────────────────┘└─────────────────────────────┘
                                            dashboard                   tab
        Returns:
            Tuple of (dashboard, tab). Tab may be empty if not in URL.
        """
        # Pattern 1: URL with both dashboard and tab (has #)
        # Example: https://testgrid.k8s.io/sig-windows-signal#capz-windows-1-33-serial-slow
        # Returns: ('sig-windows-signal', 'capz-windows-1-33-serial-slow')
        match = re.search(r'testgrid\.k8s\.io/([^#]+)#(.+)$', testgrid_url)
        if match:
            return match.group(1), match.group(2)
        
        # Pattern 2: URL with only dashboard (no #)
        # Example: https://testgrid.k8s.io/sig-windows-signal
        # Returns: ('sig-windows-signal', '')
        match = re.search(r'testgrid\.k8s\.io/([^?#]+)$', testgrid_url)
        if match:
            return match.group(1), ''
        
        # Invalid URL
        return '', ''
    
    def get_prowjob_name(self, dashboard: str, tab: str) -> Optional[str]:
        """Get prowjob name (GCS directory) for a TestGrid tab."""
        cache_key = (dashboard, tab)
        if cache_key in self._prowjob_cache:
            return self._prowjob_cache[cache_key]

        # The summary endpoint only lists *alerting* tests, so its '.Overall'
        # entry is absent for healthy tabs. Fall back to the table endpoint,
        # which always exposes the job's GCS location.
        prowjob = self._get_prowjob_from_summary(dashboard, tab)
        if not prowjob:
            prowjob = self._get_prowjob_from_table(dashboard, tab)
        if not prowjob:
            prowjob = self._pattern_based_job_name(tab)

        if prowjob:
            self._prowjob_cache[cache_key] = prowjob
        return prowjob

    def _get_prowjob_from_summary(self, dashboard: str, tab: str) -> Optional[str]:
        """Resolve the prowjob name from the dashboard summary.

        Only works when the tab currently has alerting tests, since the summary's
        'tests' array only contains failing tests (it is empty for healthy tabs).
        """
        url = f"{TESTGRID_BASE_URL}/{dashboard}/summary"
        try:
            response = self._request_with_retry(url)
            if response and response.status_code == 200:
                data = response.json()
                if tab in data:
                    tests = data[tab].get('tests', [])
                    for test in tests:
                        test_name = test.get('test_name', '')
                        if test_name.endswith('.Overall'):
                            return test_name.replace('.Overall', '')
                    if tests and '.' in tests[0].get('test_name', ''):
                        candidate = tests[0]['test_name'].split('.')[0]
                        # Validate it looks like a GCS job name, not a test suite name
                        # GCS job names are lowercase with hyphens (e.g., "ci-kubernetes-e2e-...")
                        if re.match(r'^[a-z0-9][a-z0-9-]+$', candidate):
                            return candidate
        except Exception as e:
            logger.warning(f"Failed to fetch prowjob name from summary: {e}")
        return None

    def _get_prowjob_from_table(self, dashboard: str, tab: str) -> Optional[str]:
        """Resolve the prowjob name from the TestGrid table endpoint.

        Unlike the summary endpoint, the table endpoint always reports the job's
        GCS location via the 'query' field (e.g.
        "kubernetes-ci-logs/logs/ci-kubernetes-e2enode-windows-master") and an
        "<job>.Overall" row, so it resolves correctly for healthy tabs too.
        """
        url = f"{TESTGRID_BASE_URL}/{dashboard}/table?tab={tab}"
        try:
            response = self._request_with_retry(url)
            if not (response and response.status_code == 200):
                return None
            data = response.json()

            # Primary: the 'query' field holds the full GCS path; the job name is
            # its last path component.
            query = data.get('query')
            if isinstance(query, str) and query.strip():
                candidate = query.rstrip('/').split('/')[-1]
                if re.match(r'^[a-z0-9][a-z0-9-]+$', candidate):
                    return candidate

            # Fallback: find the "<job>.Overall" row.
            for row in data.get('tests', []):
                name = row.get('name', '')
                if name.endswith('.Overall'):
                    candidate = name[:-len('.Overall')]
                    if re.match(r'^[a-z0-9][a-z0-9-]+$', candidate):
                        return candidate
        except Exception as e:
            logger.warning(f"Failed to fetch prowjob name from table: {e}")
        return None

    def _pattern_based_job_name(self, tab: str) -> Optional[str]:
        """Fallback pattern-based job name resolution."""
        if 'capz-windows' in tab:
            # Handle version tabs like "capz-windows-1.32" -> "ci-kubernetes-e2e-capz-master-windows-1-32"
            match = re.search(r'capz-windows-(\d+)\.(\d+)$', tab)
            if match:
                major, minor = match.groups()
                return f"ci-kubernetes-e2e-capz-master-windows-{major}-{minor}"
            
            # Handle tabs like "capz-windows-1-33-serial-slow" -> "ci-kubernetes-e2e-capz-1-33-windows-serial-slow"
            match = re.search(r'capz-windows-(\d+)-(\d+)-(.+)', tab)
            if match:
                major, minor, suffix = match.groups()
                return f"ci-kubernetes-e2e-capz-{major}-{minor}-windows-{suffix}"
        return None
