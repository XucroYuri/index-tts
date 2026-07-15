from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Iterator
from uuid import UUID

if os.name == "nt":
    import msvcrt
else:
    import fcntl


PHASES = {
    "not_initialized",
    "checking",
    "downloading",
    "installing",
    "validating",
    "starting",
    "ready",
    "stopped",
    "repairable",
    "blocked",
}
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.05


def create_operation(root: Path, operation_id: str, component: str, action: str, initiator: str) -> dict[str, object]:
    directory = _operation_dir(root, operation_id)
    directory.mkdir(parents=True, exist_ok=True)
    operation_path = directory / "operation.json"
    with _operation_lock(directory):
        if operation_path.exists():
            raise FileExistsError(f"operation already exists: {directory.name}")
        operation: dict[str, object] = {
            "operation_id": directory.name,
            "component": component,
            "action": action,
            "initiator": initiator,
            "started_at": _timestamp(),
            "status": "not_initialized",
            "exit_code": None,
        }
        _write_json_atomic(operation_path, operation)
    return operation


def append_event(
    root: Path,
    operation_id: str,
    phase: str,
    message: str,
    *,
    percent: float | None = None,
    error_code: str | None = None,
) -> dict[str, object]:
    if phase not in PHASES:
        raise ValueError(f"unsupported operation phase: {phase}")
    directory = _operation_dir(root, operation_id)
    events_path = directory / "events.jsonl"
    with _operation_lock(directory):
        if not (directory / "operation.json").is_file():
            raise FileNotFoundError(f"operation does not exist: {directory.name}")
        seq = len(_read_events(events_path, ignore_malformed_final=False)) + 1
        event: dict[str, object] = {
            "seq": seq,
            "timestamp": _timestamp(),
            "phase": phase,
            "message": message,
        }
        if percent is not None:
            event["percent"] = max(0.0, min(100.0, float(percent)))
        if error_code:
            event["error_code"] = error_code
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with events_path.open("ab") as handle:
            written = handle.write(line)
            if written != len(line):
                raise OSError(f"incomplete operation event write: {written} of {len(line)} bytes")
            handle.flush()
            os.fsync(handle.fileno())
    return event


def finish_operation(root: Path, operation_id: str, status: str, exit_code: int) -> dict[str, object]:
    if status not in PHASES:
        raise ValueError(f"unsupported operation status: {status}")
    directory = _operation_dir(root, operation_id)
    operation_path = directory / "operation.json"
    operation = json.loads(operation_path.read_text(encoding="utf-8"))
    operation["status"] = status
    operation["exit_code"] = int(exit_code)
    operation["finished_at"] = _timestamp()
    _write_json_atomic(operation_path, operation)
    return operation


def read_operation(root: Path, operation_id: str) -> tuple[dict[str, object], list[dict[str, object]]]:
    directory = _operation_dir(root, operation_id)
    operation = json.loads((directory / "operation.json").read_text(encoding="utf-8"))
    events = _read_events(directory / "events.jsonl", ignore_malformed_final=True)
    return operation, events


def _operation_dir(root: Path, operation_id: str) -> Path:
    canonical_id = _canonical_operation_id(operation_id)
    operations_root = Path(root).resolve()
    directory = (operations_root / canonical_id).resolve()
    try:
        directory.relative_to(operations_root)
    except ValueError as error:
        raise ValueError(f"operation directory escapes operations root: {operation_id}") from error
    return directory


def _canonical_operation_id(operation_id: str) -> str:
    try:
        parsed = UUID(operation_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"operation_id must be a valid UUID: {operation_id}") from error
    return str(parsed)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def _operation_lock(directory: Path, *, timeout: float | None = None) -> Iterator[None]:
    wait_seconds = LOCK_TIMEOUT_SECONDS if timeout is None else float(timeout)
    if wait_seconds < 0:
        raise ValueError("operation lock timeout must be non-negative")
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".operation.lock"
    with lock_path.open("a+b", buffering=0) as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                _acquire_os_lock(handle)
                break
            except OSError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out acquiring operation lock: {directory.name}") from error
                time.sleep(min(LOCK_POLL_SECONDS, remaining))
        try:
            yield
        finally:
            _release_os_lock(handle)


def _acquire_os_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_events(events_path: Path, *, ignore_malformed_final: bool) -> list[dict[str, object]]:
    if not events_path.exists():
        return []
    lines = [line for line in events_path.read_bytes().splitlines() if line.strip()]
    events: list[dict[str, object]] = []
    for index, line in enumerate(lines):
        try:
            events.append(json.loads(line))
        except (UnicodeDecodeError, json.JSONDecodeError):
            if ignore_malformed_final and index == len(lines) - 1:
                break
            raise
    return events


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
