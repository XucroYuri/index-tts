from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from urllib.parse import quote


Runner = Callable[..., subprocess.CompletedProcess[str]]
SAFE_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def _validated_names(names: Sequence[str], *, label: str) -> list[str]:
    values = list(names)
    if len(values) != 6 or len(set(values)) != 6:
        raise ValueError(f"{label} must contain exactly six unique asset names")
    for name in values:
        if (
            not name
            or name != name.strip()
            or "/" in name
            or "\\" in name
            or "\r" in name
            or "\n" in name
        ):
            raise ValueError(f"{label} contains an unsafe asset name")
    return sorted(values)


def query_release_asset_names(
    repository: str,
    tag: str,
    *,
    run: Runner,
) -> list[str]:
    if SAFE_REPOSITORY.fullmatch(repository) is None:
        raise ValueError("GitHub repository identity is invalid")
    if not tag or tag != tag.strip() or "\r" in tag or "\n" in tag or "\0" in tag:
        raise ValueError("GitHub release tag is invalid")
    endpoint = f"repos/{repository}/releases/tags/{quote(tag, safe='')}"
    command = ["gh", "api", endpoint, "--jq", ".assets[].name"]
    completed = run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("GitHub release asset query failed")
    names = completed.stdout.splitlines()
    if any(not name for name in names):
        raise ValueError("GitHub release asset query returned an empty name")
    return names


def verify_published_release_assets(
    repository: str,
    tag: str,
    expected_names: Sequence[str],
    *,
    run: Runner,
) -> None:
    expected = _validated_names(expected_names, label="local release allowlist")
    actual = _validated_names(
        query_release_asset_names(repository, tag, run=run),
        label="published release asset set",
    )
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise ValueError(
            f"published release asset set mismatch: missing={missing}, extra={extra}"
        )


def main(argv: Sequence[str] | None = None, *, run: Runner | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify that a GitHub tag exposes exactly the local six portable assets."
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--expected-name", required=True, action="append")
    args = parser.parse_args(argv)
    try:
        verify_published_release_assets(
            args.repository,
            args.tag,
            args.expected_name,
            run=run or subprocess.run,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("published GitHub Release asset set matches the exact local six-asset allowlist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
