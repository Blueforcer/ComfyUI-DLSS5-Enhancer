"""Discovery and validation of the native DLSS 5 runtime.

The runtime binaries are NVIDIA/ReShade/RenoDX components that this repository
neither ships nor redistributes; ``install_runtime.py`` fetches them, or the
user points at an existing DLSS 5 Visual Enhancer installation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = PACKAGE_ROOT / "config.json"

RUNTIME_ENV = "DLSS5_RUNTIME_DIR"
FFMPEG_ENV = "DLSS5_FFMPEG_DIR"
CARRIER_ENV = "DLSS5_LINUX_CARRIER"

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

# Wine loads the carrier beside the executable, and the executable must be a
# regular file so its own directory is a valid DLL search path. The Linux
# staging directory therefore holds a real worker copy, the Linux ReShade
# carrier and symlinks to everything the worker shares with the runtime.
LINUX_STAGING_NAME = "runtime-linux"
LINUX_STAGED_FILES = (
    WORKER_NAME,
    "dxgi.dll",
    "renodx-dlss5.addon64",
    "nvngx_dlss.dll",
    "nvngx_dlssnr.dll",
    "_nvngx.dll",
)

# The upstream ReShade carrier faults inside a CUDA descriptor lookup under
# VKD3D-Proton because it re-translates descriptors allocated on the
# unwrapped device. ComfyUI-NVIDIA-DLSS-Frame-Interpolation distributes a
# patched ReShade 6.8.0.1 build (patches/ in that repository); verify that
# exact build when it is resolved automatically.
EXPECTED_LINUX_CARRIER_SHA256 = (
    "596e4a61b96540683fdb92f80c72c96637247276a34615c3c26a35ba78725e80"
)
SIBLING_CARRIER = (
    PACKAGE_ROOT.parent
    / "ComfyUI-NVIDIA-DLSS-Frame-Interpolation"
    / "bin"
    / "runtime"
    / "host-linux"
    / "dxgi.dll"
)

IS_LINUX = sys.platform == "linux"
LINUX_STAGING_ROOT = PACKAGE_ROOT / LINUX_STAGING_NAME

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
        if IS_LINUX:
            return LINUX_STAGING_ROOT / WORKER_NAME
        return self.root / WORKER_NAME

    @property
    def session_root(self) -> Path:
        """Directory the worker must run in for the carrier to load."""
        if IS_LINUX:
            return LINUX_STAGING_ROOT
        return self.root

    @property
    def addon(self) -> Path:
        return self.root / "renodx-dlss5.addon64"

    @property
    def neural_runtime(self) -> Path:
        return self.root / "nvngx_dlssnr.dll"

    @property
    def reshade_log(self) -> Path:
        if IS_LINUX:
            return LINUX_STAGING_ROOT / "ReShade.log"
        return self.root / "ReShade.log"

    def require_ffmpeg(self) -> "RuntimeLayout":
        """Resolve ffmpeg now, so a missing binary fails before a long render."""
        _resolve_ffmpeg(self.config, self.root)
        return self

    def require_linux_staging(self) -> "RuntimeLayout":
        """Prepare and verify the Wine staging directory before a render.

        The runtime itself may be complete while the Linux staging pieces
        (patched carrier, driver bridge) are still missing, so this check is
        separate from :meth:`validate` and runs at session start, where the
        message can tell the user what still has to be configured.
        """
        if IS_LINUX:
            prepare_linux_runtime(self)
            missing = [
                str(LINUX_STAGING_ROOT / name)
                for name in LINUX_STAGED_FILES
                if not (LINUX_STAGING_ROOT / name).exists()
            ]
            if missing:
                raise RuntimeMissing(
                    "The Linux DLSS 5 staging directory in "
                    f"{LINUX_STAGING_ROOT} is incomplete. Missing:\n  "
                    + "\n  ".join(missing)
                    + "\n\nRun scripts/setup_linux.py in the node folder "
                    "(it installs the patched ReShade carrier and links the "
                    "NVIDIA driver bridge), then run the node again. See "
                    "docs/linux.md."
                )
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
        if IS_LINUX:
            prepare_linux_runtime(self)
        return self


def _read_config() -> dict:
    if not CONFIG_FILE.is_file():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeMissing(f"{CONFIG_FILE} is not readable JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


def write_config(
    runtime_dir: Path,
    ffmpeg_dir: Path | None = None,
    linux_carrier: Path | None = None,
) -> Path:
    """Persist the chosen runtime location for later ComfyUI sessions."""
    payload = {"runtime_dir": str(Path(runtime_dir).resolve())}
    if ffmpeg_dir is not None:
        payload["ffmpeg_dir"] = str(Path(ffmpeg_dir).resolve())
    if linux_carrier is not None:
        payload["linux_carrier"] = str(Path(linux_carrier).resolve())
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
    names = ("ffmpeg", "ffprobe") if IS_LINUX else ("ffmpeg.exe", "ffprobe.exe")
    for directory in _ffmpeg_candidates(config, runtime_root):
        ffmpeg = directory / names[0]
        ffprobe = directory / names[1]
        if ffmpeg.is_file() and ffprobe.is_file():
            return ffmpeg, ffprobe
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return Path(ffmpeg), Path(ffprobe)
    raise RuntimeMissing(
        "ffmpeg/ffprobe were not found. They ship with the DLSS 5 Visual Enhancer "
        f"download, or set {FFMPEG_ENV} to a directory containing them, or put "
        "them on PATH."
    )


def _replace_file(source: Path, target: Path) -> None:
    """Atomically replace ``target`` with the contents of ``source``."""
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_link(source: Path, target: Path) -> None:
    """Atomically point ``target`` at ``source`` without following old links."""
    if target.is_symlink() and target.readlink() == source:
        return
    with tempfile.TemporaryDirectory(dir=target.parent) as directory:
        link = Path(directory) / target.name
        link.symlink_to(source)
        os.replace(link, target)


def _carrier_candidates(config: dict) -> list[Path]:
    """Where a patched Linux ReShade carrier may come from."""
    candidates: list[Path] = []
    env_value = os.environ.get(CARRIER_ENV)
    if env_value:
        candidates.append(Path(env_value))
    configured = config.get("linux_carrier")
    if configured:
        candidates.append(Path(configured))
    if SIBLING_CARRIER.is_file():
        candidates.append(SIBLING_CARRIER)
    return candidates


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_linux_runtime(layout: RuntimeLayout) -> None:
    """Refresh the Wine-side staging directory without touching the runtime.

    The worker executable must be a regular file beside the carrier so Wine
    loads the patched ReShade build that tolerates VKD3D-Proton descriptors.
    Everything else stays a link so runtime updates cannot leave stale
    copies. The patched carrier and the driver bridge ``_nvngx.dll`` are
    resolved when configured and left for :meth:`require_linux_staging` to
    report when they are not; the only hard failure here is an auto-detected
    carrier that does not match the build this pack documents.
    """
    if not IS_LINUX:
        return
    LINUX_STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    # The worker: a real copy, replaced atomically.
    _replace_file(layout.root / WORKER_NAME, LINUX_STAGING_ROOT / WORKER_NAME)
    # The patched ReShade carrier: a real copy from the configured source.
    carrier = LINUX_STAGING_ROOT / "dxgi.dll"
    for source in _carrier_candidates(layout.config):
        if not source.is_file():
            continue
        if (
            source == SIBLING_CARRIER
            and _file_sha256(source) != EXPECTED_LINUX_CARRIER_SHA256
        ):
            raise RuntimeMissing(
                f"The sibling pack carrier {source} does not match the patched "
                "ReShade 6.8.0.1 build this pack expects "
                f"({EXPECTED_LINUX_CARRIER_SHA256}). Rebuild or update it as "
                "documented in docs/linux.md."
            )
        _replace_file(source, carrier)
        break
    # Shared components stay links; updates cannot leave stale NVIDIA copies.
    for name in ("renodx-dlss5.addon64", "nvngx_dlss.dll", "nvngx_dlssnr.dll"):
        _replace_link(layout.root / name, LINUX_STAGING_ROOT / name)
    # The driver bridge is linked by setup; link from the runtime when the
    # user registered an install that already carries one.
    bridge = LINUX_STAGING_ROOT / "_nvngx.dll"
    if not bridge.exists() and not bridge.is_symlink():
        if (layout.root / "_nvngx.dll").exists():
            _replace_link(layout.root / "_nvngx.dll", bridge)


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
