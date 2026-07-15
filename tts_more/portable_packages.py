from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import unicodedata
import zipfile
from pathlib import Path
from typing import Any


V1_REQUIRED_FIELDS = (
    "schema_version",
    "component",
    "version",
    "build_id",
    "api_contract",
    "default_endpoint",
    "port",
    "launcher",
    "health_path",
    "capabilities",
    "model_profile",
    "runtime",
    "sha256_manifest",
)

V2_REQUIRED_FIELDS = (
    "schema_version",
    "component",
    "version",
    "build_id",
    "package_profile",
    "platform",
    "api_contract",
    "source",
    "integration",
    "runtime",
    "models",
    "data_root",
    "launchers",
    "endpoint",
    "capabilities",
    "sha256_manifest",
    "licenses",
)
V2_REQUIRED_FIELDS = (*V2_REQUIRED_FIELDS, "package_id", "release_version", "protocol", "data")

V2_LAUNCHERS = ("initialize", "start", "stop", "repair", "build")
DEVICE_PROFILES = {"auto", "cu128", "cu126", "cpu"}
RELEASE_FORBIDDEN_PATH = re.compile(
    r"(^|/)(?:\.git|\.venv|__pycache__)(?:/|$)|"
    r"\.(?:safetensors|ckpt|pth|pt|t7|onnx|bin)$",
    re.IGNORECASE,
)
SAFE_PACKAGE_ROOT = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$")


def create_zip(package_root: Path, output: Path) -> None:
    """Create a deterministic-order ZIP64 archive with one package root."""
    package_root = package_root.resolve(strict=True)
    output = output.resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    with zipfile.ZipFile(
        temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
    ) as archive:
        for path in sorted(candidate for candidate in package_root.rglob("*") if candidate.is_file()):
            relative = path.relative_to(package_root).as_posix()
            archive.write(path, f"{package_root.name}/{relative}")
    temporary.replace(output)


def audit_release_zip(path: Path) -> dict[str, object]:
    """Fail closed unless a ZIP is a bootstrap package without local/full assets."""
    errors: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            raw_names = _raw_central_directory_names(archive, len(entries))
            parsed_entries: list[tuple[str, str]] = []
            entry_roots: set[str] = set()
            canonical_entry_roots: set[str] = set()
            for _entry, name in zip(entries, raw_names, strict=True):
                if "\\" in name:
                    raise ValueError("release ZIP top-level package directory must use forward slashes")
                canonical = _canonical_zip_entry(name)
                raw_parts = name.split("/")
                if len(raw_parts) < 2:
                    raise ValueError("release ZIP must contain files under one top-level package directory")
                raw_root = raw_parts[0]
                if (
                    not SAFE_PACKAGE_ROOT.fullmatch(raw_root)
                    or raw_root != raw_root.rstrip(" .")
                    or unicodedata.normalize("NFKC", raw_root) != raw_root
                ):
                    raise ValueError("release ZIP top-level package directory name is unsafe or ambiguous")
                entry_roots.add(raw_root)
                canonical_entry_roots.add(canonical.split("/", 1)[0])
                parsed_entries.append((canonical, name))
            if len(entry_roots) != 1 or len(canonical_entry_roots) != 1:
                raise ValueError("release ZIP must contain exactly one top-level package directory")
            canonical_names: dict[str, str] = {}
            for canonical, name in parsed_entries:
                if canonical in canonical_names:
                    errors.append(f"unsafe ZIP entry collision: {name}")
                    break
                canonical_names[canonical] = name
            if errors:
                return {"valid": False, "errors": errors, "path": str(path)}
            package_root = next(iter(canonical_entry_roots))
            relative_names = {
                canonical.split("/", 1)[1]: original
                for canonical, original in canonical_names.items()
                if "/" in canonical and canonical.split("/", 1)[0] == package_root
            }
            manifests = [
                original
                for relative, original in relative_names.items()
                if relative == "package/tts-more-package.json"
            ]
            if len(manifests) != 1:
                errors.append("release ZIP must contain exactly one package manifest")
            else:
                payload = json.loads(archive.read(manifests[0]).decode("utf-8-sig"))
                if payload.get("package_profile") != "bootstrap":
                    errors.append(f"GitHub release upload refused for profile={payload.get('package_profile')}")
            for relative, name in relative_names.items():
                parts = tuple(part for part in relative.split("/") if part)
                private_data = len(parts) >= 2 and parts[0] == "data" and parts[1] in {
                    "user",
                    "local",
                    "cache",
                    "models",
                }
                if (parts and parts[0] == "runtime") or private_data or RELEASE_FORBIDDEN_PATH.search(relative):
                    errors.append(f"forbidden release asset: {relative}")
                    break
    except (OSError, ValueError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"invalid release ZIP: {exc}")
    return {"valid": not errors, "errors": errors, "path": str(path)}


def verify_sha256_manifest(package_root: Path) -> dict[str, object]:
    """Verify exact SHA256SUMS coverage and every digest for an extracted package."""
    errors: list[str] = []
    try:
        root = package_root.resolve(strict=True)
        sums_path = root / "SHA256SUMS.txt"
        if not sums_path.is_file() or sums_path.is_symlink():
            raise ValueError("SHA256SUMS.txt is missing or unsafe")
        covered: dict[str, tuple[str, str]] = {}
        for line in sums_path.read_text(encoding="utf-8-sig").splitlines():
            match = re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", line)
            if match is None or not _is_relative_package_path(match.group(2)):
                raise ValueError("SHA256SUMS contains an invalid record")
            relative = unicodedata.normalize("NFKC", match.group(2)).replace("\\", "/")
            canonical = _canonical_relative_path(relative)
            if canonical in covered:
                raise ValueError(f"SHA256SUMS contains a duplicate path: {relative}")
            covered[canonical] = (relative, match.group(1).casefold())

        files: dict[str, Path] = {}
        for candidate in root.rglob("*"):
            if candidate == sums_path:
                continue
            if candidate.is_symlink():
                raise ValueError(f"package contains an unsafe link: {candidate.relative_to(root)}")
            if not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=True)
            try:
                relative = resolved.relative_to(root).as_posix()
            except ValueError as exc:
                raise ValueError("package file escapes package root") from exc
            canonical = _canonical_relative_path(relative)
            if canonical in files:
                raise ValueError(f"package contains a normalized path collision: {relative}")
            files[canonical] = candidate

        if set(covered) != set(files):
            missing = sorted(set(files) - set(covered))
            extra = sorted(set(covered) - set(files))
            errors.append(f"SHA256SUMS exact coverage mismatch: missing={missing}, extra={extra}")
        for canonical in sorted(set(covered) & set(files)):
            relative, expected = covered[canonical]
            actual = hashlib.sha256(files[canonical].read_bytes()).hexdigest()
            if actual != expected:
                errors.append(f"SHA256SUMS hash mismatch: {relative}")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        errors.append(str(exc))
    return {"valid": not errors, "errors": errors, "package_root": str(package_root)}


def _canonical_zip_entry(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name).replace("\\", "/")
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"unsafe ZIP entry: {name}")
    parts: list[str] = []
    for raw_part in normalized.split("/"):
        part = raw_part.rstrip(" .").casefold()
        if not part or part == ".":
            continue
        if part == ".." or ":" in part or "\x00" in part:
            raise ValueError(f"unsafe ZIP entry: {name}")
        parts.append(part)
    if not parts:
        raise ValueError(f"unsafe ZIP entry: {name}")
    return "/".join(parts)


def _raw_central_directory_names(archive: zipfile.ZipFile, count: int) -> list[str]:
    """Read names before zipfile applies platform separator normalization."""
    if archive.fp is None:
        raise ValueError("release ZIP is closed")
    stream = archive.fp
    original_position = stream.tell()
    names: list[str] = []
    try:
        stream.seek(archive.start_dir)
        for _ in range(count):
            header = stream.read(46)
            if len(header) != 46 or header[:4] != b"PK\x01\x02":
                raise ValueError("invalid ZIP central directory")
            flags = int.from_bytes(header[8:10], "little")
            name_length, extra_length, comment_length = struct.unpack_from("<HHH", header, 28)
            raw_name = stream.read(name_length)
            if len(raw_name) != name_length:
                raise ValueError("truncated ZIP member name")
            encoding = "utf-8" if flags & 0x800 else "cp437"
            names.append(raw_name.decode(encoding, errors="strict"))
            stream.seek(extra_length + comment_length, 1)
    finally:
        stream.seek(original_position)
    return names


def _canonical_relative_path(value: str) -> str:
    canonical = _canonical_zip_entry(f"package/{value}")
    return canonical.split("/", 1)[1]


def validate_manifest(manifest_path: Path, package_root: Path) -> dict[str, object]:
    """Validate a portable component manifest without depending on its host path."""
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    schema_version = payload.get("schema_version")
    if schema_version == 1:
        errors, launcher, default_endpoint = _validate_v1(payload, package_root)
    elif schema_version == 2:
        errors, launcher, default_endpoint = _validate_v2(payload, package_root)
    else:
        errors = ["schema_version must be 1 or 2"]
        launcher = ""
        default_endpoint = ""
    return {
        "valid": not errors,
        "errors": errors,
        "component": payload.get("component", ""),
        "default_endpoint": default_endpoint,
        "launcher": launcher,
    }


def _validate_v1(payload: dict[str, Any], package_root: Path) -> tuple[list[str], str, str]:
    errors = [f"{field} is required" for field in V1_REQUIRED_FIELDS if not payload.get(field)]
    launcher = str(payload.get("launcher") or "")
    if launcher and not _is_relative_package_path(launcher):
        errors.append("launcher must be a relative path")
    elif launcher and not (package_root / launcher).is_file():
        errors.append("launcher does not exist in package")
    if payload.get("api_contract") and payload.get("api_contract") != "tts-more-v1":
        errors.append("api_contract must be tts-more-v1")
    if payload.get("health_path") and not str(payload["health_path"]).startswith("/"):
        errors.append("health_path must start with /")
    return errors, launcher, str(payload.get("default_endpoint") or "")


def _validate_v2(payload: dict[str, Any], package_root: Path) -> tuple[list[str], str, str]:
    identity_fields = {"package_id", "release_version"}
    errors = [
        f"{field} is required"
        for field in V2_REQUIRED_FIELDS
        if field not in identity_fields and payload.get(field) in (None, "", [], {})
    ]
    for field in ("package_id", "release_version"):
        _require_text(payload, field, field, errors)
    profile = str(payload.get("package_profile") or "")
    if profile not in {"bootstrap", "full"}:
        errors.append("package_profile must be bootstrap or full")
    if payload.get("platform") != "windows-x64":
        errors.append("platform must be windows-x64")
    if payload.get("api_contract") != "tts-more-v1":
        errors.append("api_contract must be tts-more-v1")
    _validate_v2_data(payload, errors)

    source = _mapping(payload.get("source"))
    _require_text(source, "repository", "source.repository", errors)
    _validate_revision(source.get("revision"), "source.revision", errors)

    integration = _mapping(payload.get("integration"))
    _require_text(integration, "version", "integration.version", errors)
    _validate_revision(integration.get("source_revision"), "integration.source_revision", errors)
    _validate_sha256(integration.get("bundle_sha256"), "integration.bundle_sha256", errors)

    runtime = _mapping(payload.get("runtime"))
    _require_text(runtime, "python_version", "runtime.python_version", errors)
    profiles = runtime.get("device_profiles")
    if not isinstance(profiles, list) or not profiles:
        errors.append("runtime.device_profiles is required")
    elif any(str(item).lower() not in DEVICE_PROFILES for item in profiles):
        errors.append("runtime.device_profiles contains an unsupported profile")
    _validate_package_file(runtime.get("lock"), "runtime.lock", package_root, errors)
    _validate_relative_path(runtime.get("state_path"), "runtime.state_path", errors)

    models = _mapping(payload.get("models"))
    _validate_package_file(models.get("lock"), "models.lock", package_root, errors)
    if not isinstance(models.get("required"), bool):
        errors.append("models.required must be a boolean")

    _validate_relative_path(payload.get("data_root"), "data_root", errors)
    launchers = _mapping(payload.get("launchers"))
    for name in V2_LAUNCHERS:
        _validate_package_file(launchers.get(name), f"launchers.{name}", package_root, errors)
    launcher = str(launchers.get("start") or "")

    endpoint = _mapping(payload.get("endpoint"))
    default_endpoint = str(endpoint.get("default_url") or "")
    if not default_endpoint.startswith("http://"):
        errors.append("endpoint.default_url must start with http://")
    port = endpoint.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        errors.append("endpoint.port must be between 1 and 65535")
    for name in ("health_path", "capabilities_path"):
        value = str(endpoint.get(name) or "")
        if not value.startswith("/"):
            errors.append(f"endpoint.{name} must start with /")
    if endpoint.get("bind_policy") not in {"loopback", "trusted-lan"}:
        errors.append("endpoint.bind_policy must be loopback or trusted-lan")

    _validate_relative_path(payload.get("sha256_manifest"), "sha256_manifest", errors)
    _validate_package_file(payload.get("licenses"), "licenses", package_root, errors)
    return errors, launcher, default_endpoint


def _validate_v2_data(payload: dict[str, Any], errors: list[str]) -> None:
    protocol = _mapping(payload.get("protocol"))
    if protocol.get("name") != "tts-more-v1":
        errors.append("protocol.name must be tts-more-v1")
    for key in ("version", "controller_range"):
        _require_text(protocol, key, f"protocol.{key}", errors)
    data = _mapping(payload.get("data"))
    for key in ("user", "local", "cache", "operations"):
        _validate_relative_path(data.get(key), f"data.{key}", errors)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _require_text(payload: dict[str, Any], key: str, label: str, errors: list[str]) -> None:
    if not isinstance(payload.get(key), str) or not str(payload[key]).strip():
        errors.append(f"{label} is required")


def _validate_revision(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{40,64}", value) is None:
        errors.append(f"{label} must be an immutable hexadecimal revision")


def _validate_sha256(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        errors.append(f"{label} must be a SHA-256 digest")


def _validate_relative_path(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value or not _is_relative_package_path(value):
        errors.append(f"{label} must be a relative path")


def _validate_package_file(value: Any, label: str, package_root: Path, errors: list[str]) -> None:
    if not isinstance(value, str) or not value or not _is_relative_package_path(value):
        errors.append(f"{label} must be a relative path")
    elif not (package_root / value).is_file():
        errors.append(f"{label} does not exist in package")


def _is_relative_package_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    path = Path(normalized)
    return not path.is_absolute() and ":" not in normalized and ".." not in normalized.split("/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate TTS More portable component packages")
    subcommands = parser.add_subparsers(dest="command", required=True)
    validate = subcommands.add_parser("validate-manifest")
    validate.add_argument("--manifest", required=True, type=Path)
    validate.add_argument("--package-root", required=True, type=Path)
    create = subcommands.add_parser("create-zip")
    create.add_argument("--package-root", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    audit = subcommands.add_parser("audit-release")
    audit.add_argument("--zip", required=True, action="append", type=Path)
    verify_sums = subcommands.add_parser("verify-sha256")
    verify_sums.add_argument("--package-root", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "validate-manifest":
        report = validate_manifest(args.manifest, args.package_root)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["valid"] else 1
    if args.command == "create-zip":
        create_zip(args.package_root, args.output)
        return 0
    if args.command == "audit-release":
        reports = [audit_release_zip(path) for path in args.zip]
        print(json.dumps({"valid": all(report["valid"] for report in reports), "reports": reports}, ensure_ascii=False, sort_keys=True))
        return 0 if all(report["valid"] for report in reports) else 1
    if args.command == "verify-sha256":
        report = verify_sha256_manifest(args.package_root)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["valid"] else 1
    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
