"""Opt-in GPU regression: DLSS5_RUN_GPU_TESTS=1 python -m unittest discover -s tests."""

import os
import sys
import unittest

import numpy as np

from dlss5.imaging import fit_frame
from dlss5.paths import find_runtime
from dlss5.selftest import _synthetic_frame
from dlss5.session import DlssSession
from dlss5.settings import DlssOptions


@unittest.skipUnless(
    sys.platform == "linux" and os.environ.get("DLSS5_RUN_GPU_TESTS") == "1",
    "requires the Linux Wine runtime and an NVIDIA GPU",
)
class LinuxPixelTests(unittest.TestCase):
    def test_intensity_blends_with_current_frame(self):
        frames = [_synthetic_frame(320, 192, 0), _synthetic_frame(320, 192, 1)]
        frames[1][..., :3] = 255 - frames[1][..., :3]
        for factor, preset in ((1.0, "L"), (1.5, "M")):
            with self.subTest(factor=factor, preset=preset):
                rendered = {}
                for intensity in (0.0, 0.25, 1.0):
                    options = DlssOptions.create(
                        upscaling_factor=factor, dlss_model_preset=preset,
                        nr_intensity=intensity, local_tone_strength=0.0,
                        local_structure_strength=0.0, skin_structure_strength=0.0,
                    )
                    with DlssSession(find_runtime(), options, input_width=320,
                                     input_height=192, frame_count=2) as session:
                        motion = np.zeros((session.render_height, session.render_width, 2), dtype=np.float16)
                        output = []
                        for index, frame in enumerate(frames):
                            rgba = fit_frame(frame, session.render_width, session.render_height)
                            enhanced, pts = session.submit(
                                index=index, rgba=rgba, motion=motion, reset=True, pts=index,
                            )
                            self.assertEqual(pts, index)
                            output.append(enhanced[..., :3].astype(np.float32))
                    self.assertTrue(session.feature_report()["verified"])
                    rendered[intensity] = np.stack(output)

                # Zero intensity must preserve this frame's carrier image,
                # including after a different frame occupied the destination.
                for index, frame in enumerate(frames):
                    np.testing.assert_allclose(
                        rendered[0.0][index].mean(axis=(0, 1)),
                        frame[..., :3].mean(axis=(0, 1)), atol=5.0,
                    )
                expected = rendered[0.0] * 0.75 + rendered[1.0] * 0.25
                self.assertLess(float(np.abs(rendered[0.25] - expected).mean()), 1.0)


if __name__ == "__main__":
    unittest.main()
