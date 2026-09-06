import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from dlss5.diagnostics import detect_gpu, ensure_supported
from dlss5.paths import RuntimeLayout


class GPUDetectionTests(unittest.TestCase):
    def setUp(self):
        detect_gpu.cache_clear()
        self.addCleanup(detect_gpu.cache_clear)

    def detect(self, name, capability):
        detect_gpu.cache_clear()
        result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=f"{name}, 610.57.04, 32768, {capability}\n",
        )
        with patch("dlss5.diagnostics.subprocess.run", return_value=result):
            return detect_gpu()

    def test_geforce_generations(self):
        for name, capability, generation in (
            ("NVIDIA GeForce RTX 3090", "8.6", 30),
            ("NVIDIA GeForce RTX 4090 Laptop GPU", "8.9", 40),
            ("NVIDIA GeForce RTX 5090", "12.0", 50),
        ):
            with self.subTest(name=name):
                gpu = self.detect(name, capability)
                self.assertEqual(gpu["generation"], generation)
                self.assertEqual(gpu["beta"], generation == 30)

    def test_workstation_generations(self):
        for name, capability, generation in (
            ("NVIDIA RTX PRO 6000 Blackwell Workstation Edition", "12.0", 50),
            ("NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition", "12.0", 50),
            ("NVIDIA RTX PRO 6000 Blackwell Server Edition", "12.0", 50),
            ("NVIDIA RTX PRO 2000 Blackwell", "12.0", 50),
            ("NVIDIA RTX 6000 Ada Generation", "8.9", 40),
            ("NVIDIA RTX 2000 Ada Generation", "8.9", 40),
            ("NVIDIA RTX A6000", "8.6", 30),
            ("NVIDIA RTX A2000", "8.6", 30),
        ):
            with self.subTest(name=name):
                gpu = self.detect(name, capability)
                self.assertEqual(gpu["name"], name)
                self.assertEqual(gpu["generation"], generation)
                self.assertEqual(gpu["beta"], generation == 30)

    def test_turing_is_rejected_regardless_of_model_number(self):
        for name in ("NVIDIA GeForce RTX 2080 Ti", "NVIDIA Quadro RTX 6000",
                     "NVIDIA Quadro RTX 4000", "NVIDIA TITAN RTX"):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "compute capability 7.5"):
                self.detect(name, "7.5")

    def test_unknown_capability_does_not_fall_back_to_model_number(self):
        for capability in ("[N/A]", "", "9.0", "12.1"):
            with self.subTest(capability=capability), self.assertRaisesRegex(RuntimeError, "compute capability"):
                self.detect("NVIDIA RTX PRO 6000", capability)

    def test_compute_capability_alone_does_not_admit_non_rtx_gpus(self):
        for name, capability in (("NVIDIA A40", "8.6"), ("NVIDIA L40", "8.9"),
                                 ("NVIDIA GB10", "12.1")):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "No supported NVIDIA RTX GPU"):
                self.detect(name, capability)

    def test_ampere_workstation_keeps_windows_runtime_pair_check(self):
        gpu = self.detect("NVIDIA RTX A6000", "8.6")
        bundle = {"known_ampere_pair": False, "addon_sha256": "unknown", "neural_sha256": "unknown"}
        with patch("dlss5.diagnostics.sys.platform", "win32"), \
             patch("dlss5.diagnostics.detect_gpu", return_value=gpu), \
             patch("dlss5.diagnostics.inspect_bundle", return_value=bundle):
            with self.assertRaisesRegex(RuntimeError, "tested experimental Ampere pair"):
                ensure_supported(RuntimeLayout(Path("runtime")))
            bundle["known_ampere_pair"] = True
            self.assertEqual(ensure_supported(RuntimeLayout(Path("runtime"))), (gpu, bundle))


if __name__ == "__main__":
    unittest.main()
