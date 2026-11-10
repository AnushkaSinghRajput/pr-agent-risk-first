"""Deterministic file priorities for reviews that cannot fit the whole diff."""

from pathlib import PurePosixPath
from typing import Any

from pr_agent.algo.deterministic_risk import normalize_file_path


def _test_subject(path: str) -> str | None:
    """Return the source stem named by a conventional test filename."""
    filename = PurePosixPath(path).name
    stem = filename.rsplit(".", 1)[0]
    for suffix in (".test", ".spec", "_test", "_spec"):
        if stem.endswith(suffix):
            return stem[:-len(suffix)] or None
    if stem.startswith("test_"):
        return stem[5:] or None
    if any(part in {"test", "tests", "__tests__"} for part in PurePosixPath(path).parts[:-1]):
        return stem or None
    return None


def build_review_file_priority(diff_files: list[Any], risk_checks: dict | None) -> dict[str, int]:
    """Rank risk-shaped files first, then sensitive files and their changed tests."""
    if not diff_files or not risk_checks:
        return {}

    shape_paths = {
        normalize_file_path(item.get("file"))
        for item in risk_checks.get("risky_shapes", [])
        if isinstance(item, dict)
    }
    sensitive_paths = {
        normalize_file_path(item.get("file"))
        for item in risk_checks.get("sensitive_paths", [])
        if isinstance(item, dict)
    }
    priority = {}
    risky_stems = set()
    for file in diff_files:
        name = normalize_file_path(getattr(file, "filename", None))
        old_name = normalize_file_path(
            getattr(file, "old_filename", None) or getattr(file, "previous_filename", None)
        )
        if not name:
            continue
        paths = {name, old_name}
        level = 3 if paths & shape_paths else 2 if paths & sensitive_paths else 0
        if level:
            priority[name] = level
            # Keep both names so a rename like auth.py -> helpers.py still
            # pairs with tests/test_auth.py under the token budget.
            risky_stems.add(PurePosixPath(name).stem)
            if old_name:
                risky_stems.add(PurePosixPath(old_name).stem)

    for file in diff_files:
        name = normalize_file_path(getattr(file, "filename", None))
        if name and name not in priority and _test_subject(name) in risky_stems:
            priority[name] = 1
    return priority
