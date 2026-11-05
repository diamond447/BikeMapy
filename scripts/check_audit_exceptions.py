"""Apply the documented, expiring dependency-audit exceptions.

The exceptions live in ``.github/audit-exceptions.json``::

    {
      "python": [{"id": "GHSA-...", "package": "name", "reason": "...", "expires": "2026-12-31"}],
      "frontend": [...]
    }

Every exception needs a reason and an expiry date. An expired exception fails
the audit, so an accepted finding is reviewed again instead of being ignored
forever.

Commands:

``validate``
    Check the file and fail on a malformed or expired exception.
``pip-audit-args``
    Print ``--ignore-vuln`` arguments for the active Python exceptions.
``pnpm``
    Read ``pnpm audit --json`` from stdin and fail on a high or critical
    advisory without an active frontend exception.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parents[1] / ".github" / "audit-exceptions.json"
BLOCKING_SEVERITIES = frozenset({"high", "critical"})
REQUIRED_FIELDS = ("id", "package", "reason", "expires")


class ExceptionFileError(ValueError):
    """The exception file is malformed or contains an expired exception."""


def load_exceptions(path: Path, *, today: date) -> dict[str, list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) - {"python", "frontend"}:
        raise ExceptionFileError('the file must be an object with "python" and "frontend" lists')
    result: dict[str, list[dict[str, Any]]] = {}
    for ecosystem in ("python", "frontend"):
        entries = data.get(ecosystem, [])
        if not isinstance(entries, list):
            raise ExceptionFileError(f"{ecosystem} must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ExceptionFileError(f"{ecosystem} entries must be objects")
            missing = [field for field in REQUIRED_FIELDS if not str(entry.get(field, "")).strip()]
            if missing:
                raise ExceptionFileError(
                    f"{ecosystem} exception {entry.get('id', '?')} lacks {', '.join(missing)}"
                )
            try:
                expires = date.fromisoformat(str(entry["expires"]))
            except ValueError as exc:
                raise ExceptionFileError(
                    f"{ecosystem} exception {entry['id']} has an invalid expiry date"
                ) from exc
            if expires < today:
                raise ExceptionFileError(
                    f"{ecosystem} exception {entry['id']} ({entry['package']}) expired on "
                    f"{expires.isoformat()}; fix the finding or renew the exception with a reason"
                )
        result[ecosystem] = entries
    return result


def pip_audit_args(exceptions: dict[str, list[dict[str, Any]]]) -> list[str]:
    args: list[str] = []
    for entry in exceptions["python"]:
        args.extend(["--ignore-vuln", str(entry["id"])])
    return args


def blocking_pnpm_advisories(
    report: dict[str, Any], exceptions: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    accepted = {str(entry["id"]).upper() for entry in exceptions["frontend"]}
    blocking = []
    for advisory in (report.get("advisories") or {}).values():
        if str(advisory.get("severity", "")).lower() not in BLOCKING_SEVERITIES:
            continue
        identifiers = {
            str(advisory.get("github_advisory_id", "")).upper(),
            *(str(cve).upper() for cve in advisory.get("cves") or []),
        }
        if identifiers & accepted:
            continue
        blocking.append(advisory)
    return blocking


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("validate", "pip-audit-args", "pnpm"))
    parser.add_argument("--file", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    args = parser.parse_args(argv)
    try:
        exceptions = load_exceptions(args.file, today=args.today)
    except (OSError, json.JSONDecodeError, ExceptionFileError) as exc:
        print(f"audit exceptions: {exc}", file=sys.stderr)
        return 1
    if args.command == "validate":
        return 0
    if args.command == "pip-audit-args":
        print(" ".join(pip_audit_args(exceptions)))
        return 0
    report = json.load(sys.stdin)
    blocking = blocking_pnpm_advisories(report, exceptions)
    for advisory in blocking:
        print(
            f"{advisory.get('severity')}: {advisory.get('module_name')} "
            f"{advisory.get('github_advisory_id')} {advisory.get('title', '')} "
            f"{advisory.get('url', '')}",
            file=sys.stderr,
        )
    vulnerabilities = (report.get("metadata") or {}).get("vulnerabilities", {})
    print(f"pnpm audit: {vulnerabilities}; blocking without an exception: {len(blocking)}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
