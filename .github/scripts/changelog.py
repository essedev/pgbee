"""CHANGELOG.md tooling, standard library only.

The changelog follows Keep a Changelog (https://keepachangelog.com/en/1.1.0/): a `## [Unreleased]`
section on top, then one `## [X.Y.Z] - YYYY-MM-DD` section per release, newest first, with
`### Added`, `### Changed`, ... subsections, and link references at the bottom.

    changelog.py check                  validate the file (CI)
    changelog.py notes 0.1.0            print the section of a release (the GitHub release body)
    changelog.py release 0.1.0 [--date YYYY-MM-DD]
                                        turn Unreleased into 0.1.0 and open a new empty Unreleased
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_URL = "https://github.com/essedev/pgbee"
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
SUBSECTIONS = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security", "Upgrading")
UNRELEASED = "## [Unreleased]"
VERSION_HEADING = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\] - (\d{4}-\d{2}-\d{2})$")
LINK = re.compile(r"^\[([^\]]+)\]: (\S+)$")


class ChangelogError(Exception):
    pass


@dataclass
class Section:
    heading: str
    version: str | None  # None for Unreleased
    date: str | None
    body: list[str]


@dataclass
class Changelog:
    preamble: list[str]
    sections: list[Section]
    links: dict[str, str]

    @classmethod
    def parse(cls, text: str) -> Changelog:
        lines = text.splitlines()
        links: dict[str, str] = {}
        while lines and (not lines[-1].strip() or LINK.match(lines[-1])):
            match = LINK.match(lines.pop())
            if match:
                links[match.group(1)] = match.group(2)
        preamble: list[str] = []
        sections: list[Section] = []
        for line in lines:
            if line.startswith("## "):
                sections.append(_heading(line))
            elif sections:
                sections[-1].body.append(line)
            else:
                preamble.append(line)
        return cls(preamble, sections, dict(reversed(list(links.items()))))

    def render(self) -> str:
        out = list(self.preamble)
        for section in self.sections:
            out.append(section.heading)
            out.extend(section.body)
        while out and not out[-1].strip():
            out.pop()
        out.append("")
        out.extend(f"[{name}]: {url}" for name, url in self.links.items())
        return "\n".join(out) + "\n"

    def check(self) -> None:
        if not self.preamble or self.preamble[0] != "# Changelog":
            raise ChangelogError("the file must start with '# Changelog'")
        if not self.sections or self.sections[0].version is not None:
            raise ChangelogError(f"the first section must be '{UNRELEASED}'")
        if any(s.version is None for s in self.sections[1:]):
            raise ChangelogError("only one Unreleased section, on top")
        versions = [_key(s.version) for s in self.sections[1:] if s.version]
        if versions != sorted(versions, reverse=True) or len(set(versions)) != len(versions):
            raise ChangelogError("releases must be listed newest first, each once")
        for section in self.sections:
            for line in section.body:
                if line.startswith("### ") and line[4:] not in SUBSECTIONS:
                    raise ChangelogError(
                        f"{section.heading}: '{line}' is not one of {', '.join(SUBSECTIONS)}"
                    )
                if line.startswith("#") and not line.startswith("### "):
                    raise ChangelogError(f"{section.heading}: unexpected heading '{line}'")
            if section.version and not _content(section):
                raise ChangelogError(f"{section.heading} is empty")
            name = section.version or "Unreleased"
            if name not in self.links:
                raise ChangelogError(f"missing link reference [{name}]: at the bottom")

    def notes(self, version: str) -> str:
        for section in self.sections:
            if section.version == version:
                return "\n".join(_content(section)).strip() + "\n"
        raise ChangelogError(
            f"no section for {version}: run `make changelog-release version={version}`"
        )

    def release(self, version: str, date: str) -> None:
        _key(version)
        datetime.date.fromisoformat(date)
        if any(s.version == version for s in self.sections):
            raise ChangelogError(f"{version} is already in the changelog")
        unreleased = self.sections[0]
        if not _content(unreleased):
            raise ChangelogError("Unreleased is empty: nothing to release")
        released = [s for s in self.sections[1:] if s.version]
        if released and _key(version) <= _key(released[0].version or ""):
            raise ChangelogError(f"{version} is not newer than {released[0].version}")
        self.sections[0] = Section(UNRELEASED, None, None, [""])
        self.sections.insert(1, Section(f"## [{version}] - {date}", version, date, unreleased.body))
        previous = released[0].version if released else None
        self.links["Unreleased"] = f"{REPO_URL}/compare/v{version}...HEAD"
        self.links[version] = (
            f"{REPO_URL}/compare/v{previous}...v{version}"
            if previous
            else f"{REPO_URL}/releases/tag/v{version}"
        )
        ordered = {"Unreleased": self.links["Unreleased"]}
        for section in self.sections[1:]:
            if section.version:
                ordered[section.version] = self.links[section.version]
        self.links = ordered


def _heading(line: str) -> Section:
    if line == UNRELEASED:
        return Section(line, None, None, [])
    match = VERSION_HEADING.match(line)
    if not match:
        raise ChangelogError(f"'{line}' is neither '{UNRELEASED}' nor '## [X.Y.Z] - YYYY-MM-DD'")
    try:
        datetime.date.fromisoformat(match.group(4))
    except ValueError as exc:
        raise ChangelogError(f"'{line}': {exc}") from exc
    version = ".".join(match.group(i) for i in (1, 2, 3))
    return Section(line, version, match.group(4), [])


def _key(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise ChangelogError(f"'{version}' is not a version like 1.2.3")
    major, minor, patch = (int(g) for g in match.groups())
    return major, minor, patch


def _content(section: Section) -> list[str]:
    lines = list(section.body)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--file", type=Path, default=DEFAULT_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    notes = sub.add_parser("notes")
    notes.add_argument("version")
    release = sub.add_parser("release")
    release.add_argument("version")
    release.add_argument("--date", default=datetime.date.today().isoformat())
    args = parser.parse_args(argv)
    try:
        changelog = Changelog.parse(args.file.read_text())
        changelog.check()
        if args.command == "notes":
            sys.stdout.write(changelog.notes(args.version))
        elif args.command == "release":
            changelog.release(args.version, args.date)
            changelog.check()
            args.file.write_text(changelog.render())
            print(f"{args.file.name}: Unreleased is now {args.version} ({args.date})")
    except ChangelogError as exc:
        print(f"changelog: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
