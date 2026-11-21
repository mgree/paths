"""pytest hooks for the runner's tests."""

from runner import check


def pytest_report_header(config):
    """Say up front when a run will be slow, and how to skip the slow tests."""
    if not check.cpu_pairs():
        return (
            "only one CPU: the openat2 EXDEV tests run their own 5-second races "
            "(about 20 s more); skip them with -m 'not slow'"
        )
