"""Standalone protocol smoke test (run it through ../selftest.py).

Sends a short synthetic clip through the native worker without ComfyUI, so a
protocol, runtime or driver problem can be isolated from workflow issues.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np

from .diagnostics import detect_gpu, inspect_bundle
from .imaging import fit_frame
from .motion import TemporalGuide
from .paths import find_runtime
from .session import DlssSession
from .settings import UPSCALING_LABELS, DlssOptions


def _synthetic_frame(width: int, height: int, index: int) -> np.ndarray:
    """A gradient with a moving block, so motion vectors have something to find."""
    frame = np.zeros((height, width, 4), dtype=np.uint8)
    ramp = np.linspace(0, 255, width, dtype=np.uint8)
    frame[..., 0] = ramp[None, :]
    frame[..., 1] = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    frame[..., 3] = 255
    size = max(8, min(width, height) // 8)
    left = (index * 7) % max(1, width - size)
    top = height // 3
    frame[top : top + size, left : left + size, :3] = 255
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--frames", type=int, default=5)
    parser.add_argument("--mode", default=UPSCALING_LABELS[0], choices=UPSCALING_LABELS)
    parser.add_argument("--runtime-dir", default=None)
    arguments = parser.parse_args()

    layout = find_runtime(arguments.runtime_dir)
    print(f"runtime      : {layout.root}")
    print(f"worker       : {layout.worker}")
    gpu = detect_gpu()
    print(f"gpu          : {gpu['name']} (driver {gpu['driver']}, gen {gpu['generation']})")
    bundle = inspect_bundle(layout)
    print(f"addon sha256 : {bundle['addon_sha256']}")
    print(f"neural sha256: {bundle['neural_sha256']}")

    options = DlssOptions.create(upscaling_mode=arguments.mode)
    with DlssSession(
        layout,
        options,
        input_width=arguments.width,
        input_height=arguments.height,
        frame_count=arguments.frames,
    ) as session:
        print(
            f"negotiated   : render {session.render_width}x{session.render_height} "
            f"-> output {session.output_width}x{session.output_height}"
        )
        guide = TemporalGuide(session.render_width, session.render_height)
        for index in range(arguments.frames):
            rgba = fit_frame(
                _synthetic_frame(arguments.width, arguments.height, index),
                session.render_width,
                session.render_height,
            )
            motion = guide.process(rgba)
            output, pts = session.submit(
                index=index,
                rgba=rgba,
                motion=motion.motion,
                reset=motion.reset,
                pts=index,
            )
            print(
                f"frame {index:3d}    : {output.shape[1]}x{output.shape[0]} "
                f"pts={pts} reset={motion.reset} mean={float(output[..., :3].mean()):.2f}"
            )
        report = session.feature_report()

    print(f"feature 18   : verified, native fallback={report['native_fallback']}")
    for line in report["evidence"]:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
