from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as importlib_metadata
import importlib.util
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable
from uuid import UUID


class PortableInstallCancelled(RuntimeError):
    pass


ProgressCallback = Callable[[int, int, str], None]
CancelCheck = Callable[[], bool]
Downloader = Callable[[str, Path, int, ProgressCallback | None, CancelCheck | None], None]
CONTENT_RANGE_PATTERN = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
ENTRY_POINT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DOWNLOAD_ATTEMPTS_PER_URL = 3
DOWNLOAD_RETRY_DELAYS = (1.0, 2.0)


def load_json(path: Path) -> Any:
    """Read JSON emitted by either Python or Windows PowerShell 5.1."""
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _is_reparse_point(path: Path) -> bool:
    file_attributes = getattr(path.lstat(), "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(file_attributes & reparse_attribute)


def prune_console_launchers(site_packages: Path) -> dict[str, object]:
    site_packages = Path(site_packages)
    if _is_reparse_point(site_packages):
        raise ValueError("site-packages must be a real directory")
    site_packages = site_packages.resolve(strict=True)
    if not site_packages.is_dir():
        raise ValueError("site-packages must be a real directory")

    entry_point_names: set[str] = set()
    casefolded_names: dict[str, str] = {}
    for distribution in importlib_metadata.distributions(path=[str(site_packages)]):
        for entry_point in distribution.entry_points:
            if entry_point.group not in {"console_scripts", "gui_scripts"}:
                continue
            name = entry_point.name
            if not ENTRY_POINT_NAME_PATTERN.fullmatch(name):
                raise ValueError(f"unsafe console entry-point name: {name!r}")
            folded = name.casefold()
            previous = casefolded_names.get(folded)
            if previous is not None and previous != name:
                raise ValueError(f"ambiguous console entry-point names: {previous!r}, {name!r}")
            casefolded_names[folded] = name
            entry_point_names.add(name)

    launcher_root = site_packages / "bin"
    if not os.path.lexists(launcher_root):
        return {"preserved_unknown": [], "removed": []}
    if not launcher_root.is_dir() or _is_reparse_point(launcher_root):
        raise ValueError("console launcher root must be a real directory")

    candidates = [launcher_root / f"{name}.exe" for name in sorted(entry_point_names)]
    existing_candidates = [candidate for candidate in candidates if os.path.lexists(candidate)]
    for candidate in existing_candidates:
        if _is_reparse_point(candidate):
            raise ValueError(f"reparse-point console launcher is not removable: {candidate.name}")
        metadata = candidate.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"console launcher is not a regular file: {candidate.name}")
        if metadata.st_nlink != 1:
            raise ValueError(f"hardlinked console launcher is not removable: {candidate.name}")

    candidate_names = {candidate.name.casefold() for candidate in candidates}
    preserved_unknown = sorted(
        f"bin/{entry.name}"
        for entry in launcher_root.iterdir()
        if entry.name.casefold() not in candidate_names
    )
    removed: list[str] = []
    for candidate in existing_candidates:
        candidate.unlink()
        removed.append(f"bin/{candidate.name}")
    return {"preserved_unknown": preserved_unknown, "removed": sorted(removed)}


def resolve_operations_root(package_root: Path) -> Path:
    root = package_root.resolve(strict=False)
    relative = "data/local/operations"
    manifest_path = root / "package" / "tts-more-package.json"
    if manifest_path.is_file():
        manifest = load_json(manifest_path)
        if manifest.get("schema_version") == 2:
            relative = str((manifest.get("data") or {}).get("operations") or "")
            candidate = Path(relative.replace("\\", "/"))
            if not relative or candidate.is_absolute() or ":" in relative or ".." in candidate.parts:
                raise ValueError("manifest data.operations must be a package-relative path")
    operations_root = (root / Path(relative.replace("\\", "/"))).resolve(strict=False)
    try:
        operations_root.relative_to(root)
    except ValueError as error:
        raise ValueError("manifest data.operations resolves outside the package") from error
    return operations_root


def validate_operation_paths(
    package_root: Path,
    operation_root: Path | None,
    cancel_file: Path | None,
) -> tuple[Path | None, Path | None]:
    if (operation_root is None) != (cancel_file is None):
        raise ValueError("operation-root and cancel-file must be provided together")
    if operation_root is None or cancel_file is None:
        return None, None

    package_root = package_root.resolve(strict=False)
    operations_root = resolve_operations_root(package_root)
    operation_root = operation_root.resolve(strict=False)
    if operation_root.parent != operations_root:
        raise ValueError("operation-root must be a UUID-named direct child of the package operations root")
    try:
        UUID(operation_root.name)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("operation-root name must be a valid UUID") from error

    cancel_file = cancel_file.resolve(strict=False)
    expected_cancel = (operation_root / "cancel.requested").resolve(strict=False)
    if cancel_file != expected_cancel:
        raise ValueError("cancel-file must resolve exactly to operation-root/cancel.requested")
    return operation_root, cancel_file


def _default_package_root() -> Path:
    return Path(__file__).resolve().parents[1]


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
    asset: dict[str, Any],
    destination: Path,
    *,
    downloader: Downloader | None = None,
    progress: ProgressCallback | None = None,
    cancelled: CancelCheck | None = None,
) -> dict[str, object]:
    expected_hash = str(asset.get("sha256") or "").lower()
    expected_size = int(asset.get("size_bytes") or 0)
    urls = [str(url) for url in asset.get("urls", []) if str(url)]
    if len(expected_hash) != 64 or expected_size <= 0 or not urls:
        raise ValueError(f"asset lock is incomplete: {asset.get('id') or destination.name}")
    destination = destination.resolve(strict=False)
    if cancelled and cancelled():
        raise PortableInstallCancelled("portable installation cancelled")
    if _asset_matches(destination, expected_hash, expected_size):
        return {"path": str(destination), "reused": True, "source": ""}

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists():
        partial_size = partial.stat().st_size
        if partial_size == expected_size:
            if _asset_matches(partial, expected_hash, expected_size):
                os.replace(partial, destination)
                return {"path": str(destination), "reused": False, "source": ""}
            partial.unlink()
        elif partial_size > expected_size:
            partial.unlink()
    download = downloader or _download_http
    failures: list[str] = []
    for url in urls:
        for attempt in range(DOWNLOAD_ATTEMPTS_PER_URL):
            if cancelled and cancelled():
                raise PortableInstallCancelled("portable installation cancelled")
            resume_from = partial.stat().st_size if partial.exists() else 0
            try:
                download(url, partial, resume_from, progress, cancelled)
            except PortableInstallCancelled:
                raise
            except Exception as exc:  # Retry this immutable source before mirror fallback.
                if attempt < DOWNLOAD_ATTEMPTS_PER_URL - 1:
                    time.sleep(DOWNLOAD_RETRY_DELAYS[attempt])
                    continue
                failures.append(f"{url}: {exc}")
                break
            if cancelled and cancelled():
                raise PortableInstallCancelled("portable installation cancelled")
            if not _asset_matches(partial, expected_hash, expected_size):
                failures.append(f"{url}: failed SHA-256 verification")
                if partial.is_file() and partial.stat().st_size >= expected_size:
                    partial.unlink()
                break
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
        "completed_at": datetime.now(timezone.utc).isoformat(),
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


def _download_http(
    url: str,
    destination: Path,
    resume_from: int,
    progress: ProgressCallback | None,
    cancelled: CancelCheck | None,
) -> None:
    plan: tuple[bool, int, int] | None
    with _open_http_response(url, resume_from) as response:
        plan = _response_download_plan(response, resume_from=resume_from)
        if plan is not None:
            _write_http_response(response, destination, plan, url, progress, cancelled)
            return

    if cancelled and cancelled():
        raise PortableInstallCancelled("portable installation cancelled")
    with _open_http_response(url, 0) as response:
        plan = _response_download_plan(response, resume_from=0)
        if plan is None:
            raise RuntimeError("clean HTTP download did not provide a complete zero-based response")
        _write_http_response(response, destination, plan, url, progress, cancelled)


def _open_http_response(url: str, resume_from: int) -> Any:
    request = _http_request(url, resume_from)
    try:
        return urllib.request.urlopen(request, timeout=120)
    except urllib.error.HTTPError as error:
        if resume_from > 0 and error.code == 416:
            return error
        raise


def _http_request(url: str, resume_from: int) -> urllib.request.Request:
    request = urllib.request.Request(url)
    parsed = urllib.parse.urlsplit(url)
    if (
        str(parsed.hostname or "").lower() == "api.github.com"
        and re.fullmatch(r"/repos/[^/]+/[^/]+/releases/assets/\d+", parsed.path)
    ):
        request.add_header("Accept", "application/octet-stream")
        request.add_header("User-Agent", "tts-more-portable-installer")
    if resume_from:
        request.add_header("Range", f"bytes={resume_from}-")
    return request


def _write_http_response(
    response: Any,
    destination: Path,
    plan: tuple[bool, int, int],
    url: str,
    progress: ProgressCallback | None,
    cancelled: CancelCheck | None,
) -> None:
    append, start, total = plan
    with destination.open("ab" if append else "wb") as output:
        _copy_response(
            response,
            output,
            start=start,
            total=total,
            url=url,
            progress=progress,
            cancelled=cancelled,
        )


def _response_download_plan(response: Any, *, resume_from: int) -> tuple[bool, int, int] | None:
    status = int(getattr(response, "status", None) or getattr(response, "code", None) or 200)
    if status == 200:
        return False, 0, _response_total(response, start=0)
    if status == 206:
        content_range = _parse_content_range(response)
        if content_range is not None and content_range[0] == resume_from:
            return resume_from > 0, content_range[0], content_range[2]
        if resume_from > 0:
            return None
        raise RuntimeError("HTTP 206 response has an invalid zero-based Content-Range")
    if status == 416 and resume_from > 0:
        return None
    raise RuntimeError(f"unexpected HTTP download status: {status}")


def _parse_content_range(response: Any) -> tuple[int, int, int] | None:
    value = response.headers.get("Content-Range")
    match = CONTENT_RANGE_PATTERN.fullmatch(str(value or ""))
    if match is None:
        return None
    start, end, total = (int(item) for item in match.groups())
    if end < start or total <= end:
        return None
    content_length = response.headers.get("Content-Length")
    try:
        if content_length is not None and int(content_length) != end - start + 1:
            return None
    except (TypeError, ValueError):
        return None
    return start, end, total


def _copy_response(
    response: Any,
    output: BinaryIO,
    *,
    start: int,
    total: int,
    url: str,
    progress: ProgressCallback | None,
    cancelled: CancelCheck | None,
) -> None:
    written = start
    while chunk := response.read(1024 * 1024):
        if cancelled and cancelled():
            raise PortableInstallCancelled("portable installation cancelled")
        output.write(chunk)
        written += len(chunk)
        if progress:
            progress(written, total, url)
    if total > 0 and written != total:
        raise RuntimeError(f"incomplete HTTP download: received {written} of {total} bytes")


def _response_total(response: Any, *, start: int) -> int:
    content_range = response.headers.get("Content-Range")
    if content_range and "/" in content_range:
        try:
            return int(content_range.rsplit("/", 1)[1])
        except ValueError:
            pass
    content_length = response.headers.get("Content-Length")
    try:
        return start + int(content_length) if content_length is not None else 0
    except ValueError:
        return 0


def _operation_progress(operation_root: Path, asset_id: str) -> ProgressCallback:
    operation_root = operation_root.resolve(strict=False)
    last_update = float("-inf")

    def report(downloaded: int, total: int, _url: str) -> None:
        nonlocal last_update
        now = time.monotonic()
        complete = total > 0 and downloaded >= total
        if not complete and now - last_update < 0.25:
            return
        operations_path = Path(__file__).resolve().with_name("portable_operations.py")
        spec = importlib.util.spec_from_file_location("_tts_more_bundle_portable_operations", operations_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"unable to load bundled operation module: {operations_path}")
        operations = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(operations)
        append_event = operations.append_event

        percent = downloaded * 100.0 / total if total > 0 else None
        append_event(
            operation_root.parent,
            operation_root.name,
            "downloading",
            f"Downloading {asset_id}",
            percent=percent,
        )
        last_update = now

    return report


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
    ensure.add_argument("--package-root", type=Path, default=_default_package_root())
    ensure.add_argument("--operation-root", type=Path)
    ensure.add_argument("--cancel-file", type=Path)
    select = subcommands.add_parser("select-device")
    select.add_argument("--runtime-lock", required=True, type=Path)
    select.add_argument("--requested", default="auto")
    select.add_argument("--controllers", required=True, type=Path)
    prune = subcommands.add_parser("prune-console-launchers")
    prune.add_argument("--site-packages", required=True, type=Path)
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
        operation_root, cancel_file = validate_operation_paths(
            args.package_root,
            args.operation_root,
            args.cancel_file,
        )
        asset = load_json(args.asset)
        progress = (
            _operation_progress(operation_root, str(asset.get("id") or args.path.name))
            if operation_root is not None
            else None
        )
        try:
            report = ensure_locked_asset(
                asset,
                args.path,
                progress=progress,
                cancelled=(lambda: cancel_file.is_file()) if cancel_file is not None else None,
            )
        except PortableInstallCancelled:
            return 20
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "select-device":
        runtime_lock = load_json(args.runtime_lock)
        controllers = load_json(args.controllers)
        print(select_device_profile(runtime_lock, args.requested, controllers))
        return 0
    if args.command == "prune-console-launchers":
        print(json.dumps(prune_console_launchers(args.site_packages), sort_keys=True))
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
