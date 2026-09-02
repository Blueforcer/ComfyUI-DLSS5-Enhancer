"""Conversions between ComfyUI IMAGE tensors and the worker's RGBA8 frames."""

from __future__ import annotations

import numpy as np
import torch

try:
    import cv2
except ImportError:  # reported when a node runs, not by hiding every node
    cv2 = None


def require_cv2() -> None:
    if cv2 is None:
        raise RuntimeError(
            "OpenCV is required for frame scaling and motion estimation. "
            "Install opencv-python, or opencv-contrib-python if you already use it."
        )


def fit_frame(rgba: np.ndarray, width: int, height: int) -> np.ndarray:
    """Letterbox an RGBA frame into ``width`` x ``height`` without distortion."""
    require_cv2()
    source_height, source_width = rgba.shape[:2]
    if (source_width, source_height) == (width, height):
        return np.ascontiguousarray(rgba, dtype=np.uint8)

    scale = min(width / source_width, height / source_height)
    fit_width = max(1, min(width, int(round(source_width * scale))))
    fit_height = max(1, min(height, int(round(source_height * scale))))
    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LANCZOS4
    resized = cv2.resize(rgba, (fit_width, fit_height), interpolation=interpolation)

    canvas = np.zeros((height, width, 4), dtype=np.uint8)
    left = (width - fit_width) // 2
    top = (height - fit_height) // 2
    canvas[top : top + fit_height, left : left + fit_width] = resized
    return canvas


def tensor_to_rgba(frame: torch.Tensor) -> np.ndarray:
    """Convert one (H, W, C) float image in 0..1 to a contiguous RGBA8 array."""
    array = frame.detach().to(device="cpu", dtype=torch.float32).clamp(0.0, 1.0).numpy()
    array = np.rint(array * 255.0).astype(np.uint8)
    channels = array.shape[2]
    if channels == 4:
        return np.ascontiguousarray(array)
    if channels == 3:
        height, width = array.shape[:2]
        rgba = np.empty((height, width, 4), dtype=np.uint8)
        rgba[..., :3] = array
        rgba[..., 3] = 255
        return rgba
    if channels == 1:
        return tensor_to_rgba(frame.repeat(1, 1, 3))
    raise ValueError(f"Unsupported image with {channels} channels.")


def rgba_to_tensor(rgba: np.ndarray, *, keep_alpha: bool) -> torch.Tensor:
    """Convert an RGBA8 worker result back to a float image in 0..1."""
    channels = 4 if keep_alpha else 3
    array = np.ascontiguousarray(rgba[..., :channels], dtype=np.float32) / 255.0
    return torch.from_numpy(array)


def resize_alpha(alpha: np.ndarray, width: int, height: int) -> np.ndarray:
    """Scale a source alpha plane onto the DLSS output grid."""
    require_cv2()
    if alpha.shape[:2] == (height, width):
        return alpha
    return cv2.resize(alpha, (width, height), interpolation=cv2.INTER_LINEAR)
