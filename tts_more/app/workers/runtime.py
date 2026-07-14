from __future__ import annotations

import gc
import os
import re
import subprocess
from typing import Any

from app.subprocess_safety import noninteractive_subprocess_kwargs


_DEVICE_UUID_CACHE: dict[int | str, str | None] = {}


def worker_runtime_status(*, loaded: bool, model: Any, device_hint: str | None = None) -> dict[str, Any]:
    device = device_hint or "cpu"
    device_uuid: str | None = None
    cuda_runtime: str | None = None
    memory: dict[str, Any] = {
        "allocated_bytes": 0,
        "reserved_bytes": 0,
        "free_bytes": None,
        "total_bytes": None,
    }
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            index = _logical_cuda_index(device_hint, torch.cuda.current_device())
            device = device_hint or f"cuda:{index}"
            device_uuid = _cuda_device_uuid(index)
            cuda_runtime = str(getattr(getattr(torch, "version", None), "cuda", "") or "") or None
            memory["allocated_bytes"] = int(torch.cuda.memory_allocated(index))
            memory["reserved_bytes"] = int(torch.cuda.memory_reserved(index))
            if hasattr(torch.cuda, "mem_get_info"):
                free_bytes, total_bytes = torch.cuda.mem_get_info(index)
                memory["free_bytes"] = int(free_bytes)
                memory["total_bytes"] = int(total_bytes)
    except (ImportError, RuntimeError, AttributeError):
        pass
    return {
        "device": device,
        "device_uuid": device_uuid,
        "cuda_runtime": cuda_runtime,
        "loaded": loaded,
        "model": model,
        "memory": memory,
    }


def _cuda_device_uuid(index: int) -> str | None:
    visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
    physical_index = index
    uuid_prefix: str | None = None
    if visible_devices is not None:
        devices = [item.strip() for item in visible_devices.split(",") if item.strip()]
        if index >= len(devices):
            return None
        mapped = devices[index]
        if mapped.startswith("GPU-"):
            uuid_prefix = mapped
        elif mapped.startswith("MIG-"):
            return None
        try:
            if uuid_prefix is None:
                physical_index = int(mapped)
        except ValueError:
            return None
    cache_key: int | str = uuid_prefix.casefold() if uuid_prefix else physical_index
    if cache_key in _DEVICE_UUID_CACHE:
        return _DEVICE_UUID_CACHE[cache_key]
    value: str | None = None
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            **noninteractive_subprocess_kwargs(),
        )
        for line in completed.stdout.splitlines():
            raw_index, separator, raw_uuid = line.partition(",")
            if not separator:
                continue
            candidate_uuid = raw_uuid.strip()
            matches_uuid = uuid_prefix is not None and candidate_uuid.casefold().startswith(uuid_prefix.casefold())
            matches_index = uuid_prefix is None and int(raw_index.strip()) == physical_index
            if matches_uuid or matches_index:
                value = candidate_uuid or None
                break
    except (OSError, ValueError, subprocess.SubprocessError):
        value = None
    _DEVICE_UUID_CACHE[cache_key] = value
    return value


def _logical_cuda_index(device_hint: str | None, current_index: int) -> int:
    match = re.fullmatch(r"cuda:(\d+)", str(device_hint or "").casefold())
    return int(match.group(1)) if match else current_index


def release_cuda_memory() -> None:
    gc.collect()
    try:
        import torch  # type: ignore

        if not torch.cuda.is_available():
            return
        torch.cuda.empty_cache()
        if hasattr(torch.cuda, "ipc_collect"):
            torch.cuda.ipc_collect()
    except (ImportError, RuntimeError, AttributeError):
        return
