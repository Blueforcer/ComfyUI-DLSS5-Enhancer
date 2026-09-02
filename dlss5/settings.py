"""User-facing DLSS 5 controls and their translation to the worker protocol."""

from __future__ import annotations

import math
from dataclasses import dataclass

NR_PRESETS: dict[str, int] = {
    "Default": 0,
    "Preset #1": 1,
    "Preset #2": 2,
    "Preset #3": 3,
}

NR_STYLES: dict[str, int] = {
    "Default": 0,
    "Natural": 1,
    "Cinematic": 2,
}

DLSS_MODEL_PRESETS: dict[str, int] = {
    "Default": 0,
    "J": 10,
    "K": 11,
    "L": 12,
    "M": 13,
}

# Factor -> DLSS performance/quality mode understood by the worker.
UPSCALING_MODES: dict[float, dict[str, str | int | float]] = {
    1.0: {"label": "1x (DLAA / native)", "name": "DLAA", "perf_quality": 5},
    1.5: {"label": "1.5x (Quality)", "name": "Quality", "perf_quality": 2},
    1.724: {"label": "1.724x (Balanced)", "name": "Balanced", "perf_quality": 1},
    2.0: {"label": "2x (Performance)", "name": "Performance", "perf_quality": 0},
    3.0: {"label": "3x (Ultra Performance)", "name": "Ultra Performance", "perf_quality": 3},
}

UPSCALING_LABELS: list[str] = [str(mode["label"]) for mode in UPSCALING_MODES.values()]
_LABEL_TO_FACTOR: dict[str, float] = {
    str(mode["label"]): factor for factor, mode in UPSCALING_MODES.items()
}

MOTION_MODES = ["auto", "optical_flow", "none"]

MAX_LONG_EDGE = 7680
MAX_SHORT_EDGE = 4320

_CONTROL_RANGES = {
    "nr_intensity": (0.0, 2.0),
    "local_tone_strength": (0.0, 2.0),
    "local_structure_strength": (0.0, 2.0),
    "skin_structure_strength": (-1.0, 2.0),
}


def resolve_upscaling(label_or_factor: str | float) -> tuple[float, dict]:
    """Accept either a combo label or a raw factor and return (factor, mode)."""
    if isinstance(label_or_factor, str):
        try:
            factor = _LABEL_TO_FACTOR[label_or_factor]
        except KeyError as exc:
            choices = ", ".join(UPSCALING_LABELS)
            raise ValueError(
                f"Unknown upscaling mode {label_or_factor!r}. Choose one of: {choices}."
            ) from exc
        return factor, UPSCALING_MODES[factor]

    value = float(label_or_factor)
    for factor, mode in UPSCALING_MODES.items():
        if math.isclose(value, factor, rel_tol=0.0, abs_tol=1e-9):
            return factor, mode
    choices = ", ".join(f"{factor:g}x" for factor in UPSCALING_MODES)
    raise ValueError(f"Unsupported upscaling factor {value:g}x. Choose one of: {choices}.")


def _even(value: float) -> int:
    return max(2, int(math.floor(value / 2.0 + 0.5)) * 2)


def resolve_output_size(width: int, height: int, factor: float) -> tuple[int, int]:
    """Scale the source size to the DLSS output size, staying inside 8K limits."""
    output_width = _even(int(width) * factor)
    output_height = _even(int(height) * factor)
    if max(output_width, output_height) > MAX_LONG_EDGE or min(output_width, output_height) > MAX_SHORT_EDGE:
        usable = [
            candidate
            for candidate in UPSCALING_MODES
            if max(_even(width * candidate), _even(height * candidate)) <= MAX_LONG_EDGE
            and min(_even(width * candidate), _even(height * candidate)) <= MAX_SHORT_EDGE
        ]
        hint = (
            f" Use {max(usable):g}x or lower for this source."
            if usable
            else " The source alone already exceeds the supported boundary."
        )
        raise ValueError(
            f"A {output_width}x{output_height} output exceeds the supported "
            f"{MAX_LONG_EDGE}x{MAX_SHORT_EDGE} boundary.{hint}"
        )
    return output_width, output_height


@dataclass(frozen=True, slots=True)
class DlssOptions:
    """Validated neural-rendering controls shared by every processing node."""

    upscaling_factor: float = 1.0
    nr_preset: str = "Default"
    nr_style: str = "Default"
    dlss_model_preset: str = "Default"
    nr_intensity: float = 1.0
    local_tone_strength: float = 1.0
    local_structure_strength: float = 1.0
    skin_structure_strength: float = -1.0
    automatic_mask: bool = False
    warmup_frames: int = 0
    motion_mode: str = "auto"
    scene_change_threshold: float = 0.24
    flow_width: int = 640

    @classmethod
    def create(cls, **values) -> "DlssOptions":
        """Build options from widget values, validating every field up front."""
        upscaling = values.pop("upscaling_mode", None)
        if upscaling is not None:
            values["upscaling_factor"] = resolve_upscaling(upscaling)[0]

        for name, table, label in (
            ("nr_preset", NR_PRESETS, "NR Preset"),
            ("nr_style", NR_STYLES, "NR Style"),
            ("dlss_model_preset", DLSS_MODEL_PRESETS, "DLSS Model Preset"),
        ):
            value = values.get(name)
            if value is not None and value not in table:
                raise ValueError(
                    f"Unknown {label} {value!r}. Choose one of: {', '.join(table)}."
                )

        for name, (minimum, maximum) in _CONTROL_RANGES.items():
            if name not in values:
                continue
            value = float(values[name])
            if not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError(
                    f"{name.replace('_', ' ')} must be between {minimum:g} and {maximum:g}."
                )
            values[name] = value

        motion = values.get("motion_mode")
        if motion is not None and motion not in MOTION_MODES:
            raise ValueError(
                f"Unknown motion mode {motion!r}. Choose one of: {', '.join(MOTION_MODES)}."
            )

        options = cls(**values)
        resolve_upscaling(options.upscaling_factor)
        return options

    @property
    def mode(self) -> dict:
        return resolve_upscaling(self.upscaling_factor)[1]

    def native(self) -> dict[str, int | float]:
        """Translate the public controls into the worker's header fields."""
        return {
            "profile": 0,
            "preset": NR_PRESETS[self.nr_preset],
            "style": NR_STYLES[self.nr_style],
            "auto_mask": int(bool(self.automatic_mask)),
            "ui_correction": 0,
            "intensity": self.nr_intensity,
            "local_tone": self.local_tone_strength,
            "local_structure": self.local_structure_strength,
            "skin_structure": self.skin_structure_strength,
            "dlss_model_preset": DLSS_MODEL_PRESETS[self.dlss_model_preset],
        }

    def wants_motion(self, frame_count: int) -> bool:
        if self.motion_mode == "none":
            return False
        if self.motion_mode == "optical_flow":
            return True
        return frame_count > 1


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Everything a processing node needs: the controls plus where the runtime is."""

    options: DlssOptions
    runtime_dir: str = ""

    @property
    def runtime_override(self) -> str | None:
        return self.runtime_dir.strip() or None
