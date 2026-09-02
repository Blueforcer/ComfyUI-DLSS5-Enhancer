"""Client for the native DLSS 5 neural-rendering (NGX feature 18) worker."""

from .paths import RuntimeLayout, RuntimeMissing, find_runtime, write_config
from .session import DlssSession
from .settings import (
    DLSS_MODEL_PRESETS,
    MOTION_MODES,
    NR_PRESETS,
    NR_STYLES,
    UPSCALING_LABELS,
    DlssOptions,
    SessionConfig,
    resolve_output_size,
    resolve_upscaling,
)

__all__ = [
    "DLSS_MODEL_PRESETS",
    "DlssOptions",
    "DlssSession",
    "MOTION_MODES",
    "NR_PRESETS",
    "NR_STYLES",
    "RuntimeLayout",
    "RuntimeMissing",
    "SessionConfig",
    "UPSCALING_LABELS",
    "find_runtime",
    "resolve_output_size",
    "resolve_upscaling",
    "write_config",
]
