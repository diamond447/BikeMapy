#!/usr/bin/env python3
"""Validate the first line of a commit message against Conventional Commits."""

import re
import sys
from pathlib import Path

PATTERN = re.compile(
    r"^(build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)(\([^)]+\))?!?: .+"
)


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    lines = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    if not lines:
        print("Commit message must have a Conventional Commit subject.")
        return 1
    first_line = lines[0]
    if PATTERN.match(first_line):
        return 0
    print("Commit message must follow Conventional Commits (for example: feat: add routes).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
