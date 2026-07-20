from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import time
import unicodedata
import uuid
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
    r"(^|/)(?:\.git|\.venv|__pycache__|artifacts?|caches?|\.cache)(?:/|$)|"
    r"(^|/)\.env(?:\.[^/]+)?$|"
    r"\.(?:safetensors|ckpt|pth|pt|t7|onnx|bin)$",
    re.IGNORECASE,
)
RELEASE_FORBIDDEN_MODEL_DIRECTORIES = frozenset(
    {"pretrained_models", "checkpoints", "sovits_weights", "gpt_weights"}
)
SAFE_PACKAGE_ROOT = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$")
SAFE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")
PORTABLE_COMPONENTS = frozenset({"tts-more", "gpt-sovits", "indextts", "cosyvoice"})
RESOLVED_DEVICE_PROFILES = frozenset({"cpu", "cu126", "cu128"})
HASH_CHUNK_SIZE = 1024 * 1024
ZIP_METADATA_MAX_BYTES = 8 * 1024 * 1024


def _draft_2020_12_validator(schema: Any) -> Any:
    try:
        from jsonschema import Draft202012Validator
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "jsonschema is required for portable package schema validation, "
            "but is not required for runtime SHA-256 verification"
        ) from exc
    return Draft202012Validator(schema)


def _sha256_stream(stream: Any) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(HASH_CHUNK_SIZE), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return _sha256_stream(stream)


def _sha256_zip_member(archive: zipfile.ZipFile, name: str) -> str:
    with archive.open(name, "r") as stream:
        return _sha256_stream(stream)


def _read_zip_metadata(
    archive: zipfile.ZipFile,
    name: str,
    *,
    max_bytes: int = ZIP_METADATA_MAX_BYTES,
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    with archive.open(name, "r") as stream:
        for chunk in iter(lambda: stream.read(HASH_CHUNK_SIZE), b""):
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"ZIP metadata member exceeds {max_bytes} bytes: {name}")
            chunks.append(chunk)
    return b"".join(chunks)


def full_package_name(component: str, version: str, resolved_profile: str) -> str:
    """Return the only permitted Full ZIP name for a resolved device profile."""
    normalized_component = str(component).casefold()
    normalized_profile = str(resolved_profile).casefold()
    if normalized_component not in PORTABLE_COMPONENTS:
        raise ValueError("unknown portable component")
    if normalized_profile not in RESOLVED_DEVICE_PROFILES:
        raise ValueError("full package naming requires a resolved profile")
    if not isinstance(version, str) or not SAFE_VERSION.fullmatch(version):
        raise ValueError("package version is unsafe")
    if normalized_component == "tts-more":
        filename = f"TTS-More-{version}-windows-x64-full.zip"
    else:
        filename = (
            f"{normalized_component}-{version}-windows-x64-full-{normalized_profile}.zip"
        )
    if not SAFE_PACKAGE_ROOT.fullmatch(filename.removesuffix(".zip")):
        raise ValueError("full package name exceeds the safe package-root boundary")
    return filename


def resolve_full_profile(
    state_path: Path,
    *,
    expected_component: str,
    expected_build_id: str,
    requested_profile: str,
) -> str:
    """Resolve a Full worker profile only from a completed package-private state."""
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("full package install-state.json is missing or invalid") from exc
    return _resolve_full_profile_payload(
        payload,
        expected_component=expected_component,
        expected_build_id=expected_build_id,
        requested_profile=requested_profile,
    )


def _resolve_full_profile_payload(
    payload: Any,
    *,
    expected_component: str,
    expected_build_id: str,
    requested_profile: str,
) -> str:
    state = _mapping(payload)
    requested = str(requested_profile).casefold()
    if requested not in DEVICE_PROFILES:
        raise ValueError("requested device profile is unsupported")
    if state.get("schema_version") != 1 or state.get("ready") is not True:
        raise ValueError("full package install state is not ready")
    if state.get("component") != expected_component or state.get("build_id") != expected_build_id:
        raise ValueError("full package install state identity mismatch")
    for field in ("runtime_lock_sha256", "model_lock_sha256"):
        value = state.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
            raise ValueError(f"full package install state {field} is invalid")
    completed_at = state.get("completed_at")
    if not isinstance(completed_at, str) or not completed_at.strip():
        raise ValueError("full package install state completed_at is invalid")
    resolved = str(state.get("profile") or "").casefold()
    if resolved not in RESOLVED_DEVICE_PROFILES:
        raise ValueError("full package install state requires a resolved profile")
    if requested != "auto" and requested != resolved:
        raise ValueError("requested device profile does not match resolved profile")
    return resolved


def create_zip(package_root: Path, output: Path, archive_root: str | None = None) -> None:
    """Create a deterministic-order ZIP64 archive with one package root."""
    package_root = package_root.resolve(strict=True)
    output = output.resolve(strict=False)
    if archive_root is None:
        archive_root = package_root.name
    elif (
        not isinstance(archive_root, str)
        or not SAFE_PACKAGE_ROOT.fullmatch(archive_root)
        or archive_root != archive_root.rstrip(" .")
        or unicodedata.normalize("NFKC", archive_root) != archive_root
    ):
        raise ValueError("archive root is unsafe or ambiguous")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    with zipfile.ZipFile(
        temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
    ) as archive:
        for path in sorted(candidate for candidate in package_root.rglob("*") if candidate.is_file()):
            relative = path.relative_to(package_root).as_posix()
            archive.write(path, f"{archive_root}/{relative}")
    temporary.replace(output)


def select_full_package(
    candidates: list[Path],
    *,
    expected_component: str,
    expected_version: str,
    requested_profile: str,
    expected_source_revision: str,
) -> dict[str, object]:
    """Select and verify exactly one newly changed Full package artifact."""
    errors: list[str] = []
    report: dict[str, object] = {
        "valid": False,
        "errors": errors,
        "component": expected_component,
        "version": expected_version,
    }
    if re.fullmatch(r"[0-9a-f]{40}", expected_source_revision) is None:
        errors.append("expected source revision must be exactly 40 lowercase hexadecimal characters")
        return report
    if len(candidates) != 1:
        errors.append("component did not produce exactly one changed full ZIP")
        return report
    path = Path(candidates[0])
    report["path"] = str(path)
    report["filename"] = path.name
    try:
        actual_sha = _sha256_file(path)
        with zipfile.ZipFile(path) as archive:
            raw_package_root, package_root, relative_names = _single_zip_package_root(archive)
            manifest_name = relative_names.get("package/tts-more-package.json")
            if manifest_name is None:
                raise ValueError("full ZIP is missing its embedded package manifest")
            manifest = json.loads(_read_zip_metadata(archive, manifest_name).decode("utf-8-sig"))
            _validate_embedded_v2_package(archive, manifest, relative_names)
            if manifest.get("schema_version") != 2:
                raise ValueError("full ZIP manifest must use schema v2")
            component = str(manifest.get("component") or "")
            version = str(manifest.get("release_version") or "")
            if component != expected_component or manifest.get("package_id") != component:
                raise ValueError("full ZIP manifest component mismatch")
            if version != expected_version or manifest.get("version") != version:
                raise ValueError("full ZIP manifest version mismatch")
            if manifest.get("package_profile") != "full":
                raise ValueError("full ZIP manifest profile mismatch")
            build_id = str(manifest.get("build_id") or "")
            runtime = _mapping(manifest.get("runtime"))
            state_relative = runtime.get("state_path")
            if not isinstance(state_relative, str) or not _is_relative_package_path(state_relative):
                raise ValueError("full ZIP manifest state path is invalid")
            state_name = relative_names.get(_canonical_relative_path(state_relative))
            if state_name is None:
                raise ValueError("full ZIP install state is missing")
            state = json.loads(_read_zip_metadata(archive, state_name).decode("utf-8-sig"))
            resolved = _resolve_full_profile_payload(
                state,
                expected_component=component,
                expected_build_id=build_id,
                requested_profile=requested_profile,
            )
            if runtime.get("device_profiles") != [resolved]:
                raise ValueError("full ZIP manifest does not bind the resolved profile")
            source_revision = str(_mapping(manifest.get("source")).get("revision") or "")
            if source_revision != expected_source_revision:
                raise ValueError("full ZIP source revision does not match the expected repository revision")
            runtime_lock_name = relative_names[_canonical_relative_path(str(runtime["lock"]))]
            model_lock_name = relative_names[
                _canonical_relative_path(str(_mapping(manifest.get("models"))["lock"]))
            ]
            if state.get("runtime_lock_sha256") != _sha256_zip_member(
                archive, runtime_lock_name
            ):
                raise ValueError("full ZIP install state runtime lock digest mismatch")
            if state.get("model_lock_sha256") != _sha256_zip_member(
                archive, model_lock_name
            ):
                raise ValueError("full ZIP install state model lock digest mismatch")
            expected_filename = full_package_name(component, version, resolved)
            expected_root = expected_filename.removesuffix(".zip")
            if (
                path.name != expected_filename
                or raw_package_root != expected_root
                or package_root != expected_root.casefold()
            ):
                raise ValueError("full ZIP filename, archive root, and manifest identity do not match")
            _verify_zip_sha256_manifest(archive, relative_names)

        sidecar_text = Path(f"{path}.sha256").read_text(encoding="ascii").strip()
        sidecar_match = re.fullmatch(r"([0-9a-fA-F]{64})  ([^/\\]+)", sidecar_text)
        if sidecar_match is None or sidecar_match.group(2) != path.name:
            raise ValueError("full ZIP SHA-256 sidecar is invalid")
        if sidecar_match.group(1).casefold() != actual_sha:
            raise ValueError("full ZIP SHA-256 sidecar mismatch")
        provenance = json.loads(Path(f"{path}.provenance.json").read_text(encoding="utf-8-sig"))
        expected_provenance = {
            "component": expected_component,
            "version": expected_version,
            "profile": "full",
            "resolved_profile": resolved,
            "sha256": actual_sha,
            "source_revision": source_revision,
        }
        for key, expected in expected_provenance.items():
            if provenance.get(key) != expected:
                raise ValueError(f"full ZIP provenance {key} mismatch")
        _validate_delivery_sidecars(
            path,
            component=expected_component,
            version=expected_version,
            profile="full",
            resolved_profile=resolved,
            source_revision=source_revision,
            sha256=actual_sha,
        )
        report.update(
            {
                "valid": True,
                "errors": [],
                "resolved_profile": resolved,
                "sha256": actual_sha,
                "source_revision": source_revision,
                "sidecars_valid": True,
            }
        )
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
    ) as exc:
        errors.append(str(exc))
    return report


def _portable_schema_path() -> Path:
    candidates = (
        Path(__file__).resolve().parents[1]
        / "packaging"
        / "portable"
        / "tts-more-package.schema.json",
        Path(__file__).resolve().with_name("tts-more-package.schema.json"),
    )
    found = [path for path in candidates if path.is_file()]
    if len(found) != 1:
        raise ValueError("portable package schema is missing or ambiguous")
    return found[0]


def _delivery_comment(
    *,
    component: str,
    version: str,
    profile: str,
    resolved_profile: str | None,
    source_revision: str,
    sha256: str,
) -> str:
    resolved = resolved_profile or "none"
    return (
        "TTS-More delivery binding: "
        f"component={component};version={version};profile={profile};"
        f"resolved_profile={resolved};source_revision={source_revision};sha256={sha256}"
    )


def _validate_delivery_sidecars(
    path: Path,
    *,
    component: str,
    version: str,
    profile: str,
    resolved_profile: str | None,
    source_revision: str,
    sha256: str,
) -> None:
    def read_json(suffix: str) -> dict[str, Any]:
        try:
            payload = json.loads(Path(f"{path}.{suffix}").read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{suffix} sidecar is missing or invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{suffix} sidecar must be a JSON object")
        return payload

    spdx = read_json("spdx.json")
    expected_namespace = f"https://tts-more.local/spdx/{component}/{version}/{sha256}"
    creation = _mapping(spdx.get("creationInfo"))
    creators = creation.get("creators")
    if (
        spdx.get("spdxVersion") != "SPDX-2.3"
        or spdx.get("dataLicense") != "CC0-1.0"
        or spdx.get("SPDXID") != "SPDXRef-DOCUMENT"
        or spdx.get("name") != path.name.removesuffix(".zip")
        or spdx.get("documentNamespace") != expected_namespace
        or spdx.get("comment")
        != _delivery_comment(
            component=component,
            version=version,
            profile=profile,
            resolved_profile=resolved_profile,
            source_revision=source_revision,
            sha256=sha256,
        )
        or not isinstance(creation.get("created"), str)
        or not creation.get("created")
        or not isinstance(creators, list)
        or not creators
        or not isinstance(spdx.get("packages"), list)
    ):
        raise ValueError("spdx sidecar delivery binding or SPDX document metadata mismatch")

    licenses = read_json("licenses.json")
    license_delivery = _mapping(licenses.get("delivery"))
    expected_binding: dict[str, object] = {
        "component": component,
        "version": version,
        "profile": profile,
        "source_revision": source_revision,
        "sha256": sha256,
    }
    if resolved_profile is not None:
        expected_binding["resolved_profile"] = resolved_profile
    if (
        licenses.get("schema_version") != 1
        or licenses.get("component") != component
        or any(license_delivery.get(key) != value for key, value in expected_binding.items())
    ):
        raise ValueError("licenses sidecar delivery binding mismatch")

    acceptance = read_json("acceptance.json")
    acceptance_binding = dict(expected_binding)
    required_flags = (
        "manifest_valid",
        "schema_audit",
        "path_audit",
        "sha256_manifest_audit",
        "machine_path_scan",
    )
    if (
        acceptance.get("schema_version") != 1
        or any(acceptance.get(key) != value for key, value in acceptance_binding.items())
        or any(acceptance.get(flag) is not True for flag in required_flags)
        or acceptance.get("bootstrap_audit") is not (profile == "bootstrap")
    ):
        raise ValueError("acceptance sidecar identity or required audit flags mismatch")


def _validate_embedded_v2_package(
    archive: zipfile.ZipFile,
    manifest: Any,
    relative_names: dict[str, str],
    *,
    require_install_state: bool = True,
) -> None:
    schema = json.loads(_portable_schema_path().read_text(encoding="utf-8-sig"))
    validator = _draft_2020_12_validator(schema)
    schema_errors = sorted(validator.iter_errors(manifest), key=lambda error: list(error.path))
    if schema_errors:
        raise ValueError(f"full ZIP manifest schema v2 validation failed: {schema_errors[0].message}")
    payload = _mapping(manifest)
    runtime = _mapping(payload.get("runtime"))
    models = _mapping(payload.get("models"))
    data = _mapping(payload.get("data"))
    launchers = _mapping(payload.get("launchers"))
    path_values = [
        payload.get("data_root"),
        payload.get("sha256_manifest"),
        payload.get("licenses"),
        runtime.get("lock"),
        runtime.get("state_path"),
        models.get("lock"),
        *data.values(),
        *launchers.values(),
    ]
    for value in path_values:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or not _is_relative_package_path(value)
        ):
            raise ValueError("full ZIP manifest contains an unsafe required relative path")
    required_files = [
        payload["sha256_manifest"],
        payload["licenses"],
        runtime["lock"],
        models["lock"],
        *launchers.values(),
    ]
    if require_install_state:
        required_files.append(runtime["state_path"])
    for relative in required_files:
        canonical = _canonical_relative_path(str(relative))
        if canonical not in relative_names:
            raise ValueError(f"full ZIP required package file is missing: {relative}")
    if _canonical_relative_path(str(payload["sha256_manifest"])) != "sha256sums.txt":
        raise ValueError("full ZIP manifest must bind SHA256SUMS.txt")


def _single_zip_package_root(
    archive: zipfile.ZipFile,
) -> tuple[str, str, dict[str, str]]:
    entries = archive.infolist()
    raw_names = _raw_central_directory_names(archive, len(entries))
    raw_roots: set[str] = set()
    roots: set[str] = set()
    relative_names: dict[str, str] = {}
    for entry, raw_name in zip(entries, raw_names, strict=True):
        if "\\" in raw_name or "/" not in raw_name:
            raise ValueError("ZIP entry is outside its exact archive root")
        raw_root = raw_name.split("/", 1)[0]
        raw_roots.add(raw_root)
        canonical = _canonical_zip_entry(raw_name)
        if "/" not in canonical:
            raise ValueError("full ZIP entry is outside its package root")
        root, relative = canonical.split("/", 1)
        roots.add(root)
        if entry.is_dir():
            continue
        if relative in relative_names:
            raise ValueError(f"full ZIP contains a normalized path collision: {raw_name}")
        relative_names[relative] = raw_name
    if len(raw_roots) != 1 or len(roots) != 1:
        raise ValueError("full ZIP must contain exactly one archive root")
    raw_root = next(iter(raw_roots))
    if (
        not SAFE_PACKAGE_ROOT.fullmatch(raw_root)
        or raw_root != raw_root.rstrip(" .")
        or unicodedata.normalize("NFKC", raw_root) != raw_root
    ):
        raise ValueError("full ZIP archive root is unsafe or ambiguous")
    return raw_root, next(iter(roots)), relative_names


def _verify_zip_sha256_manifest(
    archive: zipfile.ZipFile, relative_names: dict[str, str]
) -> None:
    sums_name = relative_names.get("sha256sums.txt")
    if sums_name is None:
        raise ValueError("full ZIP SHA256SUMS.txt is missing")
    covered: dict[str, str] = {}
    for line in _read_zip_metadata(archive, sums_name).decode("utf-8-sig").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", line)
        if match is None or not _is_relative_package_path(match.group(2)):
            raise ValueError("full ZIP SHA256SUMS contains an invalid record")
        relative = _canonical_relative_path(match.group(2))
        if relative in covered:
            raise ValueError("full ZIP SHA256SUMS contains a duplicate path")
        covered[relative] = match.group(1).casefold()
    files = {key: value for key, value in relative_names.items() if key != "sha256sums.txt"}
    if set(covered) != set(files):
        raise ValueError("full ZIP SHA256SUMS exact coverage mismatch")
    for relative, expected in covered.items():
        if _sha256_zip_member(archive, files[relative]) != expected:
            raise ValueError(f"full ZIP SHA256SUMS hash mismatch: {relative}")


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
                canonical = _canonical_zip_entry(name)
                raw_parts = name.split("/")
                if len(raw_parts) < 2 or "\\" in raw_parts[0]:
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
                payload = json.loads(
                    _read_zip_metadata(archive, manifests[0]).decode("utf-8-sig")
                )
                if payload.get("package_profile") != "bootstrap":
                    errors.append(f"GitHub release upload refused for profile={payload.get('package_profile')}")
                if payload.get("schema_version") == 2 and isinstance(payload.get("launchers"), dict):
                    _audit_v2_user_layout(archive, payload, relative_names, errors)
            for relative, name in relative_names.items():
                parts = tuple(part for part in relative.split("/") if part)
                private_data = len(parts) >= 2 and parts[0] == "data" and parts[1] in {
                    "user",
                    "local",
                    "cache",
                    "models",
                }
                directory_parts = parts if archive.getinfo(name).is_dir() else parts[:-1]
                model_data = any(
                    part in RELEASE_FORBIDDEN_MODEL_DIRECTORIES for part in directory_parts
                )
                if (
                    (parts and parts[0] == "runtime")
                    or private_data
                    or model_data
                    or RELEASE_FORBIDDEN_PATH.search(relative)
                ):
                    errors.append(f"forbidden release asset: {relative}")
                    break
    except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        errors.append(f"invalid release ZIP: {exc}")
    return {"valid": not errors, "errors": errors, "path": str(path)}


def audit_release_assets(
    directory: Path,
    *,
    expected_component: str,
    expected_version: str,
) -> dict[str, object]:
    """Audit the exact Bootstrap asset set that may cross the GitHub boundary."""
    errors: list[str] = []
    directory = directory.resolve(strict=False)
    try:
        if expected_component not in PORTABLE_COMPONENTS:
            raise ValueError("expected release component is invalid")
        if SAFE_VERSION.fullmatch(expected_version) is None:
            raise ValueError("expected release version is invalid")
        if not directory.is_dir():
            raise ValueError("release asset directory is missing")
        files = sorted(path for path in directory.iterdir() if path.is_file())
        zips = [path for path in files if path.name.casefold().endswith(".zip")]
        if len(zips) != 1:
            errors.append("release asset directory must contain exactly one candidate ZIP")
        expected_files: set[str] = set()
        for path in zips:
            report = audit_release_zip(path)
            if not report["valid"]:
                errors.extend(str(error) for error in report["errors"])
                continue
            actual_sha = _sha256_file(path)
            with zipfile.ZipFile(path) as archive:
                raw_package_root, package_root, relative_names = _single_zip_package_root(archive)
                manifest_name = relative_names.get("package/tts-more-package.json")
                if manifest_name is None:
                    raise ValueError(f"release ZIP manifest is missing: {path.name}")
                manifest = json.loads(
                    _read_zip_metadata(archive, manifest_name).decode("utf-8-sig")
                )
                _validate_embedded_v2_package(
                    archive,
                    manifest,
                    relative_names,
                    require_install_state=False,
                )
                _verify_zip_sha256_manifest(archive, relative_names)
            if manifest.get("schema_version") != 2:
                raise ValueError(f"release ZIP manifest must use schema v2: {path.name}")
            profile = manifest.get("package_profile")
            if profile != "bootstrap":
                raise ValueError(f"profile={profile} is prohibited from GitHub Releases")
            component = str(manifest.get("component") or "")
            version = str(manifest.get("release_version") or "")
            if component not in PORTABLE_COMPONENTS or manifest.get("package_id") != component:
                raise ValueError(f"release ZIP manifest component is invalid: {path.name}")
            if not SAFE_VERSION.fullmatch(version) or manifest.get("version") != version:
                raise ValueError(f"release ZIP manifest version is invalid: {path.name}")
            if component != expected_component or version != expected_version:
                raise ValueError(
                    f"release ZIP does not match expected component/version: {path.name}"
                )
            expected_name = (
                f"TTS-More-{version}-windows-x64-bootstrap.zip"
                if component == "tts-more"
                else f"{component}-{version}-windows-x64-bootstrap.zip"
            )
            expected_root = expected_name.removesuffix(".zip")
            if (
                path.name != expected_name
                or raw_package_root != expected_root
                or package_root != expected_root.casefold()
            ):
                raise ValueError(f"release ZIP filename, archive root, and manifest identity mismatch: {path.name}")
            suffixes = (
                "",
                ".sha256",
                ".spdx.json",
                ".licenses.json",
                ".provenance.json",
                ".acceptance.json",
            )
            expected_files.update(f"{path.name}{suffix}" for suffix in suffixes)
            sidecar_text = Path(f"{path}.sha256").read_text(encoding="ascii").strip()
            sidecar = re.fullmatch(r"([0-9a-fA-F]{64})  ([^/\\]+)", sidecar_text)
            if sidecar is None or sidecar.group(2) != path.name or sidecar.group(1).casefold() != actual_sha:
                raise ValueError(f"release ZIP SHA-256 sidecar mismatch: {path.name}")
            provenance = json.loads(Path(f"{path}.provenance.json").read_text(encoding="utf-8-sig"))
            source_revision = str(_mapping(manifest.get("source")).get("revision") or "")
            expected_provenance = {
                "component": component,
                "version": version,
                "profile": "bootstrap",
                "source_revision": source_revision,
                "sha256": actual_sha,
            }
            for key, expected in expected_provenance.items():
                if provenance.get(key) != expected:
                    raise ValueError(f"release ZIP provenance {key} mismatch: {path.name}")
            _validate_delivery_sidecars(
                path,
                component=component,
                version=version,
                profile="bootstrap",
                resolved_profile=None,
                source_revision=source_revision,
                sha256=actual_sha,
            )
        actual_files = {path.name for path in files}
        if expected_files and actual_files != expected_files:
            missing = sorted(expected_files - actual_files)
            extra = sorted(actual_files - expected_files)
            errors.append(f"release asset allowlist mismatch: missing={missing}, extra={extra}")
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
    ) as exc:
        errors.append(str(exc))
    return {"valid": not errors, "errors": errors, "directory": str(directory)}


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_reparse_stat(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag) or stat.S_ISLNK(stat_result.st_mode)


def _reject_existing_reparse_ancestors(path: Path) -> None:
    path = _lexical_absolute(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError:
            break
        if _is_reparse_stat(current_stat):
            raise ValueError(f"output path contains a reparse point: {current}")


def _path_is_within(path: Path, parent: Path) -> bool:
    path_text = os.path.normcase(os.path.abspath(os.fspath(path)))
    parent_text = os.path.normcase(os.path.abspath(os.fspath(parent)))
    try:
        return os.path.commonpath((path_text, parent_text)) == parent_text
    except ValueError:
        return False


def validate_transaction_paths(
    output_root: Path,
    *,
    controller_root: Path,
    source_roots: list[Path],
) -> None:
    """Reject output/source overlap and existing reparse-point path components."""
    output = _lexical_absolute(output_root)
    controller = _lexical_absolute(controller_root)
    sources = [_lexical_absolute(path) for path in source_roots]
    if not sources or os.path.normcase(str(sources[0])) != os.path.normcase(str(controller)):
        raise ValueError("controller root must be the first source root")
    _reject_existing_reparse_ancestors(output)

    real_output = Path(os.path.realpath(output))
    real_controller = Path(os.path.realpath(controller))
    canonical_local_output = real_controller / "artifacts" / "portable" / "full-four"
    for index, source in enumerate(sources):
        real_source = Path(os.path.realpath(source))
        output_inside_source = _path_is_within(real_output, real_source)
        source_inside_output = _path_is_within(real_source, real_output)
        if not output_inside_source and not source_inside_output:
            continue
        allowed_controller_output = (
            index == 0
            and output_inside_source
            and _path_is_within(real_output, canonical_local_output)
        )
        if not allowed_controller_output:
            raise ValueError(f"OutputRoot overlaps a source root: {source}")


def directory_identity(path: Path) -> str:
    path = _lexical_absolute(path)
    path_stat = os.lstat(path)
    if not stat.S_ISDIR(path_stat.st_mode) or _is_reparse_stat(path_stat):
        raise ValueError(f"owned transaction path is not a plain directory: {path}")
    if int(path_stat.st_ino) == 0:
        raise ValueError(f"owned transaction path has no stable file identity: {path}")
    return f"{int(path_stat.st_dev):x}:{int(path_stat.st_ino):x}"


def publish_directory_no_replace(
    source: Path,
    destination: Path,
    *,
    expected_identity: str,
    expected_parent_identity: str,
) -> None:
    source = _lexical_absolute(source)
    destination = _lexical_absolute(destination)
    if source.parent != destination.parent:
        raise ValueError("transaction publication must remain within one parent directory")
    if directory_identity(source) != expected_identity:
        raise ValueError("transaction directory identity changed before publication")
    if directory_identity(destination.parent) != expected_parent_identity:
        raise ValueError("transaction parent directory identity changed before publication")
    try:
        os.lstat(destination)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"publication destination already exists: {destination}")
    os.rename(source, destination)
    if directory_identity(destination) != expected_identity:
        raise RuntimeError("published directory identity does not match the owned transaction")


def cleanup_owned_directory(path: Path, *, expected_identity: str) -> None:
    path = _lexical_absolute(path)
    if directory_identity(path) != expected_identity:
        raise ValueError("refusing to clean a replaced transaction directory")
    quarantine = path.with_name(f"{path.name}.cleanup-{uuid.uuid4().hex}")
    os.rename(path, quarantine)
    if directory_identity(quarantine) != expected_identity:
        raise RuntimeError("refusing to clean a transaction after identity changed")
    cleanup_path = os.fspath(quarantine)
    if os.name == "nt" and not cleanup_path.startswith("\\\\?\\"):
        cleanup_path = "\\\\?\\" + cleanup_path
    for attempt in range(10):
        try:
            if directory_identity(quarantine) != expected_identity:
                raise RuntimeError("refusing to clean a replaced quarantined transaction")
            shutil.rmtree(cleanup_path)
            return
        except OSError:
            if attempt == 9:
                raise
            time.sleep(0.05)


def _git_output(root: Path, *arguments: str, allow_empty_failure: bool = False) -> str:
    completed = subprocess.run(
        ["git", "-C", os.fspath(root), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        if allow_empty_failure and not completed.stdout.strip():
            return ""
        raise ValueError(f"Git source inventory failed: {' '.join(arguments)}")
    return completed.stdout


def _controller_builder_includes(relative: str) -> bool:
    relative = relative.replace("\\", "/")
    parts = relative.rstrip("/").split("/")
    if any(
        part in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
        for part in parts
    ):
        return False
    if relative.startswith(("backend/app/", "frontend/dist/")):
        return True
    copied_files = {
        "backend/pyproject.toml",
        "backend/uv.lock",
        "backend/.python-version",
        "Initialize.cmd",
        "Start.cmd",
        "Stop.cmd",
        "Repair.cmd",
        "LICENSE",
        "NOTICE",
        "repo.lock.json",
        "packaging/portable/toolchain.lock.json",
        "packaging/portable/runtime.lock.json",
        "packaging/portable/models.lock.json",
        "packaging/portable/tts-more-package.schema.json",
        "packaging/portable/error-catalog.zh-CN.json",
        "packaging/portable/使用说明-先看这里.txt",
    }
    copied_scripts = {
        "bootstrap-conda.ps1",
        "initialize-portable.ps1",
        "repair-portable.ps1",
        "start-production.ps1",
        "stop-production.ps1",
        "Invoke-PortableStart.ps1",
        "Show-PortableProgress.ps1",
        "Portable-Validation.ps1",
        "select-portable-folder.ps1",
        "export-portable-diagnostics.py",
        "import-portable-data.py",
        "import_portable_data.py",
        "portable_install.py",
        "portable_launcher.py",
        "portable_operations.py",
        "portable_packages.py",
        "portable_package_runner.py",
    }
    return relative in copied_files or (
        relative.startswith("scripts/") and relative[8:] in copied_scripts
    )


def _controller_build_input(relative: str) -> bool:
    relative = relative.replace("\\", "/")
    return (
        relative.startswith(("frontend/src/", "integrations/build_tools/"))
        or relative in {
            "frontend/index.html",
            "frontend/package.json",
            "frontend/pnpm-lock.yaml",
            "frontend/pnpm-workspace.yaml",
            "frontend/tsconfig.json",
            "frontend/vite.config.ts",
            "Build-Package.ps1",
            "scripts/Resolve-PortableBuildPython.ps1",
        }
    )


def _worker_builder_excludes(relative: str, profile: str) -> bool:
    parts = relative.replace("\\", "/").rstrip("/").split("/")
    root_excluded = {
        ".git",
        ".venv",
        "runtime",
        "data",
        "artifacts",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
    recursive_excluded = {
        ".git",
        ".venv",
        "artifacts",
        "cache",
        ".cache",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
    if parts[0] in root_excluded or any(part in recursive_excluded for part in parts):
        return True
    if re.fullmatch(r"\.env(?:\..+)?", parts[-1]):
        return True
    if any(part in {"SoVITS_weights", "GPT_weights"} for part in parts):
        return True
    if profile == "bootstrap" and (
        any(part in {"pretrained_models", "checkpoints"} for part in parts)
        or re.search(r"\.(safetensors|ckpt|pth|pt|t7|onnx|bin)$", parts[-1], re.IGNORECASE)
    ):
        return True
    return False


def _model_candidate(relative: str) -> bool:
    parts = relative.replace("\\", "/").split("/")
    return any(
        part in {"models", "pretrained_models", "checkpoints"} for part in parts
    ) or re.search(
        r"\.(safetensors|ckpt|pth|pt|t7|onnx|bin)$", parts[-1], re.IGNORECASE
    ) is not None


def _locked_model_assets(root: Path, component: str) -> dict[str, str]:
    if component == "tts-more":
        lock_path = root / "packaging" / "portable" / "models.lock.json"
    else:
        lock_path = root / "tts_more" / "locks" / "models.lock.json"
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    locked: dict[str, str] = {}
    for asset in payload.get("assets", []):
        if not isinstance(asset, dict):
            continue
        target = asset.get("target")
        digest = asset.get("sha256")
        if (
            isinstance(target, str)
            and _is_relative_package_path(target)
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", digest)
        ):
            locked[_canonical_relative_path(target)] = digest.casefold()
    return locked


def _untracked_repository_entries(root: Path) -> list[str]:
    output = _git_output(root, "ls-files", "--others", "-z")
    return [entry for entry in output.split("\0") if entry]


def _modified_repository_entries(root: Path) -> list[str]:
    output = _git_output(
        root,
        "diff",
        "--no-ext-diff",
        "--name-only",
        "-z",
        "HEAD",
        "--",
    )
    return [entry for entry in output.split("\0") if entry]


def _submodule_paths(root: Path) -> list[str]:
    output = _git_output(
        root,
        "config",
        "--file",
        ".gitmodules",
        "--get-regexp",
        r"^submodule\..*\.path$",
        allow_empty_failure=True,
    )
    paths: list[str] = []
    for line in output.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) == 2 and _is_relative_package_path(fields[1]):
            paths.append(fields[1].replace("\\", "/"))
    return paths


def audit_builder_source(root: Path, *, component: str, profile: str) -> dict[str, object]:
    """Audit revision drift using the active package builder's copy rules."""
    errors: list[str] = []
    root = root.resolve(strict=True)
    normalized_profile = profile.casefold()
    if component not in PORTABLE_COMPONENTS or normalized_profile not in {"bootstrap", "full"}:
        return {"valid": False, "errors": ["builder source audit identity is invalid"]}
    locked = _locked_model_assets(root, component)

    def inspect_entry(repository: Path, prefix: str, entry: str, *, tracked: bool) -> None:
        combined = f"{prefix}/{entry}".strip("/").replace("\\", "/")
        if component == "tts-more":
            # frontend/dist is a generated release boundary. CI/build orchestration
            # produces it from the revision-bound inputs below; it is intentionally
            # ignored rather than committed as generated source.
            if not tracked and combined.startswith("frontend/dist/"):
                return
            if _controller_builder_includes(combined) or _controller_build_input(combined):
                if tracked:
                    errors.append(f"copied tracked source differs from revision: {combined}")
                else:
                    errors.append(f"copied untracked source is not revision-bound: {combined}")
            return
        if _worker_builder_excludes(combined, normalized_profile):
            return
        candidate_path = repository / entry.rstrip("/")
        if candidate_path.is_symlink():
            return
        if not _model_candidate(combined):
            if tracked:
                errors.append(f"copied tracked source differs from revision: {combined}")
            else:
                errors.append(f"copied untracked source is not revision-bound: {combined}")
            return
        expected = locked.get(_canonical_relative_path(combined))
        if (
            normalized_profile != "full"
            or expected is None
            or not candidate_path.is_file()
            or _sha256_file(candidate_path) != expected
        ):
            errors.append(f"unlocked or modified model asset would be embedded: {combined}")

    def inspect_repository(repository: Path, prefix: str = "") -> None:
        for entry in _untracked_repository_entries(repository):
            inspect_entry(repository, prefix, entry, tracked=False)
        for entry in _modified_repository_entries(repository):
            inspect_entry(repository, prefix, entry, tracked=True)
        for submodule in _submodule_paths(repository):
            submodule_root = repository / submodule
            if submodule_root.is_dir():
                inspect_repository(submodule_root, f"{prefix}/{submodule}".strip("/"))

    try:
        inspect_repository(root)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    return {"valid": not errors, "errors": errors, "root": str(root)}


def _audit_v2_user_layout(
    archive: zipfile.ZipFile,
    payload: dict[str, Any],
    relative_names: dict[str, str],
    errors: list[str],
) -> None:
    required_root = {"使用说明-先看这里.txt", "app", "package", "licenses"}
    present_roots = {relative.split("/", 1)[0] for relative in relative_names}
    if not required_root <= present_roots:
        errors.append("portable package is missing the normal-user app/package/licenses layout or root guide")
        return
    for name in V2_LAUNCHERS:
        relative = str(payload["launchers"].get(name) or "").replace("\\", "/").casefold()
        if relative not in relative_names:
            errors.append(f"portable package root launcher is missing: {name}")
            return
    license_path = str(payload.get("licenses") or "").replace("\\", "/").casefold()
    if not license_path.startswith("licenses/") or license_path not in relative_names:
        errors.append("portable package licenses must be stored under licenses/")
        return
    if not any(relative.startswith("app/") for relative in relative_names):
        errors.append("portable package source is missing from app/")
        return

    component = str(payload.get("component") or "")
    if component not in {"gpt-sovits", "indextts", "cosyvoice"}:
        return
    if any(relative.startswith("tts_more/") for relative in relative_names):
        errors.append("worker integration bundle must be staged under app/tts_more")
        return
    component_path = "app/tts_more/component.json"
    runtime_lock = str(_mapping(payload.get("runtime")).get("lock") or "").replace("\\", "/").casefold()
    model_lock = str(_mapping(payload.get("models")).get("lock") or "").replace("\\", "/").casefold()
    if component_path not in relative_names or runtime_lock != "app/tts_more/locks/runtime.lock.json" or model_lock != "app/tts_more/locks/models.lock.json":
        errors.append("worker package manifest does not point to the staged app/tts_more locks")
        return
    try:
        component_config = json.loads(
            _read_zip_metadata(archive, relative_names[component_path]).decode("utf-8-sig")
        )
        model_payload = json.loads(
            _read_zip_metadata(archive, relative_names[model_lock]).decode("utf-8-sig")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"worker staged metadata is invalid: {exc}")
        return
    if component_config.get("source_root") != "app":
        errors.append("worker component source_root must be app")
        return
    required_paths = model_payload.get("required_paths")
    assets = model_payload.get("assets")
    if (
        not isinstance(required_paths, list)
        or not isinstance(assets, list)
        or any(not isinstance(asset, dict) for asset in assets)
    ):
        errors.append("worker staged model lock paths are invalid")
        return
    raw_locked_paths = [*required_paths, *(asset.get("target") for asset in assets)]
    canonical_locked_paths: list[str] = []
    try:
        for raw_path in raw_locked_paths:
            if (
                not isinstance(raw_path, str)
                or not raw_path
                or not _is_relative_package_path(raw_path)
            ):
                raise ValueError("invalid model lock path")
            canonical_locked_paths.append(_canonical_relative_path(raw_path))
    except ValueError:
        errors.append("worker staged model lock paths are invalid")
        return
    if not canonical_locked_paths or any(
        not path.startswith("app/") or path.startswith("app/app/")
        for path in canonical_locked_paths
    ):
        errors.append("worker model lock paths must be prefixed with app/ exactly once")
        return
    embedded_locked_paths = sorted(set(canonical_locked_paths) & set(relative_names))
    if embedded_locked_paths:
        errors.append(
            "worker bootstrap contains locked model asset: "
            f"{relative_names[embedded_locked_paths[0]]}"
        )


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
            actual = _sha256_file(files[canonical])
            if actual != expected:
                errors.append(f"SHA256SUMS hash mismatch: {relative}")
    except (OSError, ValueError) as exc:
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
    create.add_argument("--archive-root")
    full_name = subcommands.add_parser("full-package-name")
    full_name.add_argument("--component", required=True)
    full_name.add_argument("--version", required=True)
    full_name.add_argument("--resolved-profile", required=True)
    resolve_profile = subcommands.add_parser("resolve-full-profile")
    resolve_profile.add_argument("--state", required=True, type=Path)
    resolve_profile.add_argument("--component", required=True)
    resolve_profile.add_argument("--build-id", required=True)
    resolve_profile.add_argument("--requested-profile", required=True)
    select_full = subcommands.add_parser("select-full-package")
    select_full.add_argument("--zip", required=True, action="append", type=Path)
    select_full.add_argument("--expected-component", required=True)
    select_full.add_argument("--expected-version", required=True)
    select_full.add_argument("--requested-profile", required=True)
    select_full.add_argument("--expected-source-revision", required=True)
    audit = subcommands.add_parser("audit-release")
    audit.add_argument("--zip", required=True, action="append", type=Path)
    audit_assets = subcommands.add_parser("audit-release-assets")
    audit_assets.add_argument("--directory", required=True, type=Path)
    audit_assets.add_argument("--expected-component", required=True)
    audit_assets.add_argument("--expected-version", required=True)
    validate_paths = subcommands.add_parser("validate-transaction-paths")
    validate_paths.add_argument("--output-root", required=True, type=Path)
    validate_paths.add_argument("--controller-root", required=True, type=Path)
    validate_paths.add_argument("--source-root", required=True, action="append", type=Path)
    identity = subcommands.add_parser("directory-identity")
    identity.add_argument("--path", required=True, type=Path)
    publish = subcommands.add_parser("publish-directory")
    publish.add_argument("--source", required=True, type=Path)
    publish.add_argument("--destination", required=True, type=Path)
    publish.add_argument("--expected-identity", required=True)
    publish.add_argument("--expected-parent-identity", required=True)
    cleanup = subcommands.add_parser("cleanup-directory")
    cleanup.add_argument("--path", required=True, type=Path)
    cleanup.add_argument("--expected-identity", required=True)
    audit_source = subcommands.add_parser("audit-builder-source")
    audit_source.add_argument("--root", required=True, type=Path)
    audit_source.add_argument("--component", required=True)
    audit_source.add_argument("--profile", required=True)
    verify_sums = subcommands.add_parser("verify-sha256")
    verify_sums.add_argument("--package-root", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "validate-manifest":
        report = validate_manifest(args.manifest, args.package_root)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["valid"] else 1
    if args.command == "create-zip":
        create_zip(args.package_root, args.output, args.archive_root)
        return 0
    if args.command == "full-package-name":
        print(full_package_name(args.component, args.version, args.resolved_profile))
        return 0
    if args.command == "resolve-full-profile":
        print(
            resolve_full_profile(
                args.state,
                expected_component=args.component,
                expected_build_id=args.build_id,
                requested_profile=args.requested_profile,
            )
        )
        return 0
    if args.command == "select-full-package":
        report = select_full_package(
            args.zip,
            expected_component=args.expected_component,
            expected_version=args.expected_version,
            requested_profile=args.requested_profile,
            expected_source_revision=args.expected_source_revision,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["valid"] else 1
    if args.command == "audit-release":
        reports = [audit_release_zip(path) for path in args.zip]
        print(json.dumps({"valid": all(report["valid"] for report in reports), "reports": reports}, ensure_ascii=False, sort_keys=True))
        return 0 if all(report["valid"] for report in reports) else 1
    if args.command == "audit-release-assets":
        report = audit_release_assets(
            args.directory,
            expected_component=args.expected_component,
            expected_version=args.expected_version,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["valid"] else 1
    if args.command == "validate-transaction-paths":
        validate_transaction_paths(
            args.output_root,
            controller_root=args.controller_root,
            source_roots=args.source_root,
        )
        return 0
    if args.command == "directory-identity":
        print(directory_identity(args.path))
        return 0
    if args.command == "publish-directory":
        publish_directory_no_replace(
            args.source,
            args.destination,
            expected_identity=args.expected_identity,
            expected_parent_identity=args.expected_parent_identity,
        )
        return 0
    if args.command == "cleanup-directory":
        cleanup_owned_directory(args.path, expected_identity=args.expected_identity)
        return 0
    if args.command == "audit-builder-source":
        report = audit_builder_source(
            args.root,
            component=args.component,
            profile=args.profile,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["valid"] else 1
    if args.command == "verify-sha256":
        report = verify_sha256_manifest(args.package_root)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["valid"] else 1
    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
