"""GPU/runtime compatibility checks and native-worker failure analysis."""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, BinaryIO

from .paths import RuntimeLayout

# The only community Ampere pair the upstream project verified as working.
EXPECTED_AMPERE_ADDON_SHA256 = "D5ADF82EB44B065F4C590AC91FE824BAB07AFEA0EB9F994BDE936710C8593952"
EXPECTED_AMPERE_NEURAL_SHA256 = "6EB209E764F39872625DEBD6ABAF45E2BB6322F6F270F781F70C059AE30B3927"

FEATURE_18_MARKERS = (
    "signed DLSSNR 310.8.0 D3D12 runtime initialized",
    "feature 18 created via the signed snippet",
    "inline feature 18 evaluation succeeded",
)

_INTERESTING = ("error", "exception", "failed", "dlssnr", "feature 18")


@dataclass(slots=True)
class LogRing:
    """Bounded log buffer that always keeps the most diagnostic lines."""

    lines: deque[str] = field(default_factory=lambda: deque(maxlen=500))
    important: deque[str] = field(default_factory=lambda: deque(maxlen=120))
    dropped: int = 0

    def add(self, line: str) -> None:
        text = line.rstrip()
        if not text:
            return
        if len(self.lines) == self.lines.maxlen:
            self.dropped += 1
        self.lines.append(text)
        lowered = text.lower()
        if any(token in lowered for token in _INTERESTING):
            self.important.append(text)

    def snapshot(self) -> list[str]:
        merged = list(self.lines)
        for line in self.important:
            if line not in merged:
                merged.append(line)
        return merged


def drain(stream: BinaryIO, ring: LogRing) -> None:
    """Consume a worker pipe until EOF so it can never block on a full buffer."""
    try:
        for raw in iter(stream.readline, b""):
            ring.add(raw.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


@lru_cache(maxsize=1)
def detect_gpu() -> dict[str, Any]:
    """Identify the RTX GPU and reject generations DLSS 5 NR does not support."""
    command = [
        "nvidia-smi",
        "--query-gpu=name,driver_version,memory.total,compute_cap",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "nvidia-smi is unavailable; DLSS 5 needs an NVIDIA RTX GPU with a current driver."
        ) from exc

    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4 or "RTX" not in parts[0].upper():
            continue
        name, driver, memory, capability = parts[:4]
        match = re.search(r"RTX\s+(\d{2})", name.upper())
        generation = int(match.group(1)) if match else 0
        if generation < 30:
            raise RuntimeError(f"{name} is outside the supported RTX 30/40/50 scope.")
        return {
            "name": name,
            "driver": driver,
            "memory_mb": int(memory),
            "compute_capability": capability,
            "generation": generation,
            "beta": generation == 30,
        }
    raise RuntimeError("No supported NVIDIA RTX GPU was detected.")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def inspect_bundle(layout: RuntimeLayout) -> dict[str, Any]:
    """Fingerprint the neural components so failures can name what is installed."""
    addon = file_sha256(layout.addon)
    neural = file_sha256(layout.neural_runtime)
    return {
        "addon_sha256": addon,
        "neural_sha256": neural,
        "known_ampere_pair": (
            addon == EXPECTED_AMPERE_ADDON_SHA256 and neural == EXPECTED_AMPERE_NEURAL_SHA256
        ),
    }


def ensure_supported(layout: RuntimeLayout) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate GPU and runtime pairing before a session is started."""
    gpu = detect_gpu()
    bundle = inspect_bundle(layout)
    if gpu["generation"] == 30 and not bundle["known_ampere_pair"]:
        raise RuntimeError(
            f"{gpu['name']} needs the tested experimental Ampere pair "
            "(RenoDX DLSS5 v4.70 + DLSS NR 310.8.SF-v2). Installed hashes are "
            f"add-on {bundle['addon_sha256']} and neural runtime {bundle['neural_sha256']}."
        )
    return gpu, bundle


def relevant_lines(reshade_log: str, limit: int = 300) -> list[str]:
    """Filter a ReShade log down to neural-rendering evidence."""
    lines = reshade_log.splitlines()
    picked = [
        line
        for line in lines
        if "DLSS 5 Neural Rendering" in line
        or "DLSSNR" in line
        or "feature 18" in line
        or "exception" in line.lower()
        or "failed" in line.lower()
    ]
    return (picked or lines)[-limit:]


def verify_feature_18(reshade_log: str) -> dict[str, Any]:
    """Confirm that signed feature-18 execution actually happened.

    Without this check a render can silently complete with plain upscaling, which
    looks like a working node but applies no neural rendering at all.
    """
    missing = [marker for marker in FEATURE_18_MARKERS if marker not in reshade_log]
    if missing:
        evidence = "\n".join(relevant_lines(reshade_log, limit=40))
        raise RuntimeError(
            "The frames were rendered, but signed DLSSNR feature-18 execution was not "
            "verified. Missing evidence:\n  "
            + "\n  ".join(missing)
            + ("\n\n" + evidence if evidence else "")
        )
    return {
        "verified": True,
        "native_fallback": "NR upscaling fell back to native" in reshade_log,
        "evidence": [
            line
            for line in reshade_log.splitlines()
            if "signed DLSSNR" in line
            or "feature 18 created" in line
            or "feature 18 evaluation succeeded" in line
            or "NR upscaling fell back" in line
        ][-20:],
    }


def describe_worker_failure(
    *,
    exit_code: int | None,
    frame_index: int,
    worker_logs: list[str],
    reshade_lines: list[str],
    gpu: dict[str, Any],
    bundle: dict[str, Any],
) -> str:
    """Turn a dead worker into a message that says what to change."""
    evidence = "\n".join([*worker_logs, *reshade_lines])
    access_violation = "0xC0000005" in evidence or (
        exit_code is not None and (exit_code & 0xFFFFFFFF) == 0xC0000005
    )
    headline = (
        "The native DLSS worker crashed inside feature-18 evaluation "
        f"(access violation) on frame {frame_index}."
        if access_violation
        else f"The native DLSS worker stopped on frame {frame_index} (exit {exit_code})."
    )
    hint = ""
    if access_violation and gpu.get("generation") == 30:
        hint = (
            "\nRTX 30 is the experimental path; the RenoDX/DLSS-NR pair must match "
            "exactly and may still be unstable."
        )
    elif access_violation:
        hint = (
            "\nUpdate the NVIDIA driver and make sure the runtime files come from a "
            "matching RenoDX DLSS5 / DLSS NR release."
        )
    return (
        f"{headline}{hint}\n"
        f"GPU: {gpu.get('name', 'unknown')} (driver {gpu.get('driver', 'unknown')})\n"
        f"Add-on: {bundle.get('addon_sha256', 'unknown')}\n"
        f"Neural runtime: {bundle.get('neural_sha256', 'unknown')}\n\n"
        + "\n".join([*worker_logs[-40:], *reshade_lines[-40:]])
    )
