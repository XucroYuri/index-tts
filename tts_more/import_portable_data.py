from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import stat
import sys
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_SUPPORTED_COMPONENTS = {"tts-more", "gpt-sovits", "indextts", "cosyvoice"}
_PROHIBITED_MODEL_SEGMENTS = {
    ".cache",
    ".runtime",
    ".venv",
    "cache",
    "caches",
    "env",
    "operation",
    "operations",
    "pid",
    "pids",
    "run",
    "runtime",
    "runtimes",
    "venv",
    "virtualenv",
}
_PROHIBITED_MODEL_ROOTS = {"data", "package", *_PROHIBITED_MODEL_SEGMENTS}


class PortableMigrationError(RuntimeError):
    """Raised when a portable-data import cannot be planned or applied safely."""


@dataclass(frozen=True)
class FileEvidence:
    device: int
    inode: int
    size_bytes: int
    mtime_ns: int
    nlink: int
    file_attributes: int
    sha256: str


@dataclass(frozen=True)
class DirectoryEvidence:
    device: int
    inode: int
    nlink: int
    file_attributes: int


@dataclass
class WindowsDestinationContext:
    root: Path
    root_handle: int
    directory_handles: dict[str, int]

    def close(self) -> None:
        for handle in reversed(tuple(dict.fromkeys(self.directory_handles.values()))):
            _windows_close_handle(handle)
        self.directory_handles.clear()


@dataclass(frozen=True)
class DirectorySnapshot:
    path: Path
    relative_path: str
    evidence: DirectoryEvidence | None


@dataclass(frozen=True)
class ExistingDestination:
    path: Path
    relative_path: str
    evidence: FileEvidence


@dataclass(frozen=True)
class ImportItem:
    source: Path
    destination: Path
    relative_path: str
    evidence: FileEvidence

    @property
    def size_bytes(self) -> int:
        return self.evidence.size_bytes

    @property
    def sha256(self) -> str:
        return self.evidence.sha256


@dataclass(frozen=True)
class ImportPlan:
    old_root: Path
    new_root: Path
    old_manifest: FileEvidence
    new_manifest: FileEvidence
    old_model_lock: FileEvidence
    new_model_lock: FileEvidence
    old_directories: tuple[DirectorySnapshot, ...]
    new_directories: tuple[DirectorySnapshot, ...]
    existing_destinations: tuple[ExistingDestination, ...]
    user_files: tuple[ImportItem, ...]
    reusable_assets: tuple[ImportItem, ...]
    skipped_assets: tuple[str, ...]
    already_present: tuple[str, ...]
    plan_digest: str


@dataclass(frozen=True)
class ImportReport:
    copied_user_files: int
    reused_assets: list[str]
    skipped_assets: list[str]
    already_present: list[str]


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _path_key(path: Path | str) -> str:
    normalized = unicodedata.normalize("NFKC", os.path.normpath(os.fspath(path)))
    return normalized.replace("/", "\\").casefold()


def _relative_key(path: Path | str) -> str:
    normalized = unicodedata.normalize("NFKC", os.fspath(path)).replace("\\", "/")
    return "/".join(part.casefold() for part in normalized.split("/"))


def _iter_path_chain(path: Path) -> Iterable[Path]:
    anchor = Path(path.anchor)
    current = anchor
    if path.anchor:
        yield anchor
    for part in path.parts[1:] if path.anchor else path.parts:
        current = current / part
        yield current


def _assert_existing_root(raw: Path | str, label: str) -> Path:
    lexical = Path(os.path.abspath(os.fspath(raw)))
    for candidate in _iter_path_chain(lexical):
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise PortableMigrationError(f"{label} is missing or inaccessible: {candidate}: {exc}") from exc
        if _is_reparse(metadata) or candidate.is_symlink():
            raise PortableMigrationError(f"{label} path traverses a reparse point or link: {candidate}")
        if candidate != lexical and not stat.S_ISDIR(metadata.st_mode):
            raise PortableMigrationError(f"{label} ancestor is not a directory: {candidate}")
    try:
        metadata = lexical.lstat()
    except OSError as exc:
        raise PortableMigrationError(f"{label} is missing or inaccessible: {lexical}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise PortableMigrationError(f"{label} must be a directory: {lexical}")
    resolved = lexical.resolve(strict=True)
    if _path_key(resolved) != _path_key(lexical):
        raise PortableMigrationError(f"{label} resolves through an unsafe link")
    return resolved


def _assert_distinct_roots(old: Path, new: Path) -> None:
    old_key = _path_key(old).rstrip("\\")
    new_key = _path_key(new).rstrip("\\")
    if old_key == new_key:
        raise PortableMigrationError("old and new package roots cannot be the same")
    if new_key.startswith(old_key + "\\") or old_key.startswith(new_key + "\\"):
        raise PortableMigrationError("old and new package roots cannot contain or nest each other")


def _validate_segment(segment: str, label: str) -> None:
    normalized = unicodedata.normalize("NFKC", segment)
    if not segment or segment in {".", ".."}:
        raise PortableMigrationError(f"{label} contains an unsafe relative path segment")
    if any(ord(character) < 32 for character in segment):
        raise PortableMigrationError(f"{label} contains an unsafe control character")
    if any(character in "/\\:" for character in normalized):
        raise PortableMigrationError(f"{label} contains an unsafe ADS target")
    if segment.endswith((".", " ")) or normalized.endswith((".", " ")):
        raise PortableMigrationError(f"{label} contains an unsafe trailing dot or space")
    basename = normalized.split(".", 1)[0].casefold()
    if basename in _RESERVED_NAMES:
        raise PortableMigrationError(f"{label} contains a reserved Windows target name")


def _relative_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")):
        raise PortableMigrationError(f"{label} must be a non-empty safe relative path")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if any(not part for part in parts):
        raise PortableMigrationError(f"{label} contains an unsafe empty path segment")
    for part in parts:
        _validate_segment(part, label)
    return Path(*parts)


def _safe_child(root: Path, relative: Path, label: str) -> Path:
    candidate = root.joinpath(relative)
    root_key = _path_key(root).rstrip("\\")
    candidate_key = _path_key(candidate)
    if not candidate_key.startswith(root_key + "\\"):
        raise PortableMigrationError(f"{label} escapes the portable package root")
    return candidate


def _assert_user_data_path(relative: Path, label: str) -> None:
    if relative.as_posix() != "data/user":
        raise PortableMigrationError(f"{label} must be exactly data/user")


def _assert_model_target_allowed(relative: Path) -> None:
    parts = [unicodedata.normalize("NFKC", part).casefold() for part in relative.parts]
    filename = parts[-1]
    if parts[0] in _PROHIBITED_MODEL_ROOTS or any(
        part in _PROHIBITED_MODEL_SEGMENTS for part in parts
    ):
        raise PortableMigrationError(
            f"model asset target enters a prohibited package area: {relative.as_posix()}"
        )
    if (
        filename == "install-state.json"
        or filename == "operation.json"
        or filename == "pid.json"
        or filename.endswith(".pid")
        or filename.endswith(".pid.json")
    ):
        raise PortableMigrationError(
            f"model asset target enters a prohibited package area: {relative.as_posix()}"
        )


def _sha256_stream(stream: Any) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _capture_file(path: Path, label: str, *, require_single_link: bool = True) -> FileEvidence:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PortableMigrationError(f"cannot inspect {label}: {path}: {exc}") from exc
    if _is_reparse(metadata) or path.is_symlink():
        raise PortableMigrationError(f"{label} cannot be a reparse point or link: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise PortableMigrationError(f"{label} must be a regular file: {path}")
    if require_single_link and metadata.st_nlink != 1:
        raise PortableMigrationError(f"{label} cannot be a hard link: {path}")
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino:
                raise PortableMigrationError(f"{label} identity changed while opening: {path}")
            digest = _sha256_stream(stream)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise PortableMigrationError(f"cannot read {label}: {path}: {exc}") from exc
    if _stat_identity(opened) != _stat_identity(after):
        raise PortableMigrationError(f"{label} changed while hashing: {path}")
    return FileEvidence(
        device=after.st_dev,
        inode=after.st_ino,
        size_bytes=after.st_size,
        mtime_ns=after.st_mtime_ns,
        nlink=after.st_nlink,
        file_attributes=getattr(after, "st_file_attributes", 0),
        sha256=digest,
    )


def _capture_directory(path: Path, label: str) -> DirectoryEvidence:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PortableMigrationError(f"cannot inspect {label}: {path}: {exc}") from exc
    if _is_reparse(metadata) or path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise PortableMigrationError(f"{label} is unsafe; it must be an ordinary directory: {path}")
    return DirectoryEvidence(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        nlink=metadata.st_nlink,
        file_attributes=getattr(metadata, "st_file_attributes", 0),
    )


def _snapshot_directory_chain(
    root: Path,
    directory: Path,
    label: str,
    snapshots: dict[str, DirectorySnapshot],
) -> None:
    try:
        relative = directory.relative_to(root)
    except ValueError as exc:
        raise PortableMigrationError(f"{label} escapes the package root") from exc
    current = root
    candidates = [(current, ".")]
    for part in relative.parts:
        current /= part
        candidates.append((current, current.relative_to(root).as_posix()))
    ancestor_missing = False
    for path, relative_path in candidates:
        evidence: DirectoryEvidence | None
        if ancestor_missing:
            evidence = None
        else:
            try:
                evidence = _capture_directory(path, label)
            except PortableMigrationError as exc:
                if path.exists() or path.is_symlink():
                    raise
                if not isinstance(exc.__cause__, FileNotFoundError):
                    raise
                ancestor_missing = True
                evidence = None
        key = _path_key(path)
        snapshot = DirectorySnapshot(path, relative_path, evidence)
        previous = snapshots.get(key)
        if previous is not None and previous != snapshot:
            raise PortableMigrationError(f"{label} changed while planning: {path}")
        snapshots[key] = snapshot


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_nlink,
        getattr(metadata, "st_file_attributes", 0),
    )


def _evidence_matches(path: Path, evidence: FileEvidence, label: str) -> bool:
    try:
        current = _capture_file(path, label)
    except PortableMigrationError:
        return False
    return current == evidence


def _load_json_with_evidence(
    root: Path, path: Path, label: str
) -> tuple[dict[str, Any], FileEvidence]:
    _assert_existing_chain(root, path, label)
    try:
        metadata = path.lstat()
        if _is_reparse(metadata) or path.is_symlink():
            raise PortableMigrationError(f"{label} cannot be a reparse point or link: {path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise PortableMigrationError(f"{label} must be a regular file: {path}")
        if metadata.st_nlink != 1:
            raise PortableMigrationError(f"{label} cannot be a hard link: {path}")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _stat_identity(opened) != _stat_identity(metadata):
                raise PortableMigrationError(f"{label} identity changed while opening: {path}")
            if os.name == "nt":
                import msvcrt

                _windows_assert_handle_inside(msvcrt.get_osfhandle(stream.fileno()), root, label)
            content = stream.read()
            after = os.fstat(stream.fileno())
        if _stat_identity(opened) != _stat_identity(after):
            raise PortableMigrationError(f"{label} changed while reading: {path}")
        payload = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PortableMigrationError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PortableMigrationError(f"{label} must contain a JSON object: {path}")
    _assert_existing_chain(root, path, label)
    evidence = FileEvidence(
        device=after.st_dev,
        inode=after.st_ino,
        size_bytes=after.st_size,
        mtime_ns=after.st_mtime_ns,
        nlink=after.st_nlink,
        file_attributes=getattr(after, "st_file_attributes", 0),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    return payload, evidence


def _package_schema() -> dict[str, Any]:
    module_path = Path(__file__).resolve()
    candidates = (
        module_path.parent / "tts-more-package.schema.json",
        module_path.parent.parent
        / "packaging"
        / "portable"
        / "tts-more-package.schema.json",
    )
    existing = tuple(path for path in candidates if path.is_file())
    if not existing:
        raise PortableMigrationError("portable package schema is missing")
    if len(existing) != 1:
        raise PortableMigrationError("portable package schema location is ambiguous")
    schema_root = module_path.parent.parent
    schema, _ = _load_json_with_evidence(
        schema_root, existing[0], "portable package schema"
    )
    return schema


def _manifest(root: Path) -> tuple[dict[str, Any], FileEvidence]:
    manifest, evidence = _load_json_with_evidence(
        root, root / "package" / "tts-more-package.json", "package manifest"
    )
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise PortableMigrationError(
            "portable migration requires jsonschema to validate package manifests"
        ) from exc
    try:
        schema = _package_schema()
        errors = sorted(
            Draft202012Validator(schema).iter_errors(manifest),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PortableMigrationError(f"cannot load portable package schema: {exc}") from exc
    if errors:
        raise PortableMigrationError(f"package manifest schema validation failed: {errors[0].message}")
    if manifest.get("schema_version") != 2:
        raise PortableMigrationError("portable migration requires package manifest schema version 2")
    component = manifest.get("component")
    if not isinstance(component, str) or component not in _SUPPORTED_COMPONENTS:
        raise PortableMigrationError("package manifest component is missing or unsupported")
    return manifest, evidence


def _model_lock(
    root: Path, manifest: dict[str, Any]
) -> tuple[dict[str, Any], FileEvidence, Path]:
    models = manifest.get("models")
    if not isinstance(models, dict):
        raise PortableMigrationError("package manifest models contract is missing")
    relative = _relative_path(models.get("lock"), "model lock path")
    lock_path = _safe_child(root, relative, "model lock path")
    payload, evidence = _load_json_with_evidence(root, lock_path, "model lock")
    return payload, evidence, lock_path


def _assert_existing_chain(root: Path, candidate: Path, label: str) -> None:
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise PortableMigrationError(f"{label} escapes the package root") from exc
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PortableMigrationError(f"cannot inspect {label}: {current}: {exc}") from exc
        if _is_reparse(metadata) or current.is_symlink():
            raise PortableMigrationError(f"{label} traverses a reparse point or link: {current}")
        if current != candidate and not stat.S_ISDIR(metadata.st_mode):
            raise PortableMigrationError(f"{label} ancestor is not a directory: {current}")


def _walk_user_files(user_root: Path) -> list[tuple[Path, Path]]:
    try:
        metadata = user_root.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise PortableMigrationError(f"cannot inspect user data root: {user_root}: {exc}") from exc
    if _is_reparse(metadata) or user_root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise PortableMigrationError("user data root must be a real directory, not a reparse point or link")
    results: list[tuple[Path, Path]] = []
    stack = [user_root]
    seen: dict[str, Path] = {}
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise PortableMigrationError(f"cannot enumerate user data: {directory}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                # DirEntry.stat(..., follow_symlinks=False) reports st_nlink=0
                # on the supported Windows Python runtime. Path.lstat returns
                # the actual NTFS link count required by the migration policy.
                item_metadata = path.lstat()
            except OSError as exc:
                raise PortableMigrationError(f"cannot inspect user data: {path}: {exc}") from exc
            if entry.is_symlink() or _is_reparse(item_metadata):
                raise PortableMigrationError(f"user data cannot contain a reparse point or link: {path}")
            relative = path.relative_to(user_root)
            _relative_path(relative.as_posix(), "user data path")
            key = _relative_key(relative.as_posix())
            previous = seen.get(key)
            if previous is not None and previous != relative:
                raise PortableMigrationError(
                    f"user data path normalization collision: {previous.as_posix()} and {relative.as_posix()}"
                )
            seen[key] = relative
            if stat.S_ISDIR(item_metadata.st_mode):
                stack.append(path)
            elif stat.S_ISREG(item_metadata.st_mode):
                if item_metadata.st_nlink != 1:
                    raise PortableMigrationError(f"user data file cannot be a hard link: {path}")
                results.append((path, relative))
            else:
                raise PortableMigrationError(f"user data must contain only regular files: {path}")
    return sorted(results, key=lambda item: _relative_key(item[1].as_posix()))


def _parse_assets(lock: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    assets = lock.get("assets", [])
    if not isinstance(assets, list):
        raise PortableMigrationError(f"{label} model lock assets must be a list")
    result: dict[str, dict[str, Any]] = {}
    normalized_targets: dict[str, str] = {}
    for raw in assets:
        if not isinstance(raw, dict):
            raise PortableMigrationError(f"{label} model lock asset must be an object")
        raw_target = raw.get("target")
        target = _relative_path(raw_target, "model asset target")
        _assert_model_target_allowed(target)
        relative = target.as_posix()
        key = _relative_key(relative)
        if key in normalized_targets:
            raise PortableMigrationError(f"model asset target normalization collision: {relative}")
        normalized_targets[key] = relative
        sha256 = raw.get("sha256")
        size_bytes = raw.get("size_bytes")
        if not isinstance(sha256, str) or len(sha256) != 64 or any(
            character not in "0123456789abcdefABCDEF" for character in sha256
        ):
            raise PortableMigrationError(f"model asset target has an invalid sha256: {relative}")
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
            raise PortableMigrationError(f"model asset target has an invalid size: {relative}")
        assert isinstance(raw_target, str)
        result[raw_target] = {
            "relative": target,
            "path": relative,
            "sha256": sha256.casefold(),
            "size_bytes": size_bytes,
        }
    return result


def _model_source_evidence(root: Path, relative: Path) -> FileEvidence | None:
    candidate = _safe_child(root, relative, "model asset target")
    try:
        _assert_existing_chain(root, candidate, "model asset target")
        metadata = candidate.lstat()
    except (FileNotFoundError, PortableMigrationError, OSError):
        return None
    if _is_reparse(metadata) or candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        return None
    if metadata.st_nlink != 1:
        return None
    try:
        return _capture_file(candidate, "model asset")
    except PortableMigrationError:
        return None


def _same_content(path: Path, evidence: FileEvidence) -> bool:
    try:
        current = _capture_file(path, "existing migration destination")
    except PortableMigrationError:
        return False
    return current.size_bytes == evidence.size_bytes and current.sha256 == evidence.sha256


def _item(source: Path, destination: Path, relative_path: str, label: str) -> ImportItem:
    return ImportItem(source, destination, relative_path, _capture_file(source, label))


def _evidence_payload(evidence: FileEvidence) -> dict[str, object]:
    return {
        "device": evidence.device,
        "inode": evidence.inode,
        "size_bytes": evidence.size_bytes,
        "mtime_ns": evidence.mtime_ns,
        "nlink": evidence.nlink,
        "file_attributes": evidence.file_attributes,
        "sha256": evidence.sha256,
    }


def _directory_evidence_payload(evidence: DirectoryEvidence | None) -> dict[str, object] | None:
    if evidence is None:
        return None
    return {
        "device": evidence.device,
        "inode": evidence.inode,
        "nlink": evidence.nlink,
        "file_attributes": evidence.file_attributes,
    }


def _plan_digest(plan_fields: dict[str, object]) -> str:
    encoded = json.dumps(
        plan_fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def plan_import(old_root: Path | str, new_root: Path | str) -> ImportPlan:
    """Build a deterministic import plan without writing either package."""

    old = _assert_existing_root(old_root, "old package root")
    new = _assert_existing_root(new_root, "new package root")
    _assert_distinct_roots(old, new)
    old_directories: dict[str, DirectorySnapshot] = {}
    new_directories: dict[str, DirectorySnapshot] = {}
    existing_destinations: list[ExistingDestination] = []
    old_manifest, old_manifest_evidence = _manifest(old)
    new_manifest, new_manifest_evidence = _manifest(new)
    _snapshot_directory_chain(
        old, old / "package", "old package manifest parent", old_directories
    )
    _snapshot_directory_chain(
        new, new / "package", "new package manifest parent", new_directories
    )
    if old_manifest["component"] != new_manifest["component"]:
        raise PortableMigrationError("portable package component mismatch")

    old_data = old_manifest.get("data")
    new_data = new_manifest.get("data")
    if not isinstance(old_data, dict) or not isinstance(new_data, dict):
        raise PortableMigrationError("package manifest data contract is missing")
    old_user_relative = _relative_path(old_data.get("user"), "old user data path")
    new_user_relative = _relative_path(new_data.get("user"), "new user data path")
    _assert_user_data_path(old_user_relative, "old user data path")
    _assert_user_data_path(new_user_relative, "new user data path")
    old_user = _safe_child(old, old_user_relative, "old user data path")
    new_user = _safe_child(new, new_user_relative, "new user data path")
    _assert_existing_chain(old, old_user, "old user data path")
    _assert_existing_chain(new, new_user, "new user data path")
    _snapshot_directory_chain(old, old_user, "old user data path", old_directories)
    _snapshot_directory_chain(new, new_user, "new user data path", new_directories)

    user_files: list[ImportItem] = []
    already_present: list[str] = []
    for source, suffix in _walk_user_files(old_user):
        _snapshot_directory_chain(
            old, source.parent, "user data source parent", old_directories
        )
        relative = (new_user_relative / suffix).as_posix()
        destination = _safe_child(new, new_user_relative / suffix, "user destination")
        item = _item(source, destination, relative, "user data file")
        _assert_existing_chain(new, destination, "user destination")
        _snapshot_directory_chain(
            new, destination.parent, "user destination parent", new_directories
        )
        if destination.exists():
            destination_evidence = _capture_file(destination, "existing migration destination")
            if (
                destination_evidence.size_bytes != item.evidence.size_bytes
                or destination_evidence.sha256 != item.evidence.sha256
            ):
                raise PortableMigrationError(f"destination conflict has different content: {relative}")
            existing_destinations.append(
                ExistingDestination(destination, relative, destination_evidence)
            )
            already_present.append(relative)
        else:
            user_files.append(item)

    old_lock, old_lock_evidence, old_lock_path = _model_lock(old, old_manifest)
    new_lock, new_lock_evidence, new_lock_path = _model_lock(new, new_manifest)
    _snapshot_directory_chain(
        old, old_lock_path.parent, "old model lock parent", old_directories
    )
    _snapshot_directory_chain(
        new, new_lock_path.parent, "new model lock parent", new_directories
    )
    old_assets = _parse_assets(old_lock, "old")
    new_assets = _parse_assets(new_lock, "new")
    reusable_assets: list[ImportItem] = []
    skipped_assets: list[str] = []
    for target in sorted(new_assets, key=str.casefold):
        wanted = new_assets[target]
        relative = wanted["relative"]
        relative_text = str(wanted["path"])
        previous = old_assets.get(target)
        if (
            previous is None
            or previous["sha256"] != wanted["sha256"]
            or previous["size_bytes"] != wanted["size_bytes"]
        ):
            skipped_assets.append(relative_text)
            continue
        evidence = _model_source_evidence(old, relative)
        if (
            evidence is None
            or evidence.sha256 != wanted["sha256"]
            or evidence.size_bytes != wanted["size_bytes"]
        ):
            skipped_assets.append(relative_text)
            continue
        source = _safe_child(old, relative, "model asset target")
        destination = _safe_child(new, relative, "model asset target")
        _assert_existing_chain(new, destination, "model asset destination")
        _snapshot_directory_chain(
            old, source.parent, "model asset source parent", old_directories
        )
        _snapshot_directory_chain(
            new, destination.parent, "model asset destination parent", new_directories
        )
        item = ImportItem(source, destination, relative_text, evidence)
        if destination.exists():
            destination_evidence = _capture_file(destination, "existing migration destination")
            if (
                destination_evidence.size_bytes != evidence.size_bytes
                or destination_evidence.sha256 != evidence.sha256
            ):
                raise PortableMigrationError(
                    f"destination conflict has different content: {relative_text}"
                )
            existing_destinations.append(
                ExistingDestination(destination, relative_text, destination_evidence)
            )
            already_present.append(relative_text)
        else:
            reusable_assets.append(item)

    frozen_user = tuple(user_files)
    frozen_assets = tuple(reusable_assets)
    frozen_old_directories = tuple(
        sorted(old_directories.values(), key=lambda item: _relative_key(item.relative_path))
    )
    frozen_new_directories = tuple(
        sorted(new_directories.values(), key=lambda item: _relative_key(item.relative_path))
    )
    frozen_destinations = tuple(
        sorted(existing_destinations, key=lambda item: _relative_key(item.relative_path))
    )
    frozen_skipped = tuple(skipped_assets)
    frozen_present = tuple(sorted(already_present, key=str.casefold))

    def item_payload(item: ImportItem) -> dict[str, object]:
        return {
            "relative_path": item.relative_path,
            "evidence": _evidence_payload(item.evidence),
        }

    def directory_payload(item: DirectorySnapshot) -> dict[str, object]:
        return {
            "relative_path": item.relative_path,
            "evidence": _directory_evidence_payload(item.evidence),
        }

    def destination_payload(item: ExistingDestination) -> dict[str, object]:
        return {
            "relative_path": item.relative_path,
            "evidence": _evidence_payload(item.evidence),
        }

    digest_fields: dict[str, object] = {
        "old_root": _path_key(old),
        "new_root": _path_key(new),
        "old_manifest": _evidence_payload(old_manifest_evidence),
        "new_manifest": _evidence_payload(new_manifest_evidence),
        "old_model_lock": _evidence_payload(old_lock_evidence),
        "new_model_lock": _evidence_payload(new_lock_evidence),
        "old_directories": [directory_payload(item) for item in frozen_old_directories],
        "new_directories": [directory_payload(item) for item in frozen_new_directories],
        "existing_destinations": [
            destination_payload(item) for item in frozen_destinations
        ],
        "user_files": [item_payload(item) for item in frozen_user],
        "reusable_assets": [item_payload(item) for item in frozen_assets],
        "skipped_assets": list(frozen_skipped),
        "already_present": list(frozen_present),
    }
    return ImportPlan(
        old,
        new,
        old_manifest_evidence,
        new_manifest_evidence,
        old_lock_evidence,
        new_lock_evidence,
        frozen_old_directories,
        frozen_new_directories,
        frozen_destinations,
        frozen_user,
        frozen_assets,
        frozen_skipped,
        frozen_present,
        _plan_digest(digest_fields),
    )


def _verify_directory_snapshot(
    snapshot: DirectorySnapshot,
    created: dict[str, DirectoryEvidence],
    label: str,
) -> None:
    expected = created.get(_path_key(snapshot.path), snapshot.evidence)
    if expected is None:
        try:
            snapshot.path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PortableMigrationError(
                f"cannot inspect planned {label}: {snapshot.path}: {exc}"
            ) from exc
        raise PortableMigrationError(f"planned {label} appeared after planning: {snapshot.path}")
    current = _capture_directory(snapshot.path, label)
    if current != expected:
        raise PortableMigrationError(f"planned {label} identity changed: {snapshot.path}")


def _verify_import_state(
    plan: ImportPlan, created: dict[str, DirectoryEvidence]
) -> None:
    for snapshot in plan.old_directories:
        _verify_directory_snapshot(snapshot, {}, "source directory")
    for snapshot in plan.new_directories:
        _verify_directory_snapshot(snapshot, created, "destination directory")
    for destination in plan.existing_destinations:
        if not _evidence_matches(
            destination.path, destination.evidence, "existing migration destination"
        ):
            raise PortableMigrationError(
                f"existing migration destination identity changed: {destination.relative_path}"
            )


def _ensure_destination_parent(
    plan: ImportPlan,
    destination: Path,
    created: dict[str, DirectoryEvidence],
    windows_context: WindowsDestinationContext | None = None,
) -> None:
    root = plan.new_root
    planned = {_path_key(item.path): item for item in plan.new_directories}
    try:
        relative = destination.parent.relative_to(root)
    except ValueError as exc:
        raise PortableMigrationError("migration target escapes the new package root") from exc
    current = root
    current_handle = windows_context.root_handle if windows_context is not None else None
    for part in ((), *relative.parts):
        if part:
            current /= part
        key = _path_key(current)
        snapshot = planned.get(key)
        if snapshot is None:
            raise PortableMigrationError(
                f"migration target directory was not frozen by the plan: {current}"
            )
        if windows_context is not None:
            if not part:
                handle = windows_context.root_handle
            else:
                if current_handle is None:
                    raise PortableMigrationError("migration destination parent handle is unavailable")
                handle = windows_context.directory_handles.get(key)
                if handle is None:
                    handle = _windows_open_relative(
                        current_handle,
                        part,
                        directory=True,
                        create=snapshot.evidence is None and key not in created,
                        display_path=current,
                    )
                    windows_context.directory_handles[key] = handle
            expected = created.get(key, snapshot.evidence)
            if expected is None:
                expected = _windows_directory_evidence(handle)
                created[key] = expected
            elif not _windows_directory_handle_matches(handle, expected):
                raise PortableMigrationError(
                    f"planned destination directory identity changed: {current}"
                )
            if _windows_handle_metadata(handle)[5] & _REPARSE_POINT:
                raise PortableMigrationError(
                    f"migration target directory is an unsafe reparse point: {current}"
                )
            _windows_assert_handle_below_root(
                handle, windows_context.root_handle, "migration destination directory"
            )
            current_handle = handle
            continue
        if snapshot.evidence is not None or key in created:
            _verify_directory_snapshot(snapshot, created, "destination directory")
            continue
        try:
            current.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PortableMigrationError(
                f"cannot inspect migration target directory: {current}: {exc}"
            ) from exc
        else:
            raise PortableMigrationError(
                f"migration target directory appeared after planning: {current}"
            )
        try:
            current.mkdir()
        except FileExistsError as exc:
            raise PortableMigrationError(
                f"migration target directory appeared during creation: {current}"
            ) from exc
        except OSError as exc:
            raise PortableMigrationError(f"cannot create migration target directory: {current}: {exc}") from exc
        created[key] = _capture_directory(current, "migration target directory")


def _copy_source_to_temporary(
    item: ImportItem,
    temporary: Path,
    *,
    parent_handle: int | None = None,
    retained_handle: list[int] | None = None,
) -> FileEvidence:
    relative = _relative_path(item.relative_path, "migration item path")
    source_root = item.source
    destination_root = item.destination
    for _ in relative.parts:
        source_root = source_root.parent
        destination_root = destination_root.parent
    if _path_key(_safe_child(source_root, relative, "migration source")) != _path_key(item.source):
        raise PortableMigrationError(f"migration source path is inconsistent: {item.relative_path}")
    if _path_key(_safe_child(destination_root, relative, "migration target")) != _path_key(
        item.destination
    ):
        raise PortableMigrationError(f"migration target path is inconsistent: {item.relative_path}")
    if os.name == "nt" and parent_handle is not None:
        temporary_handle: int | None = None
        try:
            temporary_handle = _windows_open_relative(
                parent_handle,
                temporary.name,
                directory=False,
                create=True,
                display_path=temporary,
            )
            with item.source.open("rb") as source:
                before = os.fstat(source.fileno())
                if _stat_identity(before) != (
                    item.evidence.device,
                    item.evidence.inode,
                    item.evidence.size_bytes,
                    item.evidence.mtime_ns,
                    item.evidence.nlink,
                    item.evidence.file_attributes,
                ):
                    raise PortableMigrationError(
                        f"migration source identity changed: {item.relative_path}"
                    )
                import msvcrt

                _windows_assert_handle_inside(
                    msvcrt.get_osfhandle(source.fileno()), source_root, "migration source"
                )
                _windows_assert_handle_below_root(
                    temporary_handle, parent_handle, "migration temporary file"
                )
                digest = hashlib.sha256()
                copied = 0
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    _windows_write_all(temporary_handle, block)
                    digest.update(block)
                    copied += len(block)
                _windows_flush_handle(temporary_handle)
                after = os.fstat(source.fileno())
            if _stat_identity(before) != _stat_identity(after):
                raise PortableMigrationError(
                    f"migration source changed while copying: {item.relative_path}"
                )
            if copied != item.size_bytes or digest.hexdigest() != item.sha256:
                raise PortableMigrationError(
                    f"migration source content changed while copying: {item.relative_path}"
                )
            evidence = _windows_file_evidence(temporary_handle, digest.hexdigest())
            if retained_handle is not None:
                retained_handle.append(temporary_handle)
                temporary_handle = None
            return evidence
        except PortableMigrationError:
            if temporary_handle is not None:
                _windows_delete_handle(temporary_handle)
            raise
        except OSError as exc:
            if temporary_handle is not None:
                _windows_delete_handle(temporary_handle)
            raise PortableMigrationError(
                f"cannot copy migration item {item.relative_path}: {exc}"
            ) from exc
        finally:
            if temporary_handle is not None:
                _windows_close_handle(temporary_handle)
    try:
        with item.source.open("rb") as source, temporary.open("xb") as target:
            before = os.fstat(source.fileno())
            if _stat_identity(before) != (
                item.evidence.device,
                item.evidence.inode,
                item.evidence.size_bytes,
                item.evidence.mtime_ns,
                item.evidence.nlink,
                item.evidence.file_attributes,
            ):
                raise PortableMigrationError(f"migration source identity changed: {item.relative_path}")
            if os.name == "nt":
                import msvcrt

                _windows_assert_handle_inside(
                    msvcrt.get_osfhandle(source.fileno()), source_root, "migration source"
                )
                _windows_assert_handle_inside(
                    msvcrt.get_osfhandle(target.fileno()), destination_root, "migration temporary file"
                )
            digest = hashlib.sha256()
            copied = 0
            for block in iter(lambda: source.read(1024 * 1024), b""):
                target.write(block)
                digest.update(block)
                copied += len(block)
            target.flush()
            os.fsync(target.fileno())
            after = os.fstat(source.fileno())
    except FileExistsError as exc:
        raise PortableMigrationError(f"migration temporary target already exists: {temporary.name}") from exc
    except OSError as exc:
        raise PortableMigrationError(f"cannot copy migration item {item.relative_path}: {exc}") from exc
    if _stat_identity(before) != _stat_identity(after):
        raise PortableMigrationError(f"migration source changed while copying: {item.relative_path}")
    if copied != item.size_bytes or digest.hexdigest() != item.sha256:
        raise PortableMigrationError(f"migration source content changed while copying: {item.relative_path}")
    return _capture_file(temporary, "migration temporary file")


def _windows_open_handle(
    path: Path,
    *,
    directory: bool,
    delete: bool,
    share_delete: bool = True,
    child_access: bool = False,
) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create.restype = ctypes.c_void_p
    access = 0x00000080 | (0x00010000 if delete else 0)
    if directory:
        access |= 0x00000001 | (0x00000006 if child_access else 0)
    flags = 0x00200000 | (0x02000000 if directory else 0)
    share = 0x00000001 | 0x00000002 | (0x00000004 if share_delete else 0)
    handle = create(str(path), access, share, None, 3, flags, None)
    invalid = ctypes.c_void_p(-1).value
    if handle in {None, invalid}:
        error = ctypes.get_last_error()
        raise PortableMigrationError(f"cannot open verified Windows path: {path}: error {error}")
    return int(handle)


def _windows_open_relative(
    parent_handle: int,
    name: str,
    *,
    directory: bool,
    create: bool,
    display_path: Path,
) -> int:
    if not name or name in {".", ".."} or "\\" in name or "/" in name:
        raise PortableMigrationError(f"unsafe relative Windows path component: {name!r}")

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ushort),
            ("maximum_length", ctypes.c_ushort),
            ("buffer", ctypes.c_wchar_p),
        ]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("root_directory", ctypes.c_void_p),
            ("object_name", ctypes.POINTER(UnicodeString)),
            ("attributes", ctypes.c_ulong),
            ("security_descriptor", ctypes.c_void_p),
            ("security_quality_of_service", ctypes.c_void_p),
        ]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

    name_buffer = ctypes.create_unicode_buffer(name)
    unicode_name = UnicodeString(
        len(name.encode("utf-16-le")),
        len(name.encode("utf-16-le")) + 2,
        ctypes.cast(name_buffer, ctypes.c_wchar_p),
    )
    attributes = ObjectAttributes(
        ctypes.sizeof(ObjectAttributes),
        ctypes.c_void_p(parent_handle),
        ctypes.pointer(unicode_name),
        0x00000040,
        None,
        None,
    )
    io_status = IoStatusBlock()
    handle = ctypes.c_void_p()
    desired_access = 0x00000080 | 0x00100000
    share_access = 0x00000001 | 0x00000002
    options = 0x00000020 | 0x00200000
    if directory:
        desired_access |= 0x00000007
        options |= 0x00000001
    else:
        desired_access |= 0x00000002 | 0x00010000
        share_access |= 0x00000004
        options |= 0x00000040
    ntdll = ctypes.WinDLL("ntdll")
    nt_create = ntdll.NtCreateFile
    nt_create.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_ulong,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    nt_create.restype = ctypes.c_long
    status = int(
        nt_create(
            ctypes.byref(handle),
            desired_access,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            0x00000080,
            share_access,
            2 if create else 1,
            options,
            None,
            0,
        )
    )
    if status < 0:
        rtl_error = ntdll.RtlNtStatusToDosError
        rtl_error.argtypes = [ctypes.c_long]
        rtl_error.restype = ctypes.c_uint32
        error = int(rtl_error(status))
        if create and error in {5, 80, 183}:
            raise PortableMigrationError(
                f"migration target appeared during relative creation: {display_path}"
            )
        raise PortableMigrationError(
            f"cannot open verified relative Windows path: {display_path}: error {error}"
        )
    if handle.value is None:
        raise PortableMigrationError(
            f"cannot open verified relative Windows path: {display_path}"
        )
    return int(handle.value)


def _windows_close_handle(handle: int) -> None:
    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(ctypes.c_void_p(handle))


def _windows_handle_metadata(handle: int) -> tuple[int, int, int, int, int, int]:
    class HandleInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", ctypes.c_uint32),
            ("creation_time_low", ctypes.c_uint32),
            ("creation_time_high", ctypes.c_uint32),
            ("access_time_low", ctypes.c_uint32),
            ("access_time_high", ctypes.c_uint32),
            ("write_time_low", ctypes.c_uint32),
            ("write_time_high", ctypes.c_uint32),
            ("volume_serial", ctypes.c_uint32),
            ("size_high", ctypes.c_uint32),
            ("size_low", ctypes.c_uint32),
            ("links", ctypes.c_uint32),
            ("index_high", ctypes.c_uint32),
            ("index_low", ctypes.c_uint32),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    information = HandleInformation()
    if not kernel32.GetFileInformationByHandle(
        ctypes.c_void_p(handle), ctypes.byref(information)
    ):
        error = ctypes.get_last_error()
        raise PortableMigrationError(f"cannot inspect verified Windows handle: error {error}")
    file_time = (int(information.write_time_high) << 32) | int(information.write_time_low)
    mtime_ns = (file_time - 116444736000000000) * 100
    return (
        int(information.volume_serial),
        (int(information.index_high) << 32) | int(information.index_low),
        (int(information.size_high) << 32) | int(information.size_low),
        mtime_ns,
        int(information.links),
        int(information.attributes),
    )


def _windows_directory_evidence(handle: int) -> DirectoryEvidence:
    metadata = _windows_handle_metadata(handle)
    return DirectoryEvidence(metadata[0], metadata[1], metadata[4], metadata[5])


def _windows_file_evidence(handle: int, digest: str) -> FileEvidence:
    metadata = _windows_handle_metadata(handle)
    return FileEvidence(*metadata, digest)


def _windows_write_all(handle: int, data: bytes) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    write_file = kernel32.WriteFile
    write_file.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    write_file.restype = ctypes.c_int
    offset = 0
    while offset < len(data):
        block = data[offset:]
        buffer = ctypes.create_string_buffer(block)
        written = ctypes.c_uint32()
        if not write_file(
            ctypes.c_void_p(handle),
            ctypes.byref(buffer),
            len(block),
            ctypes.byref(written),
            None,
        ):
            error = ctypes.get_last_error()
            raise PortableMigrationError(
                f"cannot write migration temporary file: Windows error {error}"
            )
        if written.value == 0:
            raise PortableMigrationError("cannot write migration temporary file: zero-byte write")
        offset += int(written.value)


def _windows_flush_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.FlushFileBuffers(ctypes.c_void_p(handle)):
        error = ctypes.get_last_error()
        raise PortableMigrationError(
            f"cannot flush migration temporary file: Windows error {error}"
        )


def _windows_delete_handle(handle: int) -> None:
    disposition = ctypes.c_ubyte(1)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetFileInformationByHandle(
        ctypes.c_void_p(handle), 4, ctypes.byref(disposition), ctypes.sizeof(disposition)
    )


def _windows_final_path(handle: int) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final = kernel32.GetFinalPathNameByHandleW
    get_final.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    get_final.restype = ctypes.c_uint32
    required = get_final(ctypes.c_void_p(handle), None, 0, 0)
    if not required:
        error = ctypes.get_last_error()
        raise PortableMigrationError(f"cannot resolve verified Windows handle: error {error}")
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = get_final(ctypes.c_void_p(handle), buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        error = ctypes.get_last_error()
        raise PortableMigrationError(f"cannot resolve verified Windows handle: error {error}")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _windows_assert_handle_inside(handle: int, root: Path, label: str) -> None:
    if _windows_handle_metadata(handle)[5] & _REPARSE_POINT:
        raise PortableMigrationError(f"{label} is an unsafe reparse point")
    root_handle = _windows_open_handle(root, directory=True, delete=False)
    try:
        root_final = _windows_final_path(root_handle)
    finally:
        _windows_close_handle(root_handle)
    handle_key = _path_key(_windows_final_path(handle)).rstrip("\\")
    root_key = _path_key(root_final).rstrip("\\")
    if handle_key != root_key and not handle_key.startswith(root_key + "\\"):
        raise PortableMigrationError(f"{label} escapes the portable package root")


def _windows_assert_handle_below_root(handle: int, root_handle: int, label: str) -> None:
    if _windows_handle_metadata(handle)[5] & _REPARSE_POINT:
        raise PortableMigrationError(f"{label} is an unsafe reparse point")
    handle_key = _path_key(_windows_final_path(handle)).rstrip("\\")
    root_key = _path_key(_windows_final_path(root_handle)).rstrip("\\")
    if handle_key != root_key and not handle_key.startswith(root_key + "\\"):
        raise PortableMigrationError(f"{label} escapes the verified destination root")


def _windows_destination_context(plan: ImportPlan) -> WindowsDestinationContext:
    root_snapshot = next(
        (
            snapshot
            for snapshot in plan.new_directories
            if _path_key(snapshot.path) == _path_key(plan.new_root)
        ),
        None,
    )
    if root_snapshot is None or root_snapshot.evidence is None:
        raise PortableMigrationError("planned destination root identity is unavailable")
    root_handle = _windows_open_handle(
        plan.new_root,
        directory=True,
        delete=False,
        share_delete=False,
        child_access=True,
    )
    try:
        if not _windows_directory_handle_matches(root_handle, root_snapshot.evidence):
            raise PortableMigrationError("planned destination root identity changed")
        if _path_key(_windows_final_path(root_handle)) != _path_key(plan.new_root):
            raise PortableMigrationError("planned destination root resolved unexpectedly")
        return WindowsDestinationContext(
            plan.new_root,
            root_handle,
            {_path_key(plan.new_root): root_handle},
        )
    except BaseException:
        _windows_close_handle(root_handle)
        raise


def _windows_file_handle_matches(handle: int, evidence: FileEvidence) -> bool:
    return _windows_handle_metadata(handle) == (
        evidence.device,
        evidence.inode,
        evidence.size_bytes,
        evidence.mtime_ns,
        evidence.nlink,
        evidence.file_attributes,
    )


def _windows_directory_handle_matches(handle: int, evidence: DirectoryEvidence) -> bool:
    metadata = _windows_handle_metadata(handle)
    return (metadata[0], metadata[1], metadata[4], metadata[5]) == (
        evidence.device,
        evidence.inode,
        evidence.nlink,
        evidence.file_attributes,
    )


def _path_stat_matches_file_evidence(path: Path, evidence: FileEvidence) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return _stat_identity(metadata) == (
        evidence.device,
        evidence.inode,
        evidence.size_bytes,
        evidence.mtime_ns,
        evidence.nlink,
        evidence.file_attributes,
    )


def _windows_rename_handle_no_replace(
    file_handle: int, parent_handle: int, destination_name: str, destination: Path
) -> None:
    class RenameHeader(ctypes.Structure):
        _fields_ = [
            ("replace_if_exists", ctypes.c_ubyte),
            ("root_directory", ctypes.c_void_p),
            ("file_name_length", ctypes.c_uint32),
        ]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

    encoded_name = destination_name.encode("utf-16-le")
    name_offset = RenameHeader.file_name_length.offset + ctypes.sizeof(ctypes.c_uint32)
    buffer = ctypes.create_string_buffer(name_offset + len(encoded_name) + 2)
    header = RenameHeader.from_buffer(buffer)
    header.replace_if_exists = 0
    header.root_directory = parent_handle
    header.file_name_length = len(encoded_name)
    ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded_name, len(encoded_name))
    ntdll = ctypes.WinDLL("ntdll")
    set_information = ntdll.NtSetInformationFile
    set_information.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    set_information.restype = ctypes.c_long
    io_status = IoStatusBlock()
    status = int(
        set_information(
            ctypes.c_void_p(file_handle),
            ctypes.byref(io_status),
            ctypes.byref(buffer),
            len(buffer),
            10,
        )
    )
    if status < 0:
        rtl_error = ntdll.RtlNtStatusToDosError
        rtl_error.argtypes = [ctypes.c_long]
        rtl_error.restype = ctypes.c_uint32
        error = int(rtl_error(status))
        if error in {5, 80, 183} and destination.exists():
            raise PortableMigrationError(
                f"migration target appeared after planning: {destination}"
            )
        raise PortableMigrationError(
            f"cannot publish migration target: {destination}: Windows error {error}"
        )


def _publish_no_replace(
    temporary: Path,
    destination: Path,
    *,
    expected_temporary: FileEvidence,
    expected_parent: DirectoryEvidence,
    root: Path,
    temporary_handle: int | None = None,
    parent_handle: int | None = None,
) -> None:
    if os.name == "nt":
        if temporary_handle is None:
            if not _evidence_matches(temporary, expected_temporary, "migration temporary file"):
                raise PortableMigrationError("migration temporary identity changed before publish")
        elif not _path_stat_matches_file_evidence(temporary, expected_temporary):
            raise PortableMigrationError("migration temporary path changed before publish")
        if _capture_directory(destination.parent, "migration destination parent") != expected_parent:
            raise PortableMigrationError("migration destination parent identity changed before publish")
        owns_temporary = temporary_handle is None
        owns_parent = parent_handle is None
        if temporary_handle is None:
            temporary_handle = _windows_open_handle(temporary, directory=False, delete=True)
        if parent_handle is None:
            parent_handle = _windows_open_handle(
                destination.parent,
                directory=True,
                delete=False,
                share_delete=False,
                child_access=True,
            )
        try:
            if not _windows_file_handle_matches(temporary_handle, expected_temporary):
                raise PortableMigrationError("migration temporary identity changed during publish")
            if not _path_stat_matches_file_evidence(temporary, expected_temporary):
                raise PortableMigrationError("migration temporary path changed during publish")
            if not _windows_directory_handle_matches(parent_handle, expected_parent):
                raise PortableMigrationError("migration destination parent identity changed during publish")
            if (
                _capture_directory(destination.parent, "migration destination parent")
                != expected_parent
            ):
                raise PortableMigrationError("migration destination parent identity changed during publish")
            _windows_assert_handle_inside(temporary_handle, root, "migration temporary file")
            _windows_assert_handle_inside(parent_handle, root, "migration destination parent")
            _windows_rename_handle_no_replace(
                temporary_handle, parent_handle, destination.name, destination
            )
        finally:
            if owns_parent:
                _windows_close_handle(parent_handle)
            if owns_temporary:
                _windows_close_handle(temporary_handle)
        return
    try:
        os.link(temporary, destination)
        temporary.unlink()
    except FileExistsError as exc:
        raise PortableMigrationError(f"migration target appeared after planning: {destination}") from exc
    except OSError as exc:
        raise PortableMigrationError(f"cannot publish migration target: {destination}: {exc}") from exc


def _cleanup_owned_temporary(
    temporary: Path,
    expected: FileEvidence | None,
    root: Path,
    retained_handle: int | None = None,
) -> None:
    if os.name == "nt" and retained_handle is not None:
        if (
            expected is not None
            and _windows_file_handle_matches(retained_handle, expected)
            and _path_stat_matches_file_evidence(temporary, expected)
        ):
            _windows_assert_handle_inside(
                retained_handle, root, "migration temporary cleanup candidate"
            )
            _windows_delete_handle(retained_handle)
        return
    if expected is None or not _evidence_matches(
        temporary, expected, "migration temporary cleanup candidate"
    ):
        return
    if os.name == "nt":
        try:
            handle = _windows_open_handle(temporary, directory=False, delete=True)
        except PortableMigrationError:
            return
        try:
            if not _windows_file_handle_matches(handle, expected):
                return
            if not _path_stat_matches_file_evidence(temporary, expected):
                return
            _windows_assert_handle_inside(handle, root, "migration temporary cleanup candidate")
            disposition = ctypes.c_ubyte(1)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if not kernel32.SetFileInformationByHandle(
                ctypes.c_void_p(handle), 4, ctypes.byref(disposition), ctypes.sizeof(disposition)
            ):
                return
        finally:
            _windows_close_handle(handle)
        return
    try:
        temporary.unlink()
    except OSError:
        return


def _copy_without_overwrite(
    item: ImportItem,
    plan: ImportPlan,
    created: dict[str, DirectoryEvidence],
    windows_context: WindowsDestinationContext | None = None,
) -> None:
    _verify_import_state(plan, created)
    _ensure_destination_parent(plan, item.destination, created, windows_context)
    if item.destination.exists():
        raise PortableMigrationError(f"migration target appeared after planning: {item.relative_path}")
    temporary = item.destination.parent / (
        f"{item.destination.name}.tts-more-import-{uuid.uuid4().hex}.tmp"
    )
    expected_temporary: FileEvidence | None = None
    retained_temporary: list[int] = []
    try:
        parent_handle = (
            windows_context.directory_handles.get(_path_key(item.destination.parent))
            if windows_context is not None
            else None
        )
        if windows_context is not None and parent_handle is None:
            raise PortableMigrationError("migration destination parent handle is unavailable")
        expected_temporary = _copy_source_to_temporary(
            item,
            temporary,
            parent_handle=parent_handle,
            retained_handle=retained_temporary,
        )
        _verify_import_state(plan, created)
        if not _evidence_matches(item.source, item.evidence, "migration source"):
            raise PortableMigrationError(f"migration source identity changed: {item.relative_path}")
        if retained_temporary:
            copied = _windows_file_evidence(
                retained_temporary[0], expected_temporary.sha256
            )
            if not _path_stat_matches_file_evidence(temporary, copied):
                raise PortableMigrationError(
                    f"migration temporary path identity changed: {item.relative_path}"
                )
        else:
            copied = _capture_file(temporary, "migration temporary file")
        if copied != expected_temporary:
            raise PortableMigrationError(f"migration temporary identity changed: {item.relative_path}")
        if copied.size_bytes != item.size_bytes or copied.sha256 != item.sha256:
            raise PortableMigrationError(f"migration temporary content changed: {item.relative_path}")
        if item.destination.exists():
            raise PortableMigrationError(f"migration target appeared after planning: {item.relative_path}")
        parent_key = _path_key(item.destination.parent)
        expected_parent = created.get(parent_key)
        if expected_parent is None:
            expected_parent = next(
                (
                    snapshot.evidence
                    for snapshot in plan.new_directories
                    if _path_key(snapshot.path) == parent_key
                ),
                None,
            )
        if expected_parent is None:
            raise PortableMigrationError("migration destination parent was not frozen")
        _publish_no_replace(
            temporary,
            item.destination,
            expected_temporary=copied,
            expected_parent=expected_parent,
            root=plan.new_root,
            temporary_handle=retained_temporary[0] if retained_temporary else None,
            parent_handle=parent_handle,
        )
    finally:
        _cleanup_owned_temporary(
            temporary,
            expected_temporary,
            plan.new_root,
            retained_temporary[0] if retained_temporary else None,
        )
        for handle in retained_temporary:
            _windows_close_handle(handle)


def apply_import(plan: ImportPlan) -> ImportReport:
    """Revalidate and atomically apply a server-generated import plan."""

    try:
        current = plan_import(plan.old_root, plan.new_root)
    except PortableMigrationError as exc:
        raise PortableMigrationError(f"portable migration plan changed or target drifted: {exc}") from exc
    if current.plan_digest != plan.plan_digest:
        raise PortableMigrationError("portable migration plan changed or identity drifted")
    created_directories: dict[str, DirectoryEvidence] = {}
    windows_context = _windows_destination_context(plan) if os.name == "nt" else None
    try:
        _verify_import_state(plan, created_directories)
        copied_user = 0
        for item in plan.user_files:
            _copy_without_overwrite(item, plan, created_directories, windows_context)
            copied_user += 1
        for item in plan.reusable_assets:
            _copy_without_overwrite(item, plan, created_directories, windows_context)
        return ImportReport(
            copied_user_files=copied_user,
            reused_assets=[item.relative_path for item in plan.reusable_assets],
            skipped_assets=list(plan.skipped_assets),
            already_present=list(plan.already_present),
        )
    finally:
        if windows_context is not None:
            windows_context.close()


def _summary(plan: ImportPlan) -> dict[str, object]:
    return {
        "plan_digest": plan.plan_digest,
        "old_root": str(plan.old_root),
        "new_root": str(plan.new_root),
        "user_file_count": len(plan.user_files),
        "user_bytes": sum(item.size_bytes for item in plan.user_files),
        "reusable_assets": [item.relative_path for item in plan.reusable_assets],
        "reusable_asset_bytes": sum(item.size_bytes for item in plan.reusable_assets),
        "skipped_assets": list(plan.skipped_assets),
        "already_present": list(plan.already_present),
        "old_package_preserved": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or apply a portable TTS package data import")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply"):
        command = subparsers.add_parser(name)
        command.add_argument("--old-root", required=True, type=Path)
        command.add_argument("--new-root", required=True, type=Path)
        if name == "apply":
            command.add_argument("--confirmed-digest")
    try:
        args = parser.parse_args(argv)
        plan = plan_import(args.old_root, args.new_root)
        if args.command == "plan":
            print(json.dumps(_summary(plan), ensure_ascii=False, sort_keys=True))
            return 0
        if not args.confirmed_digest or args.confirmed_digest != plan.plan_digest:
            print("portable import requires the exact confirmed plan digest", file=sys.stderr)
            return 2
        report = apply_import(plan)
        print(json.dumps(report.__dict__, ensure_ascii=False, sort_keys=True))
        return 0
    except PortableMigrationError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
