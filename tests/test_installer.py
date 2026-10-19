"""CPU-only tests for the release-archive mapping and Python discovery."""

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dlss5.runtime_archive import (  # noqa: E402
    ArchiveLayoutError,
    select_nr_arch,
    select_runtime_members,
    selected_nr_arch,
)
import install_runtime  # noqa: E402


FLAT_V3 = [
    "bin/runtime/nvngx.dll",
    "bin/runtime/dxgi.dll",
    "bin/runtime/renodx-dlss5.addon64",
    "bin/runtime/nvngx_dlss.dll",
    "bin/runtime/nvngx_dlssnr.dll",
    "bin/ffmpeg/bin/ffmpeg.exe",
]

HOST_V6 = [
    "bin/runtime/host/nvngx.dll",
    "bin/runtime/host/dxgi.dll",
    "bin/runtime/host/renodx-dlss5.addon64",
    "bin/runtime/host/nvngx_dlssnr.dll",
    "bin/runtime/dlss/nvngx_dlss.dll",
    "bin/runtime/dlssnr/Turing+/nvngx_dlssnr.dll",
    "bin/runtime/dlssnr/Ada Lovelace+/nvngx_dlssnr.dll",
    "bin/runtime/dlssnr/Blackwell+/nvngx_dlssnr.dll",
    "bin/ffmpeg/bin/ffmpeg.exe",
]


class MappingTests(unittest.TestCase):
    def test_flat_release_maps_to_the_same_names(self):
        selected = select_runtime_members(FLAT_V3)
        self.assertEqual(
            selected,
            {
                "nvngx.dll": "bin/runtime/nvngx.dll",
                "renodx-dlss5.addon64": "bin/runtime/renodx-dlss5.addon64",
                "nvngx_dlss.dll": "bin/runtime/nvngx_dlss.dll",
                "dxgi.dll": "bin/runtime/dxgi.dll",
                "nvngx_dlssnr.dll": "bin/runtime/nvngx_dlssnr.dll",
            },
        )

    def test_host_release_picks_the_gpu_variant(self):
        selected = select_runtime_members(
            HOST_V6, gpu_name="NVIDIA GeForce RTX 5090"
        )
        self.assertEqual(
            selected["nvngx_dlssnr.dll"],
            "bin/runtime/dlssnr/Blackwell+/nvngx_dlssnr.dll",
        )
        self.assertEqual(selected["nvngx.dll"], "bin/runtime/host/nvngx.dll")
        self.assertEqual(
            selected["nvngx_dlss.dll"], "bin/runtime/dlss/nvngx_dlss.dll"
        )

    def test_host_release_without_gpu_uses_the_host_runtime(self):
        selected = select_runtime_members(HOST_V6)
        self.assertEqual(
            selected["nvngx_dlssnr.dll"],
            "bin/runtime/host/nvngx_dlssnr.dll",
        )

    def test_explicit_arch_overrides_detection(self):
        selected = select_runtime_members(
            HOST_V6, nr_arch="turing", gpu_name="NVIDIA GeForce RTX 5090"
        )
        self.assertEqual(
            selected["nvngx_dlssnr.dll"],
            "bin/runtime/dlssnr/Turing+/nvngx_dlssnr.dll",
        )

    def test_backslashes_are_normalized(self):
        selected = select_runtime_members(
            [name.replace("/", "\\") for name in HOST_V6],
            gpu_name="RTX 4090",
        )
        self.assertEqual(
            selected["nvngx_dlssnr.dll"],
            "bin\\runtime\\dlssnr\\Ada Lovelace+\\nvngx_dlssnr.dll",
        )

    def test_v7_addon_layout_is_rejected(self):
        names = [
            name for name in HOST_V6 if name != "bin/runtime/host/renodx-dlss5.addon64"
        ]
        names.append("bin/runtime/dlssnr/renodx-dlss5.addon64")
        with self.assertRaisesRegex(ArchiveLayoutError, "v7.0"):
            select_runtime_members(names)

    def test_v8_without_worker_is_rejected(self):
        names = [name for name in HOST_V6 if "nvngx.dll" not in name]
        with self.assertRaisesRegex(ArchiveLayoutError, "v8.0"):
            select_runtime_members(names)

    def test_missing_piece_is_reported(self):
        names = [name for name in HOST_V6 if "nvngx_dlss.dll" not in name]
        with self.assertRaisesRegex(ArchiveLayoutError, "nvngx_dlss.dll"):
            select_runtime_members(names)


class ArchTests(unittest.TestCase):
    def test_gpu_names_map_to_architectures(self):
        cases = {
            "NVIDIA GeForce RTX 5090": "blackwell",
            "NVIDIA GeForce RTX 5080": "blackwell",
            "NVIDIA GeForce RTX 4090": "ada",
            "NVIDIA GeForce RTX 4070 Ti": "ada",
            "NVIDIA GeForce RTX 3090": "turing",
            "NVIDIA GeForce RTX 2080": "turing",
            "NVIDIA RTX A6000": None,
            "AMD Radeon": None,
            "": None,
            None: None,
        }
        for name, expected in cases.items():
            self.assertEqual(select_nr_arch(name), expected, name)

    def test_selected_variant_is_reported(self):
        selected = select_runtime_members(
            HOST_V6, gpu_name="NVIDIA GeForce RTX 5090"
        )
        self.assertEqual(selected_nr_arch(selected), "blackwell")


class ArchiveNameTests(unittest.TestCase):
    def test_kept_archive_uses_the_release_filename(self):
        self.assertEqual(
            install_runtime.archive_name(install_runtime.RELEASE_URL),
            "DLSS.5.Visual.Enhancer.v6.0.zip",
        )

    def test_query_string_does_not_leak_into_the_name(self):
        self.assertEqual(
            install_runtime.archive_name(
                "https://example.invalid/releases/v6.0/Package.zip?x=1"
            ),
            "Package.zip",
        )


class PythonDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="dlss5-python-"))
        self.addCleanup(shutil.rmtree, self.directory)
        spec = importlib.util.spec_from_file_location(
            "dlss5_setup_linux", ROOT / "scripts" / "setup_linux.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_prefers_venv_over_dot_venv(self):
        (self.directory / "venv/bin").mkdir(parents=True)
        (self.directory / "venv/bin/python").write_text("")
        (self.directory / ".venv/bin").mkdir(parents=True)
        (self.directory / ".venv/bin/python").write_text("")
        self.assertEqual(
            self.module.default_python(self.directory),
            self.directory / "venv/bin/python",
        )

    def test_falls_back_to_dot_venv(self):
        (self.directory / ".venv/bin").mkdir(parents=True)
        (self.directory / ".venv/bin/python").write_text("")
        self.assertEqual(
            self.module.default_python(self.directory),
            self.directory / ".venv/bin/python",
        )


if __name__ == "__main__":
    unittest.main()
