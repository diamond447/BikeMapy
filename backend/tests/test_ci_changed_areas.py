"""Pull requests skip only the CI work their changes cannot affect."""

import subprocess
from pathlib import Path

import pytest
from scripts.ci_changed_areas import all_changed, changed_paths, classify, main


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (
            ["frontend/package.json", "frontend/pnpm-lock.yaml"],
            {"backend": False, "frontend": True, "e2e": True, "api_contract": True},
        ),
        (
            ["uv.lock"],
            {"backend": True, "frontend": False, "e2e": False, "api_contract": True},
        ),
        (
            [".github/workflows/ci.yml"],
            {"backend": True, "frontend": True, "e2e": False, "api_contract": True},
        ),
        (
            ["docs/architecture.md", "README.md"],
            {"backend": False, "frontend": False, "e2e": False, "api_contract": False},
        ),
        (
            ["deploy/restore.sh", "frontend/src/App.tsx"],
            {"backend": True, "frontend": True, "e2e": True, "api_contract": True},
        ),
    ],
)
def test_changed_paths_select_the_affected_job_groups(
    paths: list[str], expected: dict[str, bool]
) -> None:
    assert classify(paths) == expected


def test_runs_outside_pull_requests_report_every_area(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert main([]) == 0
    assert all(all_changed().values())
    assert output.read_text().splitlines() == [
        "backend=true",
        "frontend=true",
        "e2e=true",
        "api_contract=true",
    ]


def test_changed_paths_compare_against_the_merge_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.email=ci@example.test", "-c", "user.name=ci", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    (tmp_path / "README.md").write_text("base\n")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-q", "-b", "feature")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "App.tsx").write_text("change\n")
    git("add", ".")
    git("commit", "-q", "-m", "feature")
    head = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    (tmp_path / "uv.lock").write_text("unrelated change on main\n")
    git("add", ".")
    git("commit", "-q", "-m", "main moves on")
    monkeypatch.chdir(tmp_path)
    # Only the pull request's own change counts, not later commits on main.
    assert changed_paths(git("rev-parse", "HEAD"), head) == ["frontend/App.tsx"]
    assert changed_paths(base, head) == ["frontend/App.tsx"]
