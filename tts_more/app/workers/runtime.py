from __future__ import annotations

import gc
import os
import re
from typing import Any

_DEVICE_UUID_CACHE: dict[tuple[str, int], str | None] = {}


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
            device_uuid = _cuda_device_uuid(index, torch.cuda)
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


def _cuda_device_uuid(index: int, cuda: Any) -> str | None:
    visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
    devices = [item.strip() for item in visible_devices.split(",") if item.strip()] if visible_devices is not None else []
    if visible_devices is not None and index >= len(devices):
        return None
    cache_key = ((visible_devices or "").casefold(), index)
    if cache_key in _DEVICE_UUID_CACHE:
        return _DEVICE_UUID_CACHE[cache_key]
    value: str | None = None
    try:
        raw_uuid = getattr(cuda.get_device_properties(index), "uuid", None)
        value = str(raw_uuid).strip() or None if raw_uuid is not None else None
    except (RuntimeError, AttributeError, TypeError, ValueError):
        pass
    if value is None and devices:
        visible_identifier = devices[index]
        if visible_identifier.casefold().startswith("gpu-"):
            value = visible_identifier
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
