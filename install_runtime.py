"""Fetch or register the native DLSS 5 runtime this node pack drives.

The runtime consists of NVIDIA and upstream-built binaries that are not part of
this repository and are not redistributed by it.  This script downloads the
official DLSS 5 Visual Enhancer release and keeps only the runtime and ffmpeg
components, or simply records the path of an existing installation.

Two upstream layouts exist and this script handles both:

  v10.0  bin/runtime/{dlssg,dlssnr,rtx_video}/   (default)
  v3.0   bin/runtime/*.dll                        (--release v3)

Usage:
    python install_runtime.py                      # download and install v10.0
    python install_runtime.py --release v3         # download and install v3.0
    python install_runtime.py --runtime-dir <path> # use an existing install
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dlss5.paths import (  # noqa: E402  (path bootstrap must run first)
    PACKAGE_ROOT,
    write_config,
)

# Upstream release tags carry a leading "v" ("v10.0", not "10.0"), and the asset
# filename changed between releases.  Getting either wrong yields a bare 404.
RELEASES = {
    "v10": {
        "url": (
            "https://github.com/Merserk/dlss5-visual-enhancer/releases/download/v10.0/"
            "Visual.Enhancer.v10.0.zip"
        ),
        "size_mb": 690,
        # Relative to bin/runtime/.  Every file the feature actually needs at
        # load time -- the engine DLLs resolve their siblings from their own
        # directory, so a subfolder must be installed whole or not at all.
        "runtime_files": (
            "dlssnr/nvngx_dlssnr.dll",
            "dlssnr/neuroframe_engine_neural_rendering.dll",
            "dlssnr/neuroframe_caller.dll",
            "dlssg/nvngx_dlssg.dll",
            "dlssg/neuroframe_engine_frame_interpolation.dll",
            "rtx_video/nvngx_vsr.dll",
            "rtx_video/nvngx_truehdr.dll",
            "rtx_video/neuroframe_engine_upscaling.dll",
        ),
    },
    "v3": {
        "url": (
            "https://github.com/Merserk/dlss5-visual-enhancer/releases/download/v3.0/"
            "DLSS.5.Visual.Enhancer.v3.0.zip"
        ),
        "size_mb": 467,
        "runtime_files": (
            "nvngx.dll",
            "nvngx_dlss.dll",
            "nvngx_dlssnr.dll",
            "dxgi.dll",
            "renodx-dlss5.addon64",
        ),
    },
}
DEFAULT_RELEASE = "v10"

RUNTIME_TARGET = PACKAGE_ROOT / "runtime"
FFMPEG_TARGET = PACKAGE_ROOT / "ffmpeg" / "bin"

NOTICES = {
    "v10": """
This downloads the DLSS 5 Visual Enhancer v10.0 release (about 690 MB) and
extracts only its runtime components:

  dlssnr/nvngx_dlssnr.dll     DLSS Neural Rendering runtime  - NVIDIA proprietary terms
  dlssnr/neuroframe_engine_neural_rendering.dll
  dlssnr/neuroframe_caller.dll
                              in-process D3D12/NGX feature-18 bridge and caller
                              shim built by the upstream project, under its own terms
  dlssg/nvngx_dlssg.dll       DLSS Frame Generation runtime  - NVIDIA proprietary terms
  dlssg/neuroframe_engine_frame_interpolation.dll
  rtx_video/nvngx_vsr.dll     RTX Video Super Resolution     - NVIDIA RTX Video SDK terms
  rtx_video/nvngx_truehdr.dll RTX Video HDR                  - NVIDIA RTX Video SDK terms
  rtx_video/neuroframe_engine_upscaling.dll
  ffmpeg.exe/ffprobe.exe

The LICENSE-*.txt and NVIDIA_RTX_Video_SDK_License.* files shipped alongside
these binaries are installed with them and must not be removed.

Note that v10.0 dropped the ReShade/RenoDX injection path entirely: there is no
nvngx.dll worker, no dxgi.dll carrier, no renodx-dlss5.addon64, and no DLSS
Super Resolution (nvngx_dlss.dll).  If you need those, install v3.0 instead
with --release v3.

These files are neither owned nor redistributed by this node pack. Install only
components you are authorised to use, from sources their licences permit. This
project is not affiliated with NVIDIA, ReShade, RenoDX or the upstream project.
""",
    "v3": """
This downloads the DLSS 5 Visual Enhancer v3.0 release (about 467 MB) and
extracts only its runtime components:

  nvngx.dll             standalone D3D12 worker built by the upstream project, under
                        its own terms (an executable, not NVIDIA's NGX core)
  nvngx_dlss.dll        DLSS Super Resolution runtime      - NVIDIA proprietary terms
  nvngx_dlssnr.dll      DLSS Neural Rendering runtime      - NVIDIA proprietary terms
  dxgi.dll              ReShade carrier                    - BSD-3-Clause
  renodx-dlss5.addon64  RenoDX DLSS 5 add-on               - its own distribution terms
  ffmpeg.exe/ffprobe.exe

These files are neither owned nor redistributed by this node pack. Install only
components you are authorised to use, from sources their licences permit. This
project is not affiliated with NVIDIA, ReShade, RenoDX or the upstream project.
""",
}


def _download(url: str, target: Path) -> Path:
    print(f"Downloading {url}")
    try:
        response = urllib.request.urlopen(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise SystemExit(
                f"The release archive was not found (HTTP 404):\n  {url}\n\n"
                "The URL is wrong, not your install. Open\n"
                "  https://github.com/Merserk/dlss5-visual-enhancer/releases\n"
                "and copy the exact asset link -- note that the tag carries a "
                "leading 'v' (v10.0) and the asset filename differs between "
                "releases. Then pass it with --url."
            ) from exc
        raise
    with response, target.open("wb") as sink:
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
    for existing in target.rglob("*"):
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


def _extract(archive_path: Path) -> None:
    """Extract bin/runtime/ and bin/ffmpeg/bin/, preserving subdirectories.

    v10.0 nests its runtime in dlssg/, dlssnr/ and rtx_video/ subfolders and the
    engine DLLs resolve their siblings from their own directory, so the relative
    path below the marker is preserved verbatim rather than flattened.
    """
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


def _detect_layout(runtime_dir: Path) -> str | None:
    """Return the release key whose manifest this directory satisfies."""
    for key, release in RELEASES.items():
        if all((runtime_dir / name).is_file() for name in release["runtime_files"]):
            return key
    return None


def _verify_runtime(runtime_dir: Path, release_key: str) -> None:
    expected = RELEASES[release_key]["runtime_files"]
    missing = [name for name in expected if not (runtime_dir / name).is_file()]
    if not missing:
        return
    detected = _detect_layout(runtime_dir)
    hint = ""
    if detected and detected != release_key:
        hint = (
            f"\n\nThe files present match the {detected} layout instead. "
            f"Re-run with --release {detected}."
        )
    raise SystemExit(
        f"{runtime_dir} is not a complete {release_key} DLSS 5 runtime.\n"
        f"Missing: {', '.join(missing)}{hint}"
    )


def _confirm(auto_yes: bool) -> None:
    answer = input("Continue? [y/N] ").strip().lower() if not auto_yes else "y"
    if answer not in {"y", "yes"}:
        raise SystemExit("Aborted.")


def _register(runtime_dir: Path, release_key: str | None) -> None:
    detected = release_key or _detect_layout(runtime_dir)
    if detected is None:
        raise SystemExit(
            f"{runtime_dir} is not a DLSS 5 runtime directory. Expected either the "
            f"v10 layout ({RELEASES['v10']['runtime_files'][0]}) or the v3 layout "
            f"({RELEASES['v3']['runtime_files'][0]})."
        )
    _verify_runtime(runtime_dir, detected)
    ffmpeg_dir = runtime_dir.parent / "ffmpeg" / "bin"
    if not ffmpeg_dir.is_dir():
        print(
            "Warning: no ffmpeg found next to the runtime. The image node works, but "
            "the video node needs ffmpeg on PATH or in DLSS5_FFMPEG_DIR."
        )
    config = write_config(runtime_dir, ffmpeg_dir if ffmpeg_dir.is_dir() else None)
    print(f"Registered {runtime_dir} ({detected} layout)")
    print(f"Wrote {config}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="Use an existing runtime directory (the folder containing dlssnr/ or nvngx.dll).",
    )
    parser.add_argument(
        "--release",
        choices=sorted(RELEASES),
        default=DEFAULT_RELEASE,
        help=f"Upstream release layout to install (default: {DEFAULT_RELEASE}).",
    )
    parser.add_argument("--url", help="Release archive to download (overrides --release).")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument("--keep-archive", action="store_true", help="Keep the downloaded zip.")
    arguments = parser.parse_args()

    release_key = arguments.release
    # Stray whitespace around a pasted URL turns into a 404, so drop it early.
    url = (arguments.url or RELEASES[release_key]["url"]).strip()

    print(NOTICES[release_key])
    if arguments.runtime_dir:
        explicit = release_key if arguments.release != DEFAULT_RELEASE else None
        _register(arguments.runtime_dir.expanduser().resolve(), explicit)
        return

    if not url.lower().startswith("https://"):
        raise SystemExit(f"Refusing to download {url}: only https URLs are accepted.")
    _confirm(arguments.yes)
    with tempfile.TemporaryDirectory(prefix="dlss5-") as workspace:
        archive = _download(url, Path(workspace) / "release.zip")
        _extract(archive)
        if arguments.keep_archive:
            shutil.copy2(archive, PACKAGE_ROOT / archive.name)

    _verify_runtime(RUNTIME_TARGET, release_key)
    missing = [
        name for name in ("ffmpeg.exe", "ffprobe.exe") if not (FFMPEG_TARGET / name).is_file()
    ]
    if missing:
        raise SystemExit(
            f"The archive did not provide {', '.join(missing)} in {FFMPEG_TARGET}. "
            "The image node works, but the video node needs them."
        )
    config = write_config(RUNTIME_TARGET, FFMPEG_TARGET)
    print(f"Runtime ready in {RUNTIME_TARGET} ({release_key} layout)")
    print(f"Wrote {config}")
    print("Restart ComfyUI to use the DLSS5 nodes.")


if __name__ == "__main__":
    main()
