"""The CHANGELOG.md tooling used by CI and by the release workflow (.github/scripts)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "changelog", ROOT / ".github/scripts/changelog.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["changelog"] = module  # dataclasses look the module up while it loads
    spec.loader.exec_module(module)
    return module


changelog = load()

FIRST = """# Changelog

Intro.

## [Unreleased]

### Added

- Something new.

[Unreleased]: https://github.com/essedev/pgbee/commits/main
"""


def run(path: Path, *args: str) -> int:
    return int(changelog.main(["--file", str(path), *args]))


def test_the_repository_changelog_is_valid() -> None:
    assert run(ROOT / "CHANGELOG.md", "check") == 0


def test_release_cycle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(FIRST)
    assert run(path, "notes", "0.1.0") == 1
    assert run(path, "release", "0.1.0", "--date", "2026-09-27") == 0
    text = path.read_text()
    assert "## [Unreleased]\n\n## [0.1.0] - 2026-09-27\n\n### Added\n\n- Something new.\n" in text
    assert text.endswith(
        "[Unreleased]: https://github.com/essedev/pgbee/compare/v0.1.0...HEAD\n"
        "[0.1.0]: https://github.com/essedev/pgbee/releases/tag/v0.1.0\n"
    )
    capsys.readouterr()
    assert run(path, "notes", "0.1.0") == 0
    assert capsys.readouterr().out == "### Added\n\n- Something new.\n"
    # Nothing new since: a second release is refused, then allowed once Unreleased has content.
    assert run(path, "release", "0.2.0") == 1
    path.write_text(
        path.read_text().replace("## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n\n- A bug.\n")
    )
    assert run(path, "release", "0.1.1", "--date", "2026-10-01") == 0
    assert (
        "[0.1.1]: https://github.com/essedev/pgbee/compare/v0.1.0...v0.1.1\n[0.1.0]:"
        in path.read_text()
    )
    assert run(path, "check") == 0


@pytest.mark.parametrize(
    ("broken", "message"),
    [
        (FIRST.replace("## [Unreleased]", "## Unreleased"), "neither"),
        (FIRST.replace("### Added", "### New stuff"), "is not one of"),
        (
            FIRST.replace("[Unreleased]: https://github.com/essedev/pgbee/commits/main\n", ""),
            "missing link",
        ),
        (FIRST.replace("# Changelog", "# Changes"), "must start"),
    ],
)
def test_check_rejects_a_malformed_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], broken: str, message: str
) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(broken)
    assert run(path, "check") == 1
    assert message in capsys.readouterr().err


def test_release_refuses_an_older_version(tmp_path: Path) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(FIRST)
    assert run(path, "release", "0.2.0", "--date", "2026-09-27") == 0
    path.write_text(path.read_text().replace("## [Unreleased]\n", "## [Unreleased]\n\n- More.\n"))
    assert run(path, "release", "0.1.5") == 1
