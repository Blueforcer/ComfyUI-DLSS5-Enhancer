"""Fetch or register the native DLSS 5 runtime this node pack drives.

The runtime consists of NVIDIA, ReShade and RenoDX binaries that are not part of
this repository and are not redistributed by it.  This script downloads the
official DLSS 5 Visual Enhancer release and keeps only the runtime and ffmpeg
components, or simply records the path of an existing installation.

Usage:
    python install_runtime.py                      # download and install
    python install_runtime.py --runtime-dir <path> # use an existing install
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dlss5.paths import (  # noqa: E402  (path bootstrap must run first)
    PACKAGE_ROOT,
    REQUIRED_RUNTIME_FILES,
    RuntimeLayout,
    write_config,
)
from dlss5.runtime_archive import (  # noqa: E402
    ArchiveLayoutError,
    NR_ARCHES,
    select_runtime_members,
    selected_nr_arch,
)

# v6.0 is the newest release that still ships the standalone protocol-4
# worker (bin/runtime/host/nvngx.dll) and loads renodx-dlss5.addon64 from
# beside it. v7.0 moved the add-on to dlssnr/ and v8.0 replaced the worker
# with the Neuroframe Engine; see dlss5/runtime_archive.py.
RELEASE_URL = (
    "https://github.com/Merserk/dlss5-visual-enhancer/releases/download/v6.0/"
    "DLSS.5.Visual.Enhancer.v6.0.zip"
)

RUNTIME_TARGET = PACKAGE_ROOT / "runtime"
FFMPEG_TARGET = PACKAGE_ROOT / "ffmpeg" / "bin"

NOTICE = """
This downloads the DLSS 5 Visual Enhancer release v6.0 (about 856 MB) and
extracts only its runtime components:

  nvngx.dll             standalone D3D12 worker built by the upstream project, under
                        its own terms (an executable, not NVIDIA's NGX core)
  nvngx_dlss.dll        DLSS Super Resolution runtime      - NVIDIA proprietary terms
  nvngx_dlssnr.dll      DLSS Neural Rendering runtime      - NVIDIA proprietary terms
  dxgi.dll              ReShade carrier                    - BSD-3-Clause
  renodx-dlss5.addon64  RenoDX DLSS 5 add-on               - its own distribution terms
  ffmpeg.exe/ffprobe.exe

On Windows the installer also extracts ffmpeg. On Linux it extracts the
runtime only (about 240 MB) and uses the system ffmpeg.

v6.0 is the newest release this pack can drive: v7.0 moved the add-on into
dlssnr/ and v8.0 replaced the standalone worker with the Neuroframe Engine.

These files are neither owned nor redistributed by this node pack. Install only
components you are authorised to use, from sources their licences permit. This
project is not affiliated with NVIDIA, ReShade, RenoDX or the upstream project.

On Linux the runtime binaries are the same Windows files and run through
Wine. Afterwards run scripts/setup_linux.py once to configure the Wine prefix,
the driver bridge and the patched ReShade carrier. See docs/linux.md.
"""

IS_LINUX = sys.platform == "linux"


def _download(url: str, target: Path) -> Path:
    print(f"Downloading {url}")
    with urllib.request.urlopen(url) as response, target.open("wb") as sink:
        total = int(response.headers.get("Content-Length") or 0)
        received = 0
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            sink.write(block)
            received += len(block)
            if total:
                percent = received * 100 // total
                print(f"\r  {percent:3d}%  {received / 1e6:8.1f} / {total / 1e6:.1f} MB", end="")
            else:
                print(f"\r  {received / 1e6:8.1f} MB", end="")
    print()
    if total and received != total:
        raise RuntimeError(
            f"The download stopped after {received} of {total} bytes. Run the script again."
        )
    return target


def _members(archive: zipfile.ZipFile, marker: str) -> list[zipfile.ZipInfo]:
    return [
        info
        for info in archive.infolist()
        if not info.is_dir() and marker in info.filename.replace("\\", "/").lower()
    ]


def _relative_to_marker(name: str, marker: str) -> str:
    """Split on the marker case insensitively without index arithmetic."""
    lowered_marker = marker.lower()
    for index in range(len(name)):
        if name[index : index + len(marker)].lower() == lowered_marker:
            return name[index + len(marker) :]
    return ""


def _unsafe(relative: str) -> bool:
    """Reject archive entries that would escape the extraction directory."""
    candidate = Path(relative)
    return (
        relative.startswith(("/", "\\"))
        or candidate.is_absolute()
        or bool(candidate.drive)
        or ".." in candidate.parts
    )


def _check_writable(target: Path) -> None:
    """Refuse to half-overwrite a runtime that another process is using."""
    for existing in target.glob("*"):
        if not existing.is_file():
            continue
        try:
            with existing.open("ab"):
                pass
        except OSError as exc:
            raise SystemExit(
                f"{existing} is in use ({exc}). Close ComfyUI and any running DLSS "
                "worker before reinstalling the runtime."
            )


def _extract_tree(archive: zipfile.ZipFile, marker: str, target: Path) -> None:
    """Extract a directory tree, used for the Windows ffmpeg binaries."""
    members = _members(archive, marker)
    if not members:
        raise RuntimeError(f"The archive contains no {marker} entries.")
    target.mkdir(parents=True, exist_ok=True)
    _check_writable(target)
    print(f"Extracting {len(members)} files to {target}")
    resolved_target = target.resolve()
    skipped = []
    for info in members:
        name = info.filename.replace("\\", "/")
        relative = _relative_to_marker(name, marker)
        if not relative or _unsafe(relative):
            skipped.append(info.filename)
            continue
        destination = (target / relative).resolve()
        if not destination.is_relative_to(resolved_target):
            skipped.append(info.filename)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, destination.open("wb") as sink:
            shutil.copyfileobj(source, sink)
    for name in skipped:
        print(f"Skipped unsafe archive entry: {name}")


def archive_name(url: str) -> str:
    """Name a kept archive after the release file, not the temp download."""
    return Path(urllib.parse.urlparse(url).path).name or "release.zip"


def gpu_name() -> str | None:
    """Ask nvidia-smi for the first GPU, tolerating machines without it."""
    try:
        result = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            text=True,
            timeout=15,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    names = [line.strip() for line in result.splitlines() if line.strip()]
    return names[0] if names else None


def _extract(archive_path: Path, nr_arch: str = "auto") -> None:
    with zipfile.ZipFile(archive_path) as archive:
        infos = {
            info.filename: info
            for info in archive.infolist()
            if not info.is_dir()
        }
        detected_gpu = gpu_name()
        try:
            selected = select_runtime_members(
                infos, nr_arch=nr_arch, gpu_name=detected_gpu
            )
        except ArchiveLayoutError as exc:
            raise SystemExit(str(exc)) from exc
        RUNTIME_TARGET.mkdir(parents=True, exist_ok=True)
        _check_writable(RUNTIME_TARGET)
        print(f"Extracting {len(selected)} runtime files to {RUNTIME_TARGET}")
        for target_name, member in selected.items():
            info = infos[member]
            destination = RUNTIME_TARGET / target_name
            with archive.open(info) as source, destination.open("wb") as sink:
                shutil.copyfileobj(source, sink)
            print(f"  {target_name:22} <- {member}")
        build = selected_nr_arch(
            selected, nr_arch=nr_arch, gpu_name=detected_gpu
        )
        print(f"Neural Rendering build: {build}")
        if not IS_LINUX:
            _extract_tree(archive, "bin/ffmpeg/bin/", FFMPEG_TARGET)


def _confirm(auto_yes: bool) -> None:
    if auto_yes:
        return
    answer = input("Continue? [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        raise SystemExit("Aborted.")


def _register(runtime_dir: Path) -> None:
    missing = [name for name in REQUIRED_RUNTIME_FILES if not (runtime_dir / name).is_file()]
    if missing:
        if (runtime_dir / "host/nvngx.dll").is_file():
            raise SystemExit(
                f"{runtime_dir} is an upstream host-layout runtime. This pack "
                "needs the flat layout that install_runtime.py creates, so run "
                "install_runtime.py without --runtime-dir (it selects the "
                "worker and the GPU-specific Neural Rendering build), or point "
                "at a flat folder containing: "
                + ", ".join(REQUIRED_RUNTIME_FILES)
            )
        raise SystemExit(
            f"{runtime_dir} is not a DLSS 5 runtime directory. Missing: {', '.join(missing)}"
        )
    ffmpeg_dir = runtime_dir.parent / "ffmpeg" / "bin"
    if ffmpeg_dir.is_dir():
        config = write_config(runtime_dir, ffmpeg_dir)
    elif IS_LINUX:
        config = write_config(runtime_dir)
        print(
            "Note: Linux uses the system ffmpeg; none was extracted next to "
            "the runtime. The video node needs ffmpeg and ffprobe on PATH or "
            "in DLSS5_FFMPEG_DIR."
        )
    else:
        config = write_config(runtime_dir)
        print(
            "Warning: no ffmpeg found next to the runtime. The image node works, but "
            "the video node needs ffmpeg on PATH or in DLSS5_FFMPEG_DIR."
        )
    print(f"Registered {runtime_dir}")
    print(f"Wrote {config}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="Use an existing runtime directory (the folder containing nvngx.dll).",
    )
    parser.add_argument("--url", default=RELEASE_URL, help="Release archive to download.")
    parser.add_argument(
        "--nr-arch",
        choices=NR_ARCHES,
        default="auto",
        help="Neural Rendering build to install. auto picks by GPU name "
        "(RTX 20/30 Turing, RTX 40 Ada, RTX 50 Blackwell).",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument("--keep-archive", action="store_true", help="Keep the downloaded zip.")
    arguments = parser.parse_args()

    print(NOTICE)
    if arguments.runtime_dir:
        _register(arguments.runtime_dir.expanduser().resolve())
        return

    if not arguments.url.lower().startswith("https://"):
        raise SystemExit(
            f"Refusing to download {arguments.url}: only https URLs are accepted."
        )
    _confirm(arguments.yes)
    with tempfile.TemporaryDirectory(prefix="dlss5-") as workspace:
        archive = _download(arguments.url, Path(workspace) / "release.zip")
        _extract(archive, arguments.nr_arch)
        if arguments.keep_archive:
            shutil.copy2(archive, PACKAGE_ROOT / archive_name(arguments.url))

    layout = RuntimeLayout(root=RUNTIME_TARGET).validate()
    if IS_LINUX:
        if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            print(
                "Warning: ffmpeg/ffprobe are not on PATH. The image node works, "
                "but the video node needs them on PATH or in DLSS5_FFMPEG_DIR."
            )
        config = write_config(layout.root)
        print(f"Runtime ready in {layout.root}")
        print(f"Wrote {config}")
        print(
            "Linux: now run scripts/setup_linux.py once to configure the Wine "
            "prefix, the driver bridge and the patched ReShade carrier."
        )
    else:
        missing = [
            name
            for name in ("ffmpeg.exe", "ffprobe.exe")
            if not (FFMPEG_TARGET / name).is_file()
        ]
        if missing:
            raise SystemExit(
                f"The archive did not provide {', '.join(missing)} in {FFMPEG_TARGET}. "
                "The image node works, but the video node needs them."
            )
        config = write_config(layout.root, FFMPEG_TARGET)
        print(f"Runtime ready in {layout.root}")
        print(f"Wrote {config}")
    print("Restart ComfyUI to use the DLSS5 nodes.")


if __name__ == "__main__":
    main()
