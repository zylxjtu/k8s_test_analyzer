"""TestGrid client for fetching Kubernetes CI test dashboard info."""

import logging
import re
from dataclasses import dataclass
from typing import Optional

import requests

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
    
    def list_dashboard_tabs(self, dashboard: str) -> list[str]:
        """List all available tabs for a dashboard."""
        url = f"{TESTGRID_BASE_URL}/{dashboard}/summary"
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return sorted(response.json().keys())
        except requests.RequestException as e:
            logger.error(f"Failed to list dashboard tabs: {e}")
            return []
    
    def get_dashboard_summary(self, dashboard: str) -> dict:
        """Get summary of all jobs in a dashboard."""
        url = f"{TESTGRID_BASE_URL}/{dashboard}/summary"
        try:
            response = self.session.get(url, timeout=30)
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
            response = self.session.get(url, timeout=30)
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
        url = f"{TESTGRID_BASE_URL}/{dashboard}/summary"
        try:
            response = self.session.get(url, timeout=30)
            if response.status_code == 200:
                data = response.json()
                if tab in data:
                    tests = data[tab].get('tests', [])
                    for test in tests:
                        test_name = test.get('test_name', '')
                        if test_name.endswith('.Overall'):
                            return test_name.replace('.Overall', '')
                    if tests and '.' in tests[0].get('test_name', ''):
                        return tests[0]['test_name'].split('.')[0]
        except Exception as e:
            logger.warning(f"Failed to fetch prowjob name: {e}")
        
        return self._pattern_based_job_name(tab)
    
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
