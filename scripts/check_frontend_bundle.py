"""Fail if a built browser bundle contains a server-only credential marker."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

# These names are intentionally broader than the current settings so adding a
# secret setting cannot accidentally make it into a public Vite bundle.
FORBIDDEN_MARKERS = (
    "DJANGO_SECRET_KEY",
    "POSTGRES_PASSWORD",
    "GITHUB_OAUTH_CLIENT_SECRET",
    "REPORT_TURNSTILE_SECRET_KEY",
    "REPORT_RATE_LIMIT_HMAC_SECRET",
    "RATE_LIMIT_HMAC_SECRET",
    "CELERY_BROKER_URL",
    "BEGIN PRIVATE KEY",
)
FORBIDDEN_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----"),
    re.compile(r"(?:ghp_|github_pat_|sk_(?:live|test)_|xox[baprs]-)[A-Za-z0-9_-]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)


def check_bundle(
    directory: Path,
    *,
    read_only: bool = False,
    forbidden_values: tuple[str, ...] = (),
) -> list[str]:
    findings: list[str] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix not in {".css", ".html", ".js", ".json", ".map"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in FORBIDDEN_MARKERS:
            if marker in text:
                findings.append(f"{path}: contains forbidden marker {marker!r}")
        for pattern in FORBIDDEN_VALUE_PATTERNS:
            if pattern.search(text):
                findings.append(f"{path}: contains a credential-shaped value")
        for value in forbidden_values:
            if value and value in text:
                findings.append(f"{path}: contains a forbidden environment value")
        if read_only and "/reports/" in text:
            findings.append(f"{path}: contains the report mutation endpoint")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="also reject the report mutation endpoint in preview bundles",
    )
    parser.add_argument(
        "--forbidden-value",
        action="append",
        default=[],
        help="literal value that must not occur in the bundle (repeatable)",
    )
    args = parser.parse_args()
    if not args.directory.is_dir():
        parser.error(f"bundle directory does not exist: {args.directory}")
    environment_values = tuple(
        value
        for name, value in os.environ.items()
        if any(marker in name.upper() for marker in ("SECRET", "PASSWORD", "TOKEN", "PRIVATE_KEY"))
        and len(value) >= 8
    )
    findings = check_bundle(
        args.directory,
        read_only=args.read_only,
        forbidden_values=tuple(args.forbidden_value) + environment_values,
    )
    if findings:
        print("Frontend bundle contains server-only credential markers:")
        print("\n".join(findings))
        return 1
    print(f"Frontend bundle secret scan passed: {args.directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
