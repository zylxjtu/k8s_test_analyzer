"""
Google Cloud Storage client for downloading Kubernetes CI logs.

Supports both authenticated (gsutil/gcloud) and unauthenticated (HTTP) access.
"""

import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# GCS bucket and paths
GCS_BUCKET = "kubernetes-ci-logs"
GCS_LOGS_PREFIX = "logs"
GCSWEB_BASE_URL = "https://gcsweb.k8s.io/gcs/"
GCS_STORAGE_URL = "https://storage.googleapis.com/"


class GCSClient:
    """Client for accessing Kubernetes CI logs from GCS."""
    
    def __init__(self, cache_dir: Optional[Path] = None, use_gsutil: bool = False):
        """
        Initialize GCS client.
        
        Args:
            cache_dir: Directory to cache downloaded files. Defaults to ~/.k8s-test-analyzer/cache
            use_gsutil: If True, use gsutil CLI for downloads (requires gcloud auth)
        """
        self.cache_dir = cache_dir or Path.home() / ".k8s-test-analyzer" / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.use_gsutil = use_gsutil
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "k8s-test-analyzer/0.1.0"
        })
    
    def get_job_builds(self, job_name: str, limit: int = 10) -> list[str]:
        """
        Get list of build IDs for a job, sorted by most recent first.
        
        Args:
            job_name: Name of the CI job (e.g., "ci-kubernetes-e2e-capz-1-33-windows-serial-slow")
            limit: Maximum number of builds to return
            
        Returns:
            List of build IDs (as strings)
        """
        url = f"{GCSWEB_BASE_URL}{GCS_BUCKET}/{GCS_LOGS_PREFIX}/{job_name}/"
        logger.info(f"Fetching build list from {url}")
        
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            builds = []
            
            for link in soup.find_all('a'):
                href = link.get('href', '')
                # Build IDs are numeric directories
                match = re.search(r'/(\d+)/$', href)
                if match:
                    builds.append(match.group(1))
            
            # Sort by build ID (numeric, descending = most recent first)
            builds.sort(key=lambda x: int(x), reverse=True)
            return builds[:limit]
            
        except requests.RequestException as e:
            logger.error(f"Failed to fetch build list: {e}")
            return []
    
    def is_build_finished(self, job_name: str, build_id: str) -> bool:
        """
        Check if a build has finished by checking if finished.json exists.
        
        Args:
            job_name: Name of the CI job
            build_id: Build ID to check
            
        Returns:
            True if build is finished, False otherwise
        """
        url = f"{GCS_STORAGE_URL}{GCS_BUCKET}/{GCS_LOGS_PREFIX}/{job_name}/{build_id}/finished.json"
        try:
            response = self.session.head(url, timeout=10)
            return response.status_code == 200
        except requests.RequestException:
            return False
    
    def get_latest_build(self, job_name: str, finished_only: bool = True) -> Optional[str]:
        """
        Get the latest build ID for a job.
        
        Args:
            job_name: Name of the CI job
            finished_only: If True, only return builds that have finished.json
            
        Returns:
            Build ID string, or None if no suitable build found
        """
        # Get list of recent builds
        builds = self.get_job_builds(job_name, limit=10)
        if not builds:
            return None
        
        if not finished_only:
            return builds[0]
        
        # Find the first finished build
        for build_id in builds:
            if self.is_build_finished(job_name, build_id):
                logger.info(f"Found latest finished build: {build_id}")
                return build_id
            else:
                logger.debug(f"Build {build_id} not finished, skipping")
        
        logger.warning(f"No finished builds found in last {len(builds)} builds")
        return None
    
    def list_artifacts(self, job_name: str, build_id: str, path: str = "") -> list[dict]:
        """
        List artifacts in a build directory.
        
        Args:
            job_name: Name of the CI job
            build_id: Build ID
            path: Subdirectory path within the build (e.g., "artifacts/")
            
        Returns:
            List of dicts with 'name', 'type' ('file' or 'dir'), 'size', 'url'
        """
        base_path = f"{GCS_BUCKET}/{GCS_LOGS_PREFIX}/{job_name}/{build_id}"
        if path:
            base_path = f"{base_path}/{path}"
        url = f"{GCSWEB_BASE_URL}{base_path}/"
        
        logger.debug(f"Listing artifacts at {url}")
        
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            items = []
            
            for row in soup.find_all('a'):
                href = row.get('href', '')
                name = row.get_text().strip()
                
                if name in ['..', ''] or href.endswith('..'):
                    continue
                
                is_dir = href.endswith('/')
                items.append({
                    'name': name.rstrip('/'),
                    'type': 'dir' if is_dir else 'file',
                    'url': href if href.startswith('http') else urljoin(url, href)
                })
            
            return items
            
        except requests.RequestException as e:
            logger.error(f"Failed to list artifacts: {e}")
            return []
    
    def download_file(self, job_name: str, build_id: str, file_path: str, 
                      force: bool = False) -> Optional[Path]:
        """
        Download a file from GCS, with caching.
        
        Args:
            job_name: Name of the CI job
            build_id: Build ID
            file_path: Path to file within build (e.g., "artifacts/junit_01.xml")
            force: If True, re-download even if cached
            
        Returns:
            Path to downloaded file, or None if download failed
        """
        # Construct cache path
        cache_path = self.cache_dir / job_name / build_id / file_path
        
        if cache_path.exists() and not force:
            logger.debug(f"Using cached file: {cache_path}")
            return cache_path
        
        # Construct download URL
        url = f"{GCS_STORAGE_URL}{GCS_BUCKET}/{GCS_LOGS_PREFIX}/{job_name}/{build_id}/{file_path}"
        
        logger.info(f"Downloading {url}")

        try:
            response = self.session.get(url, timeout=60, stream=True)
            response.raise_for_status()

            cache_path.parent.mkdir(parents=True, exist_ok=True)

            # Use atomic write: download to temp file, then rename on success
            # This prevents partial/corrupt files if download is interrupted
            temp_fd, temp_path = tempfile.mkstemp(dir=cache_path.parent, suffix='.tmp')
            try:
                with os.fdopen(temp_fd, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)

                # Atomic rename - only happens if download completed successfully
                os.rename(temp_path, cache_path)
                logger.debug(f"Saved to {cache_path}")
                return cache_path
            except Exception:
                # Clean up temp file on any failure
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise

        except requests.RequestException as e:
            logger.error(f"Failed to download {file_path}: {e}")
            return None
    
    def download_build_logs(self, job_name: str, build_id: str, 
                           include_artifacts: bool = True,
                           artifact_patterns: Optional[list[str]] = None) -> dict[str, Path]:
        """
        Download all relevant logs for a build.
        
        Args:
            job_name: Name of the CI job
            build_id: Build ID
            include_artifacts: Whether to download artifacts directory
            artifact_patterns: List of glob patterns for artifacts to download
                             (default: ["junit*.xml", "*.log", "*.json", "*.yaml", "*.txt"])
        
        Returns:
            Dict mapping file paths to local file paths
        """
        if artifact_patterns is None:
            artifact_patterns = ["junit*.xml", "*.log", "*.json", "*.yaml", "*.txt"]
        
        downloaded = {}
        
        # Core files to always download
        core_files = [
            "build-log.txt",
            "started.json",
            "finished.json",
            "prowjob.json",
            "clone-log.txt",
            "podinfo.json"
        ]
        
        for file_path in core_files:
            local_path = self.download_file(job_name, build_id, file_path)
            if local_path:
                downloaded[file_path] = local_path
        
        if include_artifacts:
            # Download from artifacts directory
            self._download_artifacts_recursive(
                job_name, build_id, "artifacts",
                artifact_patterns, downloaded
            )
        
        return downloaded
    
    def _download_artifacts_recursive(self, job_name: str, build_id: str, path: str,
                                       patterns: list[str], downloaded: dict[str, Path]):
        """Recursively download artifacts matching patterns from a directory."""
        items = self.list_artifacts(job_name, build_id, path)
        
        for item in items:
            if item['type'] == 'file':
                name = item['name']
                should_download = any(
                    self._matches_pattern(name, pattern)
                    for pattern in patterns
                )
                if should_download:
                    file_path = f"{path}/{name}"
                    local_path = self.download_file(job_name, build_id, file_path)
                    if local_path:
                        downloaded[file_path] = local_path
            elif item['type'] == 'dir':
                # Recurse into subdirectory
                self._download_artifacts_recursive(
                    job_name, build_id, f"{path}/{item['name']}",
                    patterns, downloaded
                )
    
    def _matches_pattern(self, filename: str, pattern: str) -> bool:
        """Check if filename matches a simple glob pattern."""
        import fnmatch
        return fnmatch.fnmatch(filename.lower(), pattern.lower())
    
    def get_file_content(self, job_name: str, build_id: str, file_path: str) -> Optional[str]:
        """
        Get content of a file, downloading if necessary.
        
        Args:
            job_name: Name of the CI job
            build_id: Build ID  
            file_path: Path to file within build
            
        Returns:
            File content as string, or None if not found
        """
        local_path = self.download_file(job_name, build_id, file_path)
        if local_path and local_path.exists():
            return local_path.read_text(errors='replace')
        return None
    
    def get_json_file(self, job_name: str, build_id: str, file_path: str) -> Optional[dict]:
        """Get and parse a JSON file."""
        content = self.get_file_content(job_name, build_id, file_path)
        if content:
            try:
                return json.loads(content)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON from {file_path}: {e}")
        return None
    
    def clear_cache(self, job_name: Optional[str] = None, build_id: Optional[str] = None):
        """
        Clear cached files.
        
        Args:
            job_name: If provided, only clear cache for this job
            build_id: If provided (along with job_name), only clear this build
        """
        if job_name and build_id:
            path = self.cache_dir / job_name / build_id
        elif job_name:
            path = self.cache_dir / job_name
        else:
            path = self.cache_dir
        
        if path.exists():
            shutil.rmtree(path)
            logger.info(f"Cleared cache: {path}")
