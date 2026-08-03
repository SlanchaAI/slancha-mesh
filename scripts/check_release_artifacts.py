#!/usr/bin/env python3
"""Fail closed when built release archives contain local-only source files."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
import sys
import tarfile
import zipfile


FORBIDDEN_COMPONENTS = frozenset(
    {
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        ".venv-router",
        "__pycache__",
        "corpus",
        "env",
        "packages",
        "tests",
        "venv",
    }
)
REQUIRED_LEGAL_FILES = frozenset({"LICENSE", "NOTICE"})


def archive_paths(inputs: Iterable[Path]) -> list[Path]:
    """Return supported artifacts, requiring each supplied directory to be complete."""
    archives: list[Path] = []
    for input_path in inputs:
        if input_path.is_dir():
            found = sorted(
                path
                for path in input_path.iterdir()
                if path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
            )
            kinds = {"wheel" if path.suffix == ".whl" else "sdist" for path in found}
            if kinds != {"wheel", "sdist"}:
                raise ValueError(f"{input_path}: expected both a wheel and source distribution")
            archives.extend(found)
        elif input_path.is_file() and (
            input_path.suffix == ".whl" or input_path.name.endswith(".tar.gz")
        ):
            archives.append(input_path)
        else:
            raise ValueError(f"{input_path}: not a supported release artifact")
    if not archives:
        raise ValueError("no release artifacts supplied")
    return archives


def archive_members(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    with tarfile.open(path, "r:gz") as archive:
        return archive.getnames()


def forbidden_reason(member: str) -> str | None:
    path = PurePosixPath(member)
    if path.is_absolute() or ".." in path.parts:
        return "unsafe archive path"
    if any(part in FORBIDDEN_COMPONENTS for part in path.parts):
        return "forbidden local-only path"
    if any(part.startswith(".venv-") for part in path.parts):
        return "forbidden local virtual environment"
    if path.name.startswith("SESSION_LOG") and path.suffix == ".md":
        return "forbidden session log"
    if path.suffix in {".pyc", ".pyo"}:
        return "forbidden Python cache bytecode"
    return None


def validate_archive(path: Path) -> list[str]:
    members = archive_members(path)
    failures = [
        f"{path}: {reason}: {member}"
        for member in members
        if (reason := forbidden_reason(member)) is not None
    ]
    member_names = {PurePosixPath(member).name for member in members}
    for legal_file in sorted(REQUIRED_LEGAL_FILES - member_names):
        failures.append(f"{path}: missing required legal file: {legal_file}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        archives = archive_paths(args.artifacts)
    except ValueError as error:
        parser.error(str(error))

    failures = [failure for archive in archives for failure in validate_archive(archive)]
    if failures:
        print("Release artifact check failed:", file=sys.stderr)
        print("\n".join(f"- {failure}" for failure in failures), file=sys.stderr)
        return 1
    print(f"Release artifact check passed: {len(archives)} archives")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
