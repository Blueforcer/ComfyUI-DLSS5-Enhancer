"""Node that runs an IMAGE batch through the DLSS 5 neural renderer."""

from __future__ import annotations

import torch
from comfy_api.latest import io
from comfy.model_management import throw_exception_if_processing_interrupted
from comfy.utils import ProgressBar

from ..dlss5.imaging import fit_frame, resize_alpha, rgba_to_tensor, tensor_to_rgba
from ..dlss5.motion import TemporalGuide
from ..dlss5.session import DlssSession
from ..dlss5.settings import SessionConfig
from .common import confirm_feature_18, resolve_layout
from .settings_node import DLSS5_SETTINGS


class DLSS5EnhanceImages(io.ComfyNode):
    """Enhance a frame batch; the batch order is the temporal order."""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="DLSS5EnhanceImages",
            display_name="DLSS5 Enhance Images",
            category="image/upscaling",
            description=(
                "Runs every image through NVIDIA DLSS 5 neural rendering (NGX feature 18) "
                "on the native worker. Feed video frames in playback order so temporal "
                "accumulation works."
            ),
            search_aliases=["dlss", "dlss5", "neural rendering", "enhance", "upscale"],
            inputs=[
                io.Image.Input("images"),
                DLSS5_SETTINGS.Input("settings"),
                io.Boolean.Input(
                    "verify_neural_rendering",
                    default=True,
                    tooltip=(
                        "Fail the render when the ReShade log shows no signed feature-18 "
                        "execution, instead of silently returning plain upscaled frames."
                    ),
                    advanced=True,
                ),
            ],
            outputs=[io.Image.Output("images")],
        )

    @classmethod
    def execute(
        cls,
        images: torch.Tensor,
        settings: SessionConfig,
        verify_neural_rendering: bool = True,
    ) -> io.NodeOutput:
        layout = resolve_layout(settings)
        options = settings.options

        count, height, width, channels = images.shape
        keep_alpha = channels == 4

        with DlssSession(
            layout,
            options,
            input_width=width,
            input_height=height,
            frame_count=count,
        ) as session:
            guide = TemporalGuide(
                session.render_width,
                session.render_height,
                flow_width=options.flow_width,
                scene_change_threshold=options.scene_change_threshold,
                enabled=options.wants_motion(count),
            )
            result = torch.empty(
                (count, session.output_height, session.output_width, 4 if keep_alpha else 3),
                dtype=torch.float32,
            )
            progress = ProgressBar(count)

            for index in range(count):
                throw_exception_if_processing_interrupted()
                rgba = fit_frame(
                    tensor_to_rgba(images[index]),
                    session.render_width,
                    session.render_height,
                )
                motion = guide.process(rgba)
                enhanced, _pts = session.submit(
                    index=index,
                    rgba=rgba,
                    motion=motion.motion,
                    reset=motion.reset,
                    pts=index,
                )
                if keep_alpha:
                    # DLSS only reconstructs colour, so carry the source alpha over.
                    enhanced[..., 3] = resize_alpha(
                        rgba[..., 3], session.output_width, session.output_height
                    )
                result[index] = rgba_to_tensor(enhanced, keep_alpha=keep_alpha)
                progress.update(1)

        # ReShade writes its log out as the worker exits, so the proof of
        # feature-18 execution is only readable once the session is closed.
        confirm_feature_18(session, verify_neural_rendering)
        return io.NodeOutput(result)
