"""
Data models for test data collection.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TestStatus(Enum):
    """Status of a test run."""
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class TestCase:
    """Represents a single test case result."""
    name: str
    classname: str
    status: TestStatus
    duration_seconds: float = 0.0
    failure_message: Optional[str] = None
    failure_type: Optional[str] = None
    stack_trace: Optional[str] = None
    system_out: Optional[str] = None
    system_err: Optional[str] = None


@dataclass
class TestSuite:
    """Represents a test suite (collection of test cases)."""
    name: str
    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    time_seconds: float = 0.0
    test_cases: list[TestCase] = field(default_factory=list)
    
    @property
    def passed(self) -> int:
        return self.tests - self.failures - self.errors - self.skipped

