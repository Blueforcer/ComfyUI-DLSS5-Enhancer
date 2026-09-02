"""Temporal guide buffers for DLSS.

A decoded video has no motion vectors, so they are estimated with dense optical
flow.  DLSS expects backward motion (where a pixel came from), computed at the
render resolution and stored as FP16.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

try:
    import cv2
except ImportError:  # reported when a node runs, not by hiding every node
    cv2 = None


def require_cv2() -> None:
    if cv2 is None:
        raise RuntimeError(
            "OpenCV is required for frame scaling and motion estimation. "
            "Install it with: python -m pip install opencv-python"
        )


@dataclass(frozen=True, slots=True)
class Guide:
    """Per-frame motion buffer plus the history-reset decision."""

    motion: np.ndarray
    reset: bool
    scene_score: float


class TemporalGuide:
    """Estimate motion between consecutive frames at the DLSS render size."""

    def __init__(
        self,
        width: int,
        height: int,
        *,
        flow_width: int = 640,
        scene_change_threshold: float = 0.24,
        enabled: bool = True,
    ) -> None:
        self.width = width
        self.height = height
        self.enabled = enabled
        self.scene_change_threshold = scene_change_threshold
        self.zero_motion = np.zeros((height, width, 2), dtype=np.float16)
        self._previous_gray: np.ndarray | None = None

        scale = min(1.0, flow_width / max(1, width))
        self.flow_width = max(64, int(round(width * scale / 2) * 2))
        self.flow_height = max(64, int(round(height * scale / 2) * 2))

        self._flow = None
        if enabled:
            require_cv2()
            self._flow = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
            self._flow.setUseSpatialPropagation(True)
            self._flow.setFinestScale(1)

    def _downscaled_gray(self, rgba: np.ndarray) -> np.ndarray:
        require_cv2()
        gray = cv2.cvtColor(rgba, cv2.COLOR_RGBA2GRAY)
        return cv2.resize(
            gray,
            (self.flow_width, self.flow_height),
            interpolation=cv2.INTER_AREA,
        )

    def process(self, rgba: np.ndarray) -> Guide:
        """Return the guide for ``rgba``; the first frame always resets history."""
        if not self.enabled:
            # Zero motion, but reset only once: resetting every frame would throw
            # away the temporal history the neural pass builds up.
            first = self._previous_gray is None
            self._previous_gray = self.zero_motion
            return Guide(motion=self.zero_motion, reset=first, scene_score=0.0)

        current = self._downscaled_gray(rgba)
        if self._previous_gray is None:
            self._previous_gray = current
            return Guide(motion=self.zero_motion, reset=True, scene_score=1.0)

        scene_score = float(np.mean(cv2.absdiff(current, self._previous_gray))) / 255.0
        reset = scene_score > self.scene_change_threshold
        if reset:
            motion = self.zero_motion
        else:
            # Flow from the current frame back to the previous one.
            flow = self._flow.calc(current, self._previous_gray, None)
            flow = cv2.resize(
                flow,
                (self.width, self.height),
                interpolation=cv2.INTER_LINEAR,
            )
            flow[..., 0] *= self.width / self.flow_width
            flow[..., 1] *= self.height / self.flow_height
            motion = np.ascontiguousarray(flow.astype(np.float16))

        self._previous_gray = current
        return Guide(motion=motion, reset=reset, scene_score=scene_score)
