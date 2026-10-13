from __future__ import annotations

import sounddevice as sd


def list_input_devices() -> list[dict]:
    devices = sd.query_devices()
    return [
        {"index": i, "name": d["name"], "max_input_channels": d["max_input_channels"]}
        for i, d in enumerate(devices)
        if d["max_input_channels"] > 0
    ]


def default_input_device() -> dict | None:
    try:
        idx = sd.default.device[0]
        if idx is None or idx < 0:
            return None
        d = sd.query_devices(idx)
        return {"index": idx, "name": d["name"], "max_input_channels": d["max_input_channels"]}
    except Exception:
        return None
