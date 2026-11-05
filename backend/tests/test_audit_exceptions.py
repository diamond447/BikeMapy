"""The dependency audits accept only documented, unexpired exceptions."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from scripts.check_audit_exceptions import (
    DEFAULT_PATH,
    ExceptionFileError,
    blocking_pnpm_advisories,
    load_exceptions,
    main,
    pip_audit_args,
)

TODAY = date(2026, 11, 5)


def _write(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "audit-exceptions.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _entry(**overrides: str) -> dict[str, str]:
    return {
        "id": "GHSA-aaaa-bbbb-cccc",
        "package": "example",
        "reason": "Not reachable: the vulnerable parser is never called.",
        "expires": "2026-12-31",
    } | overrides


def test_repository_exception_file_is_valid() -> None:
    assert load_exceptions(DEFAULT_PATH, today=date.today()).keys() == {"python", "frontend"}


def test_expired_exception_fails_the_audit(tmp_path: Path) -> None:
    path = _write(tmp_path, {"python": [_entry(expires="2026-11-04")], "frontend": []})
    with pytest.raises(ExceptionFileError, match="expired on 2026-11-04"):
        load_exceptions(path, today=TODAY)
    assert main(["validate", "--file", str(path), "--today", TODAY.isoformat()]) == 1


@pytest.mark.parametrize("field", ["id", "package", "reason", "expires"])
def test_exception_without_a_required_field_is_rejected(tmp_path: Path, field: str) -> None:
    path = _write(tmp_path, {"python": [_entry(**{field: ""})], "frontend": []})
    with pytest.raises(ExceptionFileError, match=field):
        load_exceptions(path, today=TODAY)


def test_active_python_exceptions_become_pip_audit_ignores(tmp_path: Path) -> None:
    path = _write(tmp_path, {"python": [_entry(id="PYSEC-2026-1")], "frontend": []})
    assert pip_audit_args(load_exceptions(path, today=TODAY)) == ["--ignore-vuln", "PYSEC-2026-1"]


def test_pnpm_report_blocks_only_unaccepted_high_and_critical_advisories(tmp_path: Path) -> None:
    path = _write(tmp_path, {"python": [], "frontend": [_entry(id="GHSA-accepted")]})
    report = {
        "advisories": {
            "1": {"severity": "critical", "github_advisory_id": "GHSA-accepted", "cves": []},
            "2": {"severity": "high", "github_advisory_id": "GHSA-open", "cves": ["CVE-1"]},
            "3": {"severity": "moderate", "github_advisory_id": "GHSA-minor", "cves": []},
        },
        "metadata": {"vulnerabilities": {"critical": 1, "high": 1, "moderate": 1}},
    }
    blocking = blocking_pnpm_advisories(report, load_exceptions(path, today=TODAY))
    assert [advisory["github_advisory_id"] for advisory in blocking] == ["GHSA-open"]
