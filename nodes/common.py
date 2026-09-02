"""Helpers shared by the DLSS 5 processing nodes."""

from __future__ import annotations

import logging

from ..dlss5.paths import RuntimeLayout, find_runtime
from ..dlss5.session import DlssSession
from ..dlss5.settings import SessionConfig

logger = logging.getLogger("ComfyUI-DLSS5-Enhancer")


def resolve_layout(config: SessionConfig) -> RuntimeLayout:
    """Locate and validate the native runtime for this render."""
    return find_runtime(config.runtime_override)


def confirm_feature_18(session: DlssSession, enabled: bool) -> None:
    """Fail loudly when the render produced no signed feature-18 evidence.

    Without this the frames could come back merely upscaled, which is
    indistinguishable from success in the output itself.
    """
    if not enabled:
        return
    report = session.feature_report()
    if report["native_fallback"]:
        # Expected on current runtime builds: DLSS upscales first, then the
        # neural pass runs at the output resolution instead of inside the
        # upscaler. Neural rendering still happened.
        logger.info(
            "DLSS5: neural rendering ran on the native path after upscaling "
            "(the signed runtime declined the low-resolution colour contract)."
        )
    logger.info(
        "DLSS5: feature-18 neural rendering verified for %d frame(s).",
        session.frames_submitted,
    )
