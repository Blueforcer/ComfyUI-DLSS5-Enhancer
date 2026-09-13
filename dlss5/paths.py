"""Discovery and validation of the native DLSS 5 runtime.

The runtime binaries are NVIDIA/ReShade/RenoDX components that this repository
neither ships nor redistributes; ``install_runtime.py`` fetches them, or the
user points at an existing DLSS 5 Visual Enhancer installation.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = PACKAGE_ROOT / "config.json"

RUNTIME_ENV = "DLSS5_RUNTIME_DIR"
FFMPEG_ENV = "DLSS5_FFMPEG_DIR"

# The worker keeps NVIDIA's image name because the signed-snippet caller
# contract checks it; it is an executable, not a library.
WORKER_NAME = "nvngx.dll"

REQUIRED_RUNTIME_FILES = (
    WORKER_NAME,
    "dxgi.dll",
    "renodx-dlss5.addon64",
    "nvngx_dlss.dll",
    "nvngx_dlssnr.dll",
)

SETUP_HINT = """Install it with:
    python_embeded/python.exe ComfyUI/custom_nodes/ComfyUI-DLSS5-Enhancer/install_runtime.py
or point the node at an existing DLSS 5 Visual Enhancer install:
    python ... install_runtime.py --runtime-dir <path>/bin/runtime
or set the DLSS5_RUNTIME_DIR environment variable."""


class RuntimeMissing(RuntimeError):
    """Raised when the native runtime is absent or incomplete."""


@dataclass(frozen=True, slots=True)
class RuntimeLayout:
    """Resolved locations of every binary a render session needs.

    ffmpeg is resolved on first use: the image node never needs it, and failing
    its lookup there would block a perfectly usable install.
    """

    root: Path
    # Excluded from equality so the layout stays hashable and comparable.
    config: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def ffmpeg(self) -> Path:
        return _resolve_ffmpeg(self.config, self.root)[0]

    @property
    def ffprobe(self) -> Path:
        return _resolve_ffmpeg(self.config, self.root)[1]

    @property
    def worker(self) -> Path:
        return self.root / WORKER_NAME

    @property
    def addon(self) -> Path:
        return self.root / "renodx-dlss5.addon64"

    @property
    def neural_runtime(self) -> Path:
        return self.root / "nvngx_dlssnr.dll"

    @property
    def reshade_log(self) -> Path:
        return self.root / "ReShade.log"

    def require_ffmpeg(self) -> "RuntimeLayout":
        """Resolve ffmpeg now, so a missing binary fails before a long render."""
        _resolve_ffmpeg(self.config, self.root)
        return self

    def validate(self) -> "RuntimeLayout":
        missing = [
            str(self.root / name)
            for name in REQUIRED_RUNTIME_FILES
            if not (self.root / name).is_file()
        ]
        if missing:
            raise RuntimeMissing(
                "The DLSS 5 runtime in "
                f"{self.root} is incomplete. Missing:\n  "
                + "\n  ".join(missing)
                + "\n\n"
                + SETUP_HINT
            )
        ensure_worker_patched(self.worker)
        return self


def ensure_worker_patched(worker_path: Path) -> bool:
    """Ensure the native worker supports GPU/adapter selection via DLSS5_ADAPTER.

    The upstream worker hardcodes choosing the first NVIDIA adapter found.
    We patch the adapter enumeration check in a small code cave to read
    the DLSS5_ADAPTER environment variable. If DLSS5_ADAPTER is not set,
    it behaves identically to the original unpatched binary.
    """
    if not worker_path.is_file():
        return False
    try:
        data = bytearray(worker_path.read_bytes())
    except OSError:
        return False

    cave_raw = 0xC570
    hook_raw = 0x2799
    if data[cave_raw : cave_raw + 14] == b"DLSS5_ADAPTER\x00":
        return True

    original_signature = bytes.fromhex("81bd00010000de1000007509f68530010000027442")
    if data[hook_raw : hook_raw + len(original_signature)] != original_signature:
        return False

    cave_rva = 0xD170
    hook_rva = 0x3399
    str_bytes = b"DLSS5_ADAPTER\x00\x00"
    code_start_rva = cave_rva + len(str_bytes)
    getenv_iat_rva = 0xE348
    next_adapter_rva = 0x33AE
    found_nvidia_rva = 0x33F0

    code = bytearray()
    code += bytes.fromhex("81bd00010000de100000")
    code += bytes.fromhex("7509")
    code += bytes.fromhex("f6853001000002")
    code += bytes.fromhex("7502")
    code += bytes.fromhex("eb05")
    disp_next = next_adapter_rva - (code_start_rva + len(code) + 5)
    code += b"\xe9" + int.to_bytes(disp_next & 0xFFFFFFFF, 4, "little", signed=False)

    code += bytes.fromhex("515241504151")
    code += bytes.fromhex("4883ec28")

    curr_rva = code_start_rva + len(code)
    disp_str = cave_rva - (curr_rva + 7)
    code += bytes.fromhex("488d0d") + int.to_bytes(disp_str & 0xFFFFFFFF, 4, "little", signed=False)

    curr_rva = code_start_rva + len(code)
    disp_iat = getenv_iat_rva - (curr_rva + 6)
    code += bytes.fromhex("ff15") + int.to_bytes(disp_iat & 0xFFFFFFFF, 4, "little", signed=False)

    code += bytes.fromhex("4883c428")
    code += bytes.fromhex("415941585a59")

    code += bytes.fromhex("4885c0")
    match_label_offset = len(code)
    code += bytes.fromhex("7400")

    code += bytes.fromhex("31d2")
    parse_loop_offset = len(code)
    code += bytes.fromhex("0fb608")
    code += bytes.fromhex("48ffc0")
    code += bytes.fromhex("83e930")
    code += bytes.fromhex("83f909")
    ja_done_offset = len(code)
    code += bytes.fromhex("7700")
    code += bytes.fromhex("6bd20a")
    code += bytes.fromhex("01ca")
    jmp_loop_disp = parse_loop_offset - (len(code) + 2)
    code += bytes.fromhex("eb") + int.to_bytes(jmp_loop_disp & 0xFF, 1, "little", signed=False)

    done_parse_offset = len(code)
    code[ja_done_offset + 1] = done_parse_offset - (ja_done_offset + 2)

    code += bytes.fromhex("39d3")
    je_match_offset = len(code)
    code += bytes.fromhex("7400")

    disp_next2 = next_adapter_rva - (code_start_rva + len(code) + 5)
    code += b"\xe9" + int.to_bytes(disp_next2 & 0xFFFFFFFF, 4, "little", signed=False)

    match_found_offset = len(code)
    code[match_label_offset + 1] = match_found_offset - (match_label_offset + 2)
    code[je_match_offset + 1] = match_found_offset - (je_match_offset + 2)

    disp_found = found_nvidia_rva - (code_start_rva + len(code) + 5)
    code += b"\xe9" + int.to_bytes(disp_found & 0xFFFFFFFF, 4, "little", signed=False)

    data[cave_raw : cave_raw + len(str_bytes)] = str_bytes
    data[cave_raw + len(str_bytes) : cave_raw + len(str_bytes) + len(code)] = code

    disp_hook = code_start_rva - (hook_rva + 5)
    hook_bytes = b"\xe9" + int.to_bytes(disp_hook & 0xFFFFFFFF, 4, "little", signed=False) + b"\x90" * 16
    data[hook_raw : hook_raw + len(hook_bytes)] = hook_bytes

    try:
        bak = worker_path.with_suffix(".dll.bak")
        if not bak.is_file():
            bak.write_bytes(worker_path.read_bytes())
        worker_path.write_bytes(data)
        return True
    except OSError:
        return False


def _read_config() -> dict:
    if not CONFIG_FILE.is_file():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeMissing(f"{CONFIG_FILE} is not readable JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


def write_config(runtime_dir: Path, ffmpeg_dir: Path | None = None) -> Path:
    """Persist the chosen runtime location for later ComfyUI sessions."""
    payload = {"runtime_dir": str(Path(runtime_dir).resolve())}
    if ffmpeg_dir is not None:
        payload["ffmpeg_dir"] = str(Path(ffmpeg_dir).resolve())
    CONFIG_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return CONFIG_FILE


def _runtime_candidates(config: dict) -> list[Path]:
    candidates: list[Path] = []
    env_value = os.environ.get(RUNTIME_ENV)
    if env_value:
        candidates.append(Path(env_value))
    configured = config.get("runtime_dir")
    if configured:
        candidates.append(Path(configured))
    candidates.append(PACKAGE_ROOT / "runtime")
    return candidates


def _ffmpeg_candidates(config: dict, runtime_root: Path) -> list[Path]:
    candidates: list[Path] = []
    env_value = os.environ.get(FFMPEG_ENV)
    if env_value:
        candidates.append(Path(env_value))
    configured = config.get("ffmpeg_dir")
    if configured:
        candidates.append(Path(configured))
    candidates.append(PACKAGE_ROOT / "ffmpeg" / "bin")
    # Layout of a DLSS 5 Visual Enhancer install: bin/runtime and bin/ffmpeg/bin.
    candidates.append(runtime_root.parent / "ffmpeg" / "bin")
    return candidates


def _resolve_ffmpeg(config: dict, runtime_root: Path) -> tuple[Path, Path]:
    for directory in _ffmpeg_candidates(config, runtime_root):
        ffmpeg = directory / "ffmpeg.exe"
        ffprobe = directory / "ffprobe.exe"
        if ffmpeg.is_file() and ffprobe.is_file():
            return ffmpeg, ffprobe
    on_path = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if all(on_path):
        return Path(on_path[0]), Path(on_path[1])
    raise RuntimeMissing(
        "ffmpeg/ffprobe were not found. They ship with the DLSS 5 Visual Enhancer "
        f"download, or set {FFMPEG_ENV} to a directory containing them, or put "
        "them on PATH."
    )


def find_runtime(override: str | os.PathLike[str] | None = None) -> RuntimeLayout:
    """Locate the runtime; ``override`` wins, then env var, config, bundled dir."""
    config = _read_config()
    candidates = [Path(override)] if override else _runtime_candidates(config)

    for candidate in candidates:
        root = candidate.expanduser()
        if (root / WORKER_NAME).is_file():
            return RuntimeLayout(root=root.resolve(), config=config).validate()

    searched = "\n  ".join(str(path) for path in candidates)
    raise RuntimeMissing(
        "No DLSS 5 runtime was found. Searched:\n  " + searched + "\n\n" + SETUP_HINT
    )
