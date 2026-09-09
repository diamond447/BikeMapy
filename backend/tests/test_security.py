"""Regression checks for the application's security defaults."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from scripts.check_codeql_sarif import actionable_findings

from config import settings as project_settings


def test_public_api_has_bounded_anonymous_and_authenticated_rates() -> None:
    rest_framework = cast(dict[str, Any], settings.REST_FRAMEWORK)
    throttle_classes = rest_framework["DEFAULT_THROTTLE_CLASSES"]
    rates = rest_framework["DEFAULT_THROTTLE_RATES"]

    assert "rest_framework.throttling.AnonRateThrottle" in throttle_classes
    assert "rest_framework.throttling.UserRateThrottle" in throttle_classes
    assert rates["anon"]
    assert rates["user"]


def test_production_transport_settings_enable_secure_cookies_and_hsts() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from config import settings; "
                "assert settings.SESSION_COOKIE_SECURE is True; "
                "assert settings.CSRF_COOKIE_SECURE is True; "
                "assert settings.SECURE_SSL_REDIRECT is True; "
                "assert settings.SECURE_HSTS_SECONDS >= 31536000; "
                "assert settings.SECURE_HSTS_INCLUDE_SUBDOMAINS is True; "
                "assert settings.SECURE_HSTS_PRELOAD is True"
            ),
        ],
        env={
            **os.environ,
            "DJANGO_DEBUG": "false",
            "DJANGO_DATABASE_ENGINE": "django.db.backends.sqlite3",
            "PYTHONPATH": "backend",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_production_transport_validation_rejects_insecure_cookie(monkeypatch: Any) -> None:
    monkeypatch.setattr(project_settings, "DEBUG", False)
    monkeypatch.setattr(project_settings, "SESSION_COOKIE_SECURE", False)

    with pytest.raises(ImproperlyConfigured, match="SESSION_COOKIE_SECURE"):
        project_settings.validate_production_security_settings()


def test_forum_responses_have_a_parser_byte_cap() -> None:
    assert settings.BIKEFORUM_MAX_BYTES > 0


def test_codeql_gate_joins_results_to_driver_rule_severity(tmp_path: Path) -> None:
    sarif = {
        "runs": [
            {
                "tool": {
                    "driver": {
                        "rules": [{"id": "py/ssrf", "properties": {"security-severity": "8.1"}}]
                    }
                },
                "results": [
                    {
                        "ruleIndex": 0,
                        "level": "warning",
                        "properties": {"security-severity": "0"},
                    }
                ],
            }
        ]
    }
    path = tmp_path / "results.sarif"
    path.write_text(json.dumps(sarif))
    result = subprocess.run(
        [sys.executable, "scripts/check_codeql_sarif.py", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "py/ssrf" in result.stdout


def test_codeql_gate_reports_result_file_and_location(tmp_path: Path) -> None:
    sarif = {
        "runs": [
            {
                "tool": {
                    "driver": {
                        "rules": [{"id": "js/example", "properties": {"security-severity": "7"}}]
                    }
                },
                "results": [
                    {
                        "ruleIndex": 0,
                        "level": "warning",
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "frontend/src/App.tsx"},
                                    "region": {"startLine": 428, "startColumn": 7},
                                }
                            }
                        ],
                    }
                ],
            }
        ]
    }
    path = tmp_path / "results.sarif"
    path.write_text(json.dumps(sarif))

    result = subprocess.run(
        [sys.executable, "scripts/check_codeql_sarif.py", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "file=frontend/src/App.tsx" in result.stdout
    assert "location=line 428, column 7" in result.stdout


def test_codeql_gate_resolves_extension_component_and_duplicate_rule_ids() -> None:
    findings = actionable_findings(
        {
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "CodeQL",
                            "rules": [
                                {
                                    "id": "duplicate",
                                    "properties": {"security-severity": "2.0"},
                                }
                            ],
                        },
                        "extensions": [
                            {
                                "name": "custom-pack",
                                "guid": "extension-guid",
                                "rules": [
                                    {
                                        "id": "duplicate",
                                        "properties": {"security-severity": "9.8"},
                                    }
                                ],
                            }
                        ],
                    },
                    "results": [
                        {
                            "rule": {
                                "id": "duplicate",
                                "index": 0,
                                "toolComponent": {"index": 0},
                            },
                            "level": "warning",
                        }
                    ],
                }
            ]
        }
    )
    assert findings == [{"ruleId": "duplicate", "level": "warning", "severity": 9.8}]


def test_codeql_gate_modern_driver_reference_without_component() -> None:
    findings = actionable_findings(
        {
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "rules": [{"id": "py/sql", "properties": {"security-severity": "7"}}]
                        },
                        "extensions": [
                            {"rules": [{"id": "py/sql", "properties": {"security-severity": "1"}}]}
                        ],
                    },
                    "results": [{"rule": {"id": "py/sql", "index": 0}, "level": "warning"}],
                }
            ]
        }
    )
    assert findings[0]["severity"] == 7.0


def test_codeql_gate_policy_threshold_and_fail_closed_resolution() -> None:
    def result(rule_id: str, level: str = "warning") -> dict[str, Any]:
        return {"ruleId": rule_id, "level": level}

    findings = actionable_findings(
        {
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "rules": [
                                {"id": "exact", "properties": {"security-severity": "7.0"}},
                                {"id": "low", "properties": {"security-severity": "6.9"}},
                            ]
                        }
                    },
                    "results": [
                        result("exact"),
                        result("low"),
                        {"level": "error"},
                        {"ruleId": "missing", "level": "warning"},
                    ],
                }
            ]
        }
    )
    assert [finding["ruleId"] for finding in findings] == ["exact", "", "missing"]


def test_codeql_gate_rejects_missing_or_malformed_sarif(tmp_path: Path) -> None:
    missing = subprocess.run(
        [sys.executable, "scripts/check_codeql_sarif.py", str(tmp_path / "missing")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 2

    malformed_path = tmp_path / "malformed.sarif"
    malformed_path.write_text("not json")
    malformed = subprocess.run(
        [sys.executable, "scripts/check_codeql_sarif.py", str(malformed_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed.returncode == 2
