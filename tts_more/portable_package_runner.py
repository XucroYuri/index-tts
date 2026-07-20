from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import Mapping


SUPPORTED_COMPONENTS = {"gpt-sovits", "indextts", "cosyvoice"}


def _trusted_root(package_root: Path) -> Path:
    root = Path(os.path.abspath(package_root.expanduser()))
    if not root.is_dir():
        raise FileNotFoundError(f"portable package root does not exist: {root}")
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        if not current.is_dir():
            raise FileNotFoundError(f"portable package root or ancestor is missing: {current}")
        metadata = current.lstat()
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if current.is_symlink() or attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError("portable package root or ancestor is a reparse point")
    return root


def _assert_non_reparse_chain(root: Path, path: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        metadata = current.lstat()
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if current.is_symlink() or attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError("portable worker source_root traverses a reparse point")


def _mutable_cache_directory(root: Path) -> Path:
    current = root
    for part in ("data", "cache", "numba"):
        current /= part
        try:
            current.mkdir()
        except FileExistsError:
            pass
        metadata = current.lstat()
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if (
            not current.is_dir()
            or current.is_symlink()
            or attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        ):
            raise ValueError("portable mutable cache path must be a package-private directory")
    return current


def _worker_layout(root: Path) -> tuple[Path, Path, dict[str, object]]:
    candidates = (root / "app" / "tts_more", root / "tts_more")
    bundle = None
    for candidate in candidates:
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        _assert_non_reparse_chain(root, candidate)
        if (candidate / "component.json").is_file():
            bundle = candidate
            break
    if bundle is None:
        raise FileNotFoundError("portable worker component configuration is missing")
    config = json.loads((bundle / "component.json").read_text(encoding="utf-8-sig"))
    relative = str(config.get("source_root") or ".").replace("\\", "/")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ":" in relative or ".." in relative_path.parts:
        raise ValueError("portable worker source_root must be package-relative")
    source_root = (root / relative_path).resolve(strict=True)
    try:
        source_root.relative_to(root)
    except ValueError as exc:
        raise ValueError("portable worker source_root escapes package root") from exc
    _assert_non_reparse_chain(root, root / relative_path)
    if bundle.resolve(strict=True) != (source_root / "tts_more").resolve(strict=True):
        raise ValueError("portable worker bundle does not match source_root")
    return source_root, bundle, config


def build_worker_process(
    package_root: Path, environ: Mapping[str, str] | None = None
) -> tuple[list[str], Path, dict[str, str]]:
    root = _trusted_root(package_root)
    manifest = json.loads((root / "package" / "tts-more-package.json").read_text(encoding="utf-8-sig"))
    source_root, bundle_root, config = _worker_layout(root)
    component = str(manifest.get("component") or "")
    if component not in SUPPORTED_COMPONENTS or config.get("component") != component:
        raise ValueError("portable package component metadata is invalid")
    if manifest.get("api_contract") != "tts-more-v1":
        raise ValueError("portable package API contract is not tts-more-v1")

    runtime_python = root / "runtime" / "live" / "python.exe"
    if not runtime_python.is_file():
        raise FileNotFoundError("portable package runtime is missing; run Initialize.cmd first")
    module = str(config.get("module") or "")
    if not module.startswith("tts_more_worker.") or not module.endswith(":app"):
        raise ValueError("portable worker module is invalid")
    source_env = dict(os.environ if environ is None else environ)
    port = int(source_env.get("TTS_MORE_PORT") or config.get("port") or 0)
    if not 1 <= port <= 65535:
        raise ValueError("portable worker port is invalid")
    trusted_lan = source_env.get("TTS_MORE_TRUSTED_LAN") == "1"
    host = "0.0.0.0" if trusted_lan else "127.0.0.1"

    worker_env = {**source_env}
    worker_env["TTS_MORE_PACKAGE_ROOT"] = str(root)
    worker_env["TTS_MORE_ARTIFACT_ROOT"] = str(root / "data" / "local" / "artifacts")
    worker_env["PYTHONPATH"] = str(source_root)
    worker_env["PYTHONDONTWRITEBYTECODE"] = "1"
    worker_env["NUMBA_CACHE_DIR"] = str(_mutable_cache_directory(root))
    if trusted_lan:
        worker_env.pop("TTS_MORE_WORKER_ALLOW_PATH_DELIVERY", None)
    else:
        worker_env["TTS_MORE_WORKER_ALLOW_PATH_DELIVERY"] = "1"
    if component == "gpt-sovits":
        worker_env["TTS_MORE_GPTSOVITS_REPO"] = str(source_root)
    elif component == "indextts":
        worker_env["TTS_MORE_INDEXTTS_REPO"] = str(source_root)
        worker_env["TTS_MORE_INDEXTTS_PYTHON"] = str(runtime_python)
    else:
        worker_env["TTS_MORE_COSYVOICE_REPO"] = str(source_root)
        worker_env["TTS_MORE_COSYVOICE_MODEL_DIR"] = str(
            source_root / "pretrained_models" / "CosyVoice-300M"
        )

    command = [
        str(runtime_python),
        "-B",
        "-m",
        "uvicorn",
        module,
        "--app-dir",
        str(bundle_root),
        "--host",
        host,
        "--port",
        str(port),
    ]
    return command, source_root, worker_env


def port_is_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.4)
        return client.connect_ex((host, port)) == 0


def windows_port_owner(port: int) -> str:
    if os.name != "nt":
        return "owner lookup is only available on Windows"
    script = (
        f"$items=@(Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | "
        "ForEach-Object {$p=Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue; "
        "[pscustomobject]@{pid=$_.OwningProcess;name=if($p){$p.ProcessName}else{'unknown'};"
        "path=if($p){$p.Path}else{'unknown'}}}); ConvertTo-Json -InputObject $items -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result.stdout.strip() or "owner unavailable"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a validated sibling TTS More portable worker in the foreground")
    parser.add_argument("--package-root", required=True, type=Path)
    args = parser.parse_args(argv)
    command, cwd, environment = build_worker_process(args.package_root)
    port = int(command[-1])
    if port_is_listening(port):
        print(f"worker port {port} is already in use; owner: {windows_port_owner(port)}", file=sys.stderr)
        return 3
    process = subprocess.Popen(command, cwd=cwd, env=environment)
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            return process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
