import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dlss5.diagnostics import LogRing, verify_feature_18
from dlss5.paths import _resolve_ffmpeg


class LinuxRuntimeTests(unittest.TestCase):
    def test_verification_requires_neural_evaluation(self):
        initialized = "[dlss5nr] signed NR initialized\n"
        created = "[dlss5nr] Feature18 CreateFeature(18) -> 0x00000001 handle=123\n"
        with self.assertRaises(RuntimeError):
            verify_feature_18(initialized + created, direct=True)
        failed = created.replace("0x00000001", "0xBAD00005")
        evaluated = "[dlss5nr] Feature18 EvaluateFeature succeeded (frame=1, carrier=1)"
        with self.assertRaises(RuntimeError):
            verify_feature_18(initialized + failed + evaluated, direct=True)
        self.assertTrue(verify_feature_18(initialized + created + evaluated, direct=True)["verified"])

    def test_long_video_retains_neural_evidence(self):
        ring = LogRing()
        for line in ("[dlss5nr] signed NR initialized",
                     "[dlss5nr] Feature18 CreateFeature(18) -> 0x00000001",
                     "[dlss5nr] Feature18 EvaluateFeature succeeded (frame=1)"):
            ring.add(line)
        for index in range(1000):
            ring.add(f"driver message {index}")
        self.assertTrue(verify_feature_18("\n".join(ring.snapshot()), direct=True)["verified"])

    def test_linux_ffmpeg_ignores_bundled_windows_executables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                (root / name).touch()
            with patch("dlss5.paths.sys.platform", "linux"), \
                 patch("dlss5.paths._ffmpeg_candidates", return_value=[root]), \
                 patch("dlss5.paths.shutil.which", side_effect=["/usr/bin/ffmpeg", "/usr/bin/ffprobe"]):
                self.assertEqual(_resolve_ffmpeg({}, root), (Path("/usr/bin/ffmpeg"), Path("/usr/bin/ffprobe")))


if __name__ == "__main__":
    unittest.main()
