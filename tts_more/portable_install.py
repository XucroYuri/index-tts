from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable


Downloader = Callable[[str, Path, int], None]


def load_json(path: Path) -> Any:
    """Read JSON emitted by either Python or Windows PowerShell 5.1."""
    return json.loads(path.read_text(encoding="utf-8-sig"))


def nvidia_marketing_driver(windows_driver_version: str) -> tuple[int, int]:
    """Convert a Windows WDDM version such as 32.0.15.7652 to 576.52."""
    try:
        parts = [int(part) for part in windows_driver_version.split(".")]
        if len(parts) < 2:
            raise ValueError
        vendor = parts[-2] % 10
        build = parts[-1]
        return int(f"{vendor}{build // 100:02d}"), build % 100
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Windows NVIDIA driver version: {windows_driver_version}") from exc


def select_device_profile(
    runtime_lock: dict[str, Any], requested: str, controllers: Iterable[dict[str, Any]]
) -> str:
    profiles = {str(name).lower(): value for name, value in _mapping(runtime_lock.get("profiles")).items()}
    requested = requested.lower()
    if requested != "auto" and requested not in profiles:
        raise RuntimeError(f"unsupported device profile: {requested}")

    nvidia_versions = []
    for controller in controllers:
        if "nvidia" not in str(controller.get("name") or "").lower():
            continue
        try:
            nvidia_versions.append(nvidia_marketing_driver(str(controller.get("driver_version") or "")))
        except ValueError:
            continue
    installed_driver = max(nvidia_versions, default=None)

    candidates = (
        [str(item).lower() for item in runtime_lock.get("auto_order", [])]
        if requested == "auto"
        else [requested]
    )
    for candidate in candidates:
        profile = _mapping(profiles.get(candidate))
        if candidate == "cpu":
            return candidate
        required_text = str(profile.get("minimum_nvidia_driver") or "0")
        required = _parse_driver_requirement(required_text)
        if installed_driver is not None and installed_driver >= required:
            return candidate
        if requested != "auto":
            found = "not detected" if installed_driver is None else f"{installed_driver[0]}.{installed_driver[1]:02d}"
            raise RuntimeError(
                f"{candidate} requires NVIDIA driver {required_text} or newer; detected {found}"
            )
    raise RuntimeError("no compatible device profile is available")


def ensure_locked_asset(
    asset: dict[str, Any], destination: Path, *, downloader: Downloader | None = None
) -> dict[str, object]:
    expected_hash = str(asset.get("sha256") or "").lower()
    expected_size = int(asset.get("size_bytes") or 0)
    urls = [str(url) for url in asset.get("urls", []) if str(url)]
    if len(expected_hash) != 64 or expected_size <= 0 or not urls:
        raise ValueError(f"asset lock is incomplete: {asset.get('id') or destination.name}")
    destination = destination.resolve(strict=False)
    if _asset_matches(destination, expected_hash, expected_size):
        return {"path": str(destination), "reused": True, "source": ""}

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists() and partial.stat().st_size > expected_size:
        partial.unlink()
    download = downloader or _download_http
    failures: list[str] = []
    for url in urls:
        resume_from = partial.stat().st_size if partial.exists() else 0
        try:
            download(url, partial, resume_from)
        except Exception as exc:  # URL fallback is part of the package contract.
            failures.append(f"{url}: {exc}")
            continue
        if not _asset_matches(partial, expected_hash, expected_size):
            failures.append(f"{url}: failed SHA-256 verification")
            continue
        os.replace(partial, destination)
        return {"path": str(destination), "reused": False, "source": url}
    detail = "; ".join(failures) or "no source succeeded"
    if "failed SHA-256 verification" in detail:
        raise RuntimeError(f"asset failed SHA-256 verification: {asset.get('id') or destination.name}")
    raise RuntimeError(f"asset download failed: {asset.get('id') or destination.name}: {detail}")


def write_install_state(
    state_path: Path,
    *,
    component: str,
    build_id: str,
    profile: str,
    runtime_lock_sha256: str,
    model_lock_sha256: str,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "component": component,
        "build_id": build_id,
        "profile": profile,
        "runtime_lock_sha256": runtime_lock_sha256,
        "model_lock_sha256": model_lock_sha256,
        "ready": True,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, state_path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_matches(path: Path, expected_hash: str, expected_size: int) -> bool:
    return path.is_file() and path.stat().st_size == expected_size and sha256_file(path) == expected_hash


def _download_http(url: str, destination: Path, resume_from: int) -> None:
    request = urllib.request.Request(url)
    if resume_from:
        request.add_header("Range", f"bytes={resume_from}-")
    with urllib.request.urlopen(request, timeout=120) as response:
        append = resume_from > 0 and getattr(response, "status", None) == 206
        with destination.open("ab" if append else "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_driver_requirement(value: str) -> tuple[int, int]:
    try:
        major, minor = value.split(".", 1)
        return int(major), int(minor)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid minimum NVIDIA driver: {value}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TTS More portable initialization helpers")
    subcommands = parser.add_subparsers(dest="command", required=True)
    verify = subcommands.add_parser("verify-asset")
    verify.add_argument("--asset", required=True, type=Path)
    verify.add_argument("--path", required=True, type=Path)
    ensure = subcommands.add_parser("ensure-asset")
    ensure.add_argument("--asset", required=True, type=Path)
    ensure.add_argument("--path", required=True, type=Path)
    select = subcommands.add_parser("select-device")
    select.add_argument("--runtime-lock", required=True, type=Path)
    select.add_argument("--requested", default="auto")
    select.add_argument("--controllers", required=True, type=Path)
    state = subcommands.add_parser("write-state")
    state.add_argument("--path", required=True, type=Path)
    state.add_argument("--component", required=True)
    state.add_argument("--build-id", required=True)
    state.add_argument("--profile", required=True)
    state.add_argument("--runtime-lock-sha256", required=True)
    state.add_argument("--model-lock-sha256", required=True)
    args = parser.parse_args(argv)
    if args.command == "verify-asset":
        asset = load_json(args.asset)
        valid = _asset_matches(args.path, str(asset["sha256"]).lower(), int(asset["size_bytes"]))
        print(json.dumps({"valid": valid, "path": str(args.path)}, sort_keys=True))
        return 0 if valid else 1
    if args.command == "ensure-asset":
        asset = load_json(args.asset)
        print(json.dumps(ensure_locked_asset(asset, args.path), ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "select-device":
        runtime_lock = load_json(args.runtime_lock)
        controllers = load_json(args.controllers)
        print(select_device_profile(runtime_lock, args.requested, controllers))
        return 0
    if args.command == "write-state":
        write_install_state(
            args.path,
            component=args.component,
            build_id=args.build_id,
            profile=args.profile,
            runtime_lock_sha256=args.runtime_lock_sha256,
            model_lock_sha256=args.model_lock_sha256,
        )
        return 0
    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
