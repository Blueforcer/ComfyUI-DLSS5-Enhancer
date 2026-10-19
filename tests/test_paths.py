"""CPU-only tests for runtime discovery, Linux staging and ffmpeg resolution."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dlss5 import paths


def make_runtime(directory: Path) -> Path:
    runtime = directory / "runtime"
    runtime.mkdir()
    for name in paths.REQUIRED_RUNTIME_FILES:
        (runtime / name).write_bytes(b"MZ" + name.encode() + b"\0" * 56)
    return runtime


class StagingTests(unittest.TestCase):
    """The Linux staging directory mirrors the runtime without copying it."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="dlss5-paths-"))
        self.addCleanup(shutil.rmtree, self.directory)
        self.runtime = make_runtime(self.directory)
        self.staging = self.directory / "staging"

    def _layout(self, config=None):
        return paths.RuntimeLayout(root=self.runtime, config=config or {})

    def _stage(self, layout):
        with mock.patch.object(paths, "LINUX_STAGING_ROOT", self.staging):
            paths.prepare_linux_runtime(layout)

    def test_worker_is_copied_and_components_are_linked(self):
        self._stage(self._layout())
        worker = self.staging / "nvngx.dll"
        self.assertTrue(worker.is_file() and not worker.is_symlink())
        for name in ("renodx-dlss5.addon64", "nvngx_dlss.dll", "nvngx_dlssnr.dll"):
            link = self.staging / name
            self.assertTrue(link.is_symlink(), name)
            self.assertEqual(link.readlink(), self.runtime / name)

    def test_staging_is_idempotent(self):
        layout = self._layout()
        self._stage(layout)
        worker = self.staging / "nvngx.dll"
        stamp = worker.stat().st_mtime_ns
        self._stage(layout)
        self.assertEqual(worker.stat().st_mtime_ns, stamp)

    def test_carrier_is_copied_from_the_environment(self):
        carrier = self.directory / "patched-dxgi.dll"
        carrier.write_bytes(b"MZ" + b"\x00" * 62)
        with (
            mock.patch.dict(os.environ, {paths.CARRIER_ENV: str(carrier)}),
            mock.patch.object(paths, "LINUX_STAGING_ROOT", self.staging),
        ):
            self._stage(self._layout())
        staged = self.staging / "dxgi.dll"
        self.assertTrue(staged.is_file() and not staged.is_symlink())
        self.assertEqual(staged.read_bytes(), carrier.read_bytes())

    def test_mismatched_sibling_carrier_is_rejected(self):
        with (
            mock.patch.object(
                paths, "SIBLING_CARRIER", self.directory / "sibling-dxgi.dll"
            ),
            mock.patch.object(
                paths, "EXPECTED_LINUX_CARRIER_SHA256", "0" * 64
            ),
        ):
            (self.directory / "sibling-dxgi.dll").write_bytes(b"MZ" + b"\0" * 62)
            with mock.patch.object(
                paths, "LINUX_STAGING_ROOT", self.staging
            ):
                with self.assertRaises(paths.RuntimeMissing) as caught:
                    self._stage(self._layout())
        self.assertIn("does not match", str(caught.exception))

    def test_missing_bridge_is_reported_by_require_linux_staging(self):
        layout = self._layout()
        with mock.patch.object(paths, "LINUX_STAGING_ROOT", self.staging):
            paths.prepare_linux_runtime(layout)
            with self.assertRaises(paths.RuntimeMissing) as caught:
                layout.require_linux_staging()
        self.assertIn("_nvngx.dll", str(caught.exception))
        self.assertIn("setup_linux.py", str(caught.exception))

    def test_complete_staging_passes(self):
        layout = self._layout()
        carrier = self.directory / "carrier.dll"
        carrier.write_bytes(b"MZ" + b"\0" * 62)
        bridge = self.directory / "bridge" / "_nvngx.dll"
        bridge.parent.mkdir()
        bridge.write_bytes(b"MZ" + b"\0" * 62)
        with (
            mock.patch.dict(os.environ, {paths.CARRIER_ENV: str(carrier)}),
            mock.patch.object(paths, "LINUX_STAGING_ROOT", self.staging),
        ):
            (self.staging).mkdir(exist_ok=True)
            (self.staging / "_nvngx.dll").symlink_to(bridge)
            layout.require_linux_staging()
            self.assertTrue((self.staging / "dxgi.dll").is_file())


class FfmpegTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="dlss5-ffmpeg-"))
        self.addCleanup(shutil.rmtree, self.directory)
        self.runtime = make_runtime(self.directory)

    def test_linux_uses_native_binaries_and_skips_windows_ffmpeg_dirs(self):
        windows_dir = self.directory / "ffmpeg-bin"
        windows_dir.mkdir()
        (windows_dir / "ffmpeg.exe").write_bytes(b"")
        (windows_dir / "ffprobe.exe").write_bytes(b"")
        config = {"ffmpeg_dir": str(windows_dir)}
        with mock.patch.object(paths, "IS_LINUX", True):
            with self.assertRaises(paths.RuntimeMissing):
                # No native ffmpeg in the candidate dirs and none on PATH
                # inside this patched environment: the exe dir must be
                # skipped rather than used.
                with mock.patch.object(
                    paths.shutil, "which", return_value=None
                ):
                    paths._resolve_ffmpeg(config, self.runtime)
        with mock.patch.object(paths, "IS_LINUX", True):
            native = self.directory / "native"
            native.mkdir()
            (native / "ffmpeg").write_bytes(b"")
            (native / "ffprobe").write_bytes(b"")
            with mock.patch.dict(os.environ, {paths.FFMPEG_ENV: str(native)}):
                ffmpeg, ffprobe = paths._resolve_ffmpeg(config, self.runtime)
            self.assertEqual(ffmpeg.name, "ffmpeg")
            self.assertEqual(ffprobe.name, "ffprobe")

    def test_windows_layout_prefers_bundled_exe(self):
        windows_dir = self.directory / "ffmpeg-bin"
        windows_dir.mkdir()
        (windows_dir / "ffmpeg.exe").write_bytes(b"")
        (windows_dir / "ffprobe.exe").write_bytes(b"")
        config = {"ffmpeg_dir": str(windows_dir)}
        with mock.patch.object(paths, "IS_LINUX", False):
            ffmpeg, _ = paths._resolve_ffmpeg(config, self.runtime)
        self.assertEqual(ffmpeg.name, "ffmpeg.exe")


class PlatformPathTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="dlss5-layout-"))
        self.addCleanup(shutil.rmtree, self.directory)
        self.runtime = make_runtime(self.directory)

    def test_linux_worker_and_log_live_in_staging(self):
        layout = paths.RuntimeLayout(root=self.runtime)
        staging = self.directory / "staging"
        with (
            mock.patch.object(paths, "IS_LINUX", True),
            mock.patch.object(paths, "LINUX_STAGING_ROOT", staging),
        ):
            self.assertEqual(layout.worker, staging / "nvngx.dll")
            self.assertEqual(layout.session_root, staging)
            self.assertEqual(layout.reshade_log, staging / "ReShade.log")
            self.assertEqual(layout.addon, self.runtime / "renodx-dlss5.addon64")

    def test_windows_worker_and_log_live_in_the_runtime(self):
        layout = paths.RuntimeLayout(root=self.runtime)
        with mock.patch.object(paths, "IS_LINUX", False):
            self.assertEqual(layout.worker, self.runtime / "nvngx.dll")
            self.assertEqual(layout.session_root, self.runtime)
            self.assertEqual(layout.reshade_log, self.runtime / "ReShade.log")


if __name__ == "__main__":
    unittest.main()