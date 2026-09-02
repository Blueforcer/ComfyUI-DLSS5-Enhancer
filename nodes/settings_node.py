"""Node that collects the DLSS 5 neural-rendering controls."""

from __future__ import annotations

from comfy_api.latest import io

from ..dlss5.settings import (
    DLSS_MODEL_PRESETS,
    MOTION_MODES,
    NR_PRESETS,
    NR_STYLES,
    UPSCALING_LABELS,
    DlssOptions,
    SessionConfig,
)

DLSS5_SETTINGS = io.Custom("DLSS5_SETTINGS")


class DLSS5SettingsNode(io.ComfyNode):
    """One settings node feeds both the image and the video enhancer."""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="DLSS5Settings",
            display_name="DLSS5 Settings",
            category="image/upscaling",
            description=(
                "Neural-rendering controls for the NVIDIA DLSS 5 worker. "
                "Connect to DLSS5 Enhance Images or DLSS5 Enhance Video File."
            ),
            search_aliases=["dlss", "dlss5", "neural rendering", "nvidia"],
            inputs=[
                io.Combo.Input(
                    "upscaling_mode",
                    options=UPSCALING_LABELS,
                    default=UPSCALING_LABELS[0],
                    tooltip=(
                        "DLSS mode. 1x (DLAA) enhances at the source resolution; the "
                        "other modes also upscale by that factor."
                    ),
                ),
                io.Combo.Input(
                    "nr_preset",
                    options=list(NR_PRESETS),
                    default="Default",
                    tooltip=(
                        "Neural rendering preset inside the DLSS model. Measured to "
                        "have no effect on current runtime builds."
                    ),
                ),
                io.Combo.Input(
                    "nr_style",
                    options=list(NR_STYLES),
                    default="Default",
                    tooltip="Look of the neural pass: Natural stays closer to the source, Cinematic is stronger.",
                ),
                io.Float.Input(
                    "nr_intensity",
                    default=1.0,
                    min=0.0,
                    max=2.0,
                    step=0.01,
                    tooltip=(
                        "Overall strength of the neural rendering pass. Measured on "
                        "current runtime builds, values above 1.0 have no further "
                        "effect; use below 1.0 to blend back towards the source."
                    ),
                ),
                io.Float.Input(
                    "local_tone_strength",
                    default=1.0,
                    min=0.0,
                    max=2.0,
                    step=0.01,
                    tooltip="Local tone mapping strength.",
                ),
                io.Float.Input(
                    "local_structure_strength",
                    default=1.5,
                    min=0.0,
                    max=2.0,
                    step=0.01,
                    tooltip=(
                        "Local detail and structure reconstruction. Higher keeps more "
                        "texture; 1.5 is a good balance for AI generated footage."
                    ),
                ),
                io.Float.Input(
                    "skin_structure_strength",
                    default=-1.0,
                    min=-1.0,
                    max=2.0,
                    step=0.01,
                    tooltip=(
                        "Skin detail strength. -1 keeps the model's own behaviour. "
                        "Measured to have no effect on current runtime builds."
                    ),
                ),
                io.Boolean.Input(
                    "automatic_mask",
                    default=False,
                    tooltip="Let the model mask regions it should not alter.",
                ),
                io.Combo.Input(
                    "dlss_model_preset",
                    options=list(DLSS_MODEL_PRESETS),
                    default="M",
                    tooltip=(
                        "Force a specific DLSS model instead of NVIDIA's choice. M "
                        "keeps noticeably more texture than the default model on AI "
                        "generated footage. Set to Default if the worker reports that "
                        "it applied a different preset."
                    ),
                ),
                io.Combo.Input(
                    "motion",
                    options=MOTION_MODES,
                    default="auto",
                    tooltip=(
                        "Motion vectors for temporal accumulation. auto estimates optical "
                        "flow for sequences and skips it for single images."
                    ),
                    advanced=True,
                ),
                io.Float.Input(
                    "scene_change_threshold",
                    default=0.24,
                    min=0.01,
                    max=1.0,
                    step=0.01,
                    tooltip="Mean luminance change above which temporal history is reset.",
                    advanced=True,
                ),
                io.Int.Input(
                    "warmup_frames",
                    default=0,
                    min=0,
                    max=16,
                    tooltip="Extra frames the worker renders before the first output settles.",
                    advanced=True,
                ),
                io.String.Input(
                    "runtime_dir",
                    default="",
                    tooltip=(
                        "Optional path to a DLSS 5 runtime directory (the folder holding "
                        "nvngx.dll). Empty uses config.json, DLSS5_RUNTIME_DIR or the "
                        "bundled runtime folder."
                    ),
                    advanced=True,
                ),
            ],
            outputs=[DLSS5_SETTINGS.Output("settings")],
        )

    @classmethod
    def execute(
        cls,
        upscaling_mode: str,
        nr_preset: str,
        nr_style: str,
        nr_intensity: float,
        local_tone_strength: float,
        local_structure_strength: float,
        skin_structure_strength: float,
        automatic_mask: bool,
        dlss_model_preset: str,
        motion: str,
        scene_change_threshold: float,
        warmup_frames: int,
        runtime_dir: str,
    ) -> io.NodeOutput:
        options = DlssOptions.create(
            upscaling_mode=upscaling_mode,
            nr_preset=nr_preset,
            nr_style=nr_style,
            dlss_model_preset=dlss_model_preset,
            nr_intensity=nr_intensity,
            local_tone_strength=local_tone_strength,
            local_structure_strength=local_structure_strength,
            skin_structure_strength=skin_structure_strength,
            automatic_mask=bool(automatic_mask),
            warmup_frames=int(warmup_frames),
            motion_mode=motion,
            scene_change_threshold=float(scene_change_threshold),
        )
        return io.NodeOutput(SessionConfig(options=options, runtime_dir=runtime_dir))
