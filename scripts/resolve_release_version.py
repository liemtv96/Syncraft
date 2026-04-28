#!/usr/bin/env python3
"""Normalize a Git tag to a PEP 440 version and optionally rewrite package metadata."""

from __future__ import annotations

import argparse
import pathlib
import re
import sys


VERSION_PATTERN = re.compile(
    r"^v?"
    r"(?P<base>\d+\.\d+\.\d+)"
    r"(?:[-_.]?"
    r"(?P<label>a|alpha|b|beta|rc|pre|preview)"
    r"(?:[-_.]?(?P<number>\d+))?"
    r")?$",
    re.IGNORECASE,
)

LABEL_MAP = {
    "a": "a",
    "alpha": "a",
    "b": "b",
    "beta": "b",
    "rc": "rc",
    "pre": "rc",
    "preview": "rc",
}


def normalize_tag(tag: str) -> str:
    match = VERSION_PATTERN.fullmatch(tag.strip())
    if not match:
        raise ValueError(
            f"Unsupported release tag '{tag}'. Use tags like v1.2.3, v1.2.3-beta, or v1.2.3-rc.1."
        )

    base = match.group("base")
    label = match.group("label")
    if not label:
        return base

    pep440_label = LABEL_MAP[label.lower()]
    number = match.group("number") or "0"
    return f"{base}{pep440_label}{number}"


def replace_once(path: pathlib.Path, pattern: str, replacement: str) -> None:
    original = path.read_text()
    updated, count = re.subn(pattern, replacement, original, count=1, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"Could not update version in {path}")
    path.write_text(updated)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag", help="Git tag, for example v1.2.3 or v1.2.3-beta.1")
    parser.add_argument(
        "--dev-suffix",
        type=int,
        help="Append a unique PEP 440 development release suffix such as .dev123.",
    )
    parser.add_argument(
        "--write-files",
        action="store_true",
        help="Rewrite version fields in tracked package metadata files.",
    )
    args = parser.parse_args()

    try:
        version = normalize_tag(args.tag)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.dev_suffix is not None:
        if args.dev_suffix < 0:
            print("--dev-suffix must be non-negative", file=sys.stderr)
            return 1
        version = f"{version}.dev{args.dev_suffix}"

    if args.write_files:
        root = pathlib.Path(__file__).resolve().parent.parent
        replace_once(root / "pyproject.toml", r'^version = "[^"]+"$', f'version = "{version}"')
        replace_once(root / "src/syncraft/__init__.py", r'^__version__ = "[^"]+"$', f'__version__ = "{version}"')

    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
