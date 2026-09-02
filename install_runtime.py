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
import sys
import tempfile
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

RELEASE_URL = (
    "https://github.com/Merserk/dlss5-visual-enhancer/releases/download/3.0/"
    "DLSS.5.Visual.Enhancer.v3.0.zip"
)

RUNTIME_TARGET = PACKAGE_ROOT / "runtime"
FFMPEG_TARGET = PACKAGE_ROOT / "ffmpeg" / "bin"

NOTICE = """
This downloads the DLSS 5 Visual Enhancer release (about 467 MB) and extracts
only its runtime components:

  nvngx.dll             standalone D3D12 worker (an executable, not NVIDIA's NGX core)
  nvngx_dlss.dll        DLSS Super Resolution runtime      - NVIDIA proprietary terms
  nvngx_dlssnr.dll      DLSS Neural Rendering runtime      - NVIDIA proprietary terms
  dxgi.dll              ReShade carrier                    - BSD-3-Clause
  renodx-dlss5.addon64  RenoDX DLSS 5 add-on               - its own distribution terms
  ffmpeg.exe/ffprobe.exe

These files are neither owned nor redistributed by this node pack. Install only
components you are authorised to use, from sources their licences permit. This
project is not affiliated with NVIDIA, ReShade, RenoDX or the upstream project.
"""


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
    return target


def _members(archive: zipfile.ZipFile, marker: str) -> list[zipfile.ZipInfo]:
    return [
        info
        for info in archive.infolist()
        if not info.is_dir() and marker in info.filename.replace("\\", "/").lower()
    ]


def _extract(archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        groups = (
            ("bin/runtime/", RUNTIME_TARGET),
            ("bin/ffmpeg/bin/", FFMPEG_TARGET),
        )
        for marker, target in groups:
            members = _members(archive, marker)
            if not members:
                raise RuntimeError(f"The archive contains no {marker} entries.")
            target.mkdir(parents=True, exist_ok=True)
            print(f"Extracting {len(members)} files to {target}")
            for info in members:
                name = info.filename.replace("\\", "/")
                relative = name.split(marker, 1)[1]
                if not relative or relative.startswith("/") or ".." in relative.split("/"):
                    continue
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as sink:
                    shutil.copyfileobj(source, sink)


def _confirm(auto_yes: bool) -> None:
    print(NOTICE)
    if auto_yes:
        return
    answer = input("Continue? [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        raise SystemExit("Aborted.")


def _register(runtime_dir: Path) -> None:
    missing = [name for name in REQUIRED_RUNTIME_FILES if not (runtime_dir / name).is_file()]
    if missing:
        raise SystemExit(
            f"{runtime_dir} is not a DLSS 5 runtime directory. Missing: {', '.join(missing)}"
        )
    ffmpeg_dir = runtime_dir.parent / "ffmpeg" / "bin"
    config = write_config(runtime_dir, ffmpeg_dir if ffmpeg_dir.is_dir() else None)
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
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument("--keep-archive", action="store_true", help="Keep the downloaded zip.")
    arguments = parser.parse_args()

    if arguments.runtime_dir:
        _register(arguments.runtime_dir.expanduser().resolve())
        return

    _confirm(arguments.yes)
    with tempfile.TemporaryDirectory(prefix="dlss5-") as workspace:
        archive = _download(arguments.url, Path(workspace) / "release.zip")
        _extract(archive)
        if arguments.keep_archive:
            shutil.copy2(archive, PACKAGE_ROOT / archive.name)

    layout = RuntimeLayout(
        root=RUNTIME_TARGET,
        ffmpeg=FFMPEG_TARGET / "ffmpeg.exe",
        ffprobe=FFMPEG_TARGET / "ffprobe.exe",
    ).validate()
    config = write_config(layout.root, FFMPEG_TARGET)
    print(f"Runtime ready in {layout.root}")
    print(f"Wrote {config}")
    print("Restart ComfyUI to use the DLSS5 nodes.")


if __name__ == "__main__":
    main()
