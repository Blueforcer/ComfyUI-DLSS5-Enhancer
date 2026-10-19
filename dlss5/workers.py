"""Launch the bundled Windows worker without moving Python into Wine.

On Windows the worker is started directly. On Linux it runs through Wine
inside the configured dedicated prefix, with the environment captured by
``scripts/setup_linux.py`` applied to the worker process only, never to
ComfyUI itself.

Adapted from ComfyUI-NVIDIA-DLSS-Frame-Interpolation (MIT), which
originated the worker-only Wine environment approach.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .paths import PACKAGE_ROOT

CONFIG_PATH = PACKAGE_ROOT / "linux-runtime.json"
LINUX_ENV_KEYS = frozenset(
    {
        "WINEPREFIX",
        "DLSS_WINE_PATH",
        "WINESERVER",
        "WINEDEBUG",
        "WINE_HEAP_DELAY_FREE",
        "WINE_HEAP_ZERO_MEMORY",
        "WINEDLLOVERRIDES",
        "NVIDIA_WINE_DLL_DIR",
        "DXVK_ENABLE_NVAPI",
        "DXVK_CONFIG",
        "DXVK_FILTER_DEVICE_NAME",
        "VKD3D_FILTER_DEVICE_NAME",
    }
)


def linux_worker_environment() -> dict[str, str]:
    """Apply the local Wine setup only to workers, never to ComfyUI."""
    env = os.environ.copy()
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return env  # Manual environment setup remains supported.
    except OSError as exc:
        raise RuntimeError(
            f"Cannot read the Linux settings: {CONFIG_PATH}"
        ) from exc
    try:
        config = json.loads(raw)
        if (
            not isinstance(config, dict)
            or config.get("version") != 1
            or set(config) != {"version", "environment"}
        ):
            raise ValueError("expected version 1 and environment")
        settings = config["environment"]
        if not isinstance(settings, dict) or set(settings) != LINUX_ENV_KEYS:
            setting_keys = set(settings) if isinstance(settings, dict) else set()
            missing = sorted(LINUX_ENV_KEYS - setting_keys)
            unexpected = sorted(setting_keys - LINUX_ENV_KEYS)
            detail = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if unexpected:
                detail.append("unexpected " + ", ".join(unexpected))
            raise ValueError(
                "environment must contain the complete helper-generated key set"
                + (" (" + "; ".join(detail) + ")" if detail else "")
            )
        for key, value in settings.items():
            if (
                key not in LINUX_ENV_KEYS
                or not isinstance(value, str)
                or "\0" in value
            ):
                raise ValueError(f"invalid setting: {key}")
        # Saved setup is authoritative: unrelated Wine settings inherited
        # from a desktop or shell must not select a different prefix.
        env.update(settings)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"Invalid Linux settings in {CONFIG_PATH}: {exc}. "
            "Rerun scripts/setup_linux.py or remove the file for manual setup."
        ) from exc
    return env


def validate_binary(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"DLSS runtime file is missing: {path}")
    with path.open("rb") as stream:
        header = stream.read(64)
    if header.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise RuntimeError(
            f"{path.name} is a Git LFS pointer. Run git lfs pull in the "
            "custom-node directory to download the runtime binaries."
        )
    if not header.startswith(b"MZ"):
        raise RuntimeError(f"Not a Windows runtime binary: {path}")


def worker_launch(path: Path, *args: str) -> tuple[list[str], dict]:
    """Build the command and Popen options for one worker process."""
    validate_binary(path)
    command = [str(path.resolve()), *args]
    if sys.platform == "win32":
        return command, {
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
    if sys.platform != "linux":
        raise RuntimeError("DLSS workers support Windows or Linux with Wine.")

    env = linux_worker_environment()
    prefix = env.get("WINEPREFIX", "")
    if not prefix or not Path(prefix).is_absolute():
        raise RuntimeError(
            "Linux DLSS requires WINEPREFIX to point to an existing, "
            "configured Wine prefix using an absolute path. See docs/linux.md."
        )
    if not (Path(prefix) / "drive_c/windows/system32").is_dir():
        raise RuntimeError(
            "WINEPREFIX is not initialized. Set up Wine, DXVK, "
            "VKD3D-Proton and DXVK-NVAPI as described in docs/linux.md."
        )
    configured = env.get("DLSS_WINE_PATH")
    wine = (
        shutil.which(configured)
        if configured
        else (shutil.which("wine64") or shutil.which("wine"))
    )
    if not wine:
        raise RuntimeError(
            "Wine was not found. Install Wine or set DLSS_WINE_PATH to "
            "its executable (a path, not a shell command)."
        )
    # Wine diagnostics belong on stderr, never on the binary protocol stream.
    env.setdefault("WINEDEBUG", "-all")
    return [wine, *command], {"env": env, "start_new_session": True}


class WorkerProcess(subprocess.Popen):
    """Own a render's process group, including children of its Wine loader."""

    def __init__(self, path: Path, *args: str, **kwargs) -> None:
        command, options = worker_launch(path, *args)
        self._owns_group = bool(options.get("start_new_session"))
        super().__init__(command, **options, **kwargs)

    def terminate(self) -> None:
        if self._owns_group:
            self._signal_group(signal.SIGTERM)
        else:
            super().terminate()

    def kill(self) -> None:
        if self._owns_group:
            self._signal_group(signal.SIGKILL)
        else:
            super().kill()

    def _signal_group(self, sig: int) -> None:
        # Never use wineserver -k: it also kills unrelated prefix clients.
        try:
            os.killpg(self.pid, sig)
        except ProcessLookupError:
            pass