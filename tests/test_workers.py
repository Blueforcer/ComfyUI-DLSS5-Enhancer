"""CPU-only tests for the Linux worker environment and launch."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dlss5 import paths, workers


def write_settings(directory: Path, settings: dict) -> Path:
    config = directory / "linux-runtime.json"
    config.write_text(
        json.dumps({"version": 1, "environment": settings}, indent=2),
        encoding="utf-8",
    )
    return config


def complete_settings(**overrides) -> dict:
    settings = {
        "WINEPREFIX": "/tmp/dlss-prefix",
        "DLSS_WINE_PATH": "/tmp/dlss-wine",
        "WINESERVER": "/tmp/dlss-wineserver",
        "WINEDEBUG": "-all",
        "WINE_HEAP_DELAY_FREE": "1",
        "WINE_HEAP_ZERO_MEMORY": "1",
        "WINEDLLOVERRIDES": "dxgi,d3d12=n",
        "NVIDIA_WINE_DLL_DIR": "/usr/lib64/nvidia/wine",
        "DXVK_ENABLE_NVAPI": "1",
        "DXVK_CONFIG": "dxgi.hideNvidiaGpu = False",
        "DXVK_FILTER_DEVICE_NAME": "GPU",
        "VKD3D_FILTER_DEVICE_NAME": "GPU",
    }
    settings.update(overrides)
    return settings


class WorkerEnvironmentTests(unittest.TestCase):
    """The saved settings apply to workers only, with strict validation."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="dlss5-workers-"))
        self.addCleanup(shutil.rmtree, self.directory)

    def test_without_saved_settings_the_environment_is_inherited(self):
        with mock.patch.object(workers, "CONFIG_PATH", self.directory / "absent.json"):
            environment = workers.linux_worker_environment()
        self.assertEqual(environment, os.environ.copy())

    def test_saved_settings_override_ambient_wine_only_for_worker(self):
        settings = complete_settings(WINEPREFIX=str(self.directory))
        config = write_settings(self.directory, settings)
        with (
            mock.patch.object(workers, "CONFIG_PATH", config),
            mock.patch.dict(os.environ, {"WINEPREFIX": "/ambient/prefix"}),
        ):
            environment = workers.linux_worker_environment()
            # The ambient environment ComfyUI runs in is untouched.
            self.assertEqual(os.environ.get("WINEPREFIX"), "/ambient/prefix")
        self.assertEqual(environment["WINEPREFIX"], str(self.directory))
        self.assertIsNone(os.environ.get("WINEPREFIX"))

    def test_invalid_settings_fail_with_file_path(self):
        broken = self.directory / "linux-runtime.json"
        broken.write_text("{not json", encoding="utf-8")
        with (
            mock.patch.object(workers, "CONFIG_PATH", broken),
            self.assertRaises(RuntimeError) as caught,
        ):
            workers.linux_worker_environment()
        self.assertIn(str(broken), str(caught.exception))
        self.assertIn("setup_linux.py", str(caught.exception))

    def test_incomplete_key_set_is_actionable(self):
        settings = complete_settings()
        settings.pop("DXVK_FILTER_DEVICE_NAME")
        config = write_settings(self.directory, settings)
        with (
            mock.patch.object(workers, "CONFIG_PATH", config),
            self.assertRaises(RuntimeError) as caught,
        ):
            workers.linux_worker_environment()
        self.assertIn("DXVK_FILTER_DEVICE_NAME", str(caught.exception))


class WorkerLaunchTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="dlss5-launch-"))
        self.addCleanup(shutil.rmtree, self.directory)
        self.worker = self.directory / "nvngx.dll"
        self.worker.write_bytes(b"MZ" + b"\0" * 62)
        self.prefix = self.directory / "prefix"
        (self.prefix / "drive_c/windows/system32").mkdir(parents=True)

    def test_lfs_pointer_and_invalid_binary_fail_before_launch(self):
        pointer = self.directory / "pointer.dll"
        pointer.write_text(
            "version https://git-lfs.github.com/spec/v1", encoding="utf-8"
        )
        with self.assertRaises(RuntimeError) as caught:
            workers.validate_binary(pointer)
        self.assertIn("git lfs pull", str(caught.exception))
        with self.assertRaises(RuntimeError):
            workers.validate_binary(self.directory / "missing.dll")

    @unittest.skipUnless(sys.platform == "linux", "Linux launch behaviour")
    def test_linux_requires_configured_prefix(self):
        settings = complete_settings(
            WINEPREFIX="relative-prefix", DLSS_WINE_PATH=""
        )
        config = write_settings(self.directory, settings)
        with mock.patch.object(workers, "CONFIG_PATH", config):
            with self.assertRaises(RuntimeError) as caught:
                workers.worker_launch(self.worker, "--video")
            self.assertIn("WINEPREFIX", str(caught.exception))

        settings["WINEPREFIX"] = str(self.directory / "not-initialized")
        write_settings(self.directory, settings)
        with mock.patch.object(workers, "CONFIG_PATH", config):
            with self.assertRaises(RuntimeError) as caught:
                workers.worker_launch(self.worker, "--video")
            self.assertIn("not initialized", str(caught.exception))

    @unittest.skipUnless(sys.platform == "linux", "Linux launch behaviour")
    def test_wine_command_is_built_with_saved_settings(self):
        fake_wine = self.directory / "fake-wine"
        fake_wine.write_text("#!/bin/sh\nexit 0\n")
        fake_wine.chmod(0o755)
        settings = complete_settings(
            WINEPREFIX=str(self.prefix), DLSS_WINE_PATH=str(fake_wine)
        )
        config = write_settings(self.directory, settings)
        with (
            mock.patch.object(workers, "CONFIG_PATH", config),
            mock.patch.dict(os.environ, {"DXVK_FILTER_DEVICE_NAME": "ambient"}),
        ):
            command, options = workers.worker_launch(self.worker, "--video")
        self.assertEqual(
            command, [str(fake_wine), str(self.worker.resolve()), "--video"]
        )
        self.assertTrue(options["start_new_session"])
        self.assertEqual(options["env"]["DXVK_FILTER_DEVICE_NAME"], "GPU")
        self.assertNotEqual(options["env"], os.environ)

    @unittest.skipUnless(sys.platform == "win32", "Windows launch behaviour")
    def test_windows_launch_is_direct(self):
        command, options = workers.worker_launch(self.worker, "--video")
        self.assertEqual(command, [str(self.worker.resolve()), "--video"])
        self.assertIn("creationflags", options)

    @unittest.skipUnless(sys.platform == "linux", "Linux launch behaviour")
    def test_worker_process_owns_its_group(self):
        fake_wine = self.directory / "fake-wine"
        fake_wine.write_text("#!/bin/sh\ntrap '' TERM\nsleep 30\n")
        fake_wine.chmod(0o755)
        settings = complete_settings(
            WINEPREFIX=str(self.prefix), DLSS_WINE_PATH=str(fake_wine)
        )
        config = write_settings(self.directory, settings)
        with mock.patch.object(workers, "CONFIG_PATH", config):
            process = workers.WorkerProcess(self.worker, "--video")
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        self.assertEqual(
            process.args, [str(fake_wine), str(self.worker.resolve()), "--video"]
        )
        self.assertIsInstance(process, subprocess.Popen)
        process.terminate()
        process.kill()


if __name__ == "__main__":
    unittest.main()