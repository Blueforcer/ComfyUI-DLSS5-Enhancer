"""Map an upstream DLSS 5 release archive onto this pack's flat runtime layout.

The pack drives the standalone protocol-4 worker that upstream used up to
release v6.0: ``bin/runtime/host/nvngx.dll`` beside ``renodx-dlss5.addon64``.
Newer releases changed that contract:

* v7.0 moved the add-on to ``bin/runtime/dlssnr/`` and the worker resolves it
  as ``..\\dlssnr``, so a flat staging directory no longer works.
* v8.0 replaced the standalone worker with the Neuroframe Engine
  (``neuroframe_engine_*.dll``); there is no protocol-4 worker to start.

The installer therefore pins v6.0 and this module keeps the archive layout
knowledge in one testable place.
"""

from __future__ import annotations

from collections.abc import Iterable

# Logical runtime files this pack requires, and the archive paths that can
# provide them. The first match wins. Older releases used a flat layout; v5
# and v6 use host/ and dlss/ subdirectories.
RUNTIME_MEMBERS: dict[str, tuple[str, ...]] = {
    "nvngx.dll": ("host/nvngx.dll", "nvngx.dll"),
    "renodx-dlss5.addon64": (
        "host/renodx-dlss5.addon64",
        "renodx-dlss5.addon64",
    ),
    "nvngx_dlss.dll": ("dlss/nvngx_dlss.dll", "nvngx_dlss.dll"),
    "dxgi.dll": ("host/dxgi.dll", "dxgi.dll"),
}

# v6.0 ships one optimized Neural Rendering runtime per GPU family.
NR_VARIANTS: dict[str, str] = {
    "turing": "dlssnr/Turing+/nvngx_dlssnr.dll",
    "ada": "dlssnr/Ada Lovelace+/nvngx_dlssnr.dll",
    "blackwell": "dlssnr/Blackwell+/nvngx_dlssnr.dll",
}
NR_FALLBACKS: tuple[str, ...] = (
    "host/nvngx_dlssnr.dll",
    "nvngx_dlssnr.dll",
    *NR_VARIANTS.values(),
)
NR_ARCHES: tuple[str, ...] = ("auto", "turing", "ada", "blackwell")


class ArchiveLayoutError(RuntimeError):
    """Raised when an archive has no layout this pack can drive."""


def _relative_name(name: str) -> str:
    """Normalize an archive path relative to the release's runtime root."""
    normalized = name.replace("\\", "/").lstrip("/")
    marker = "bin/runtime/"
    index = normalized.lower().find(marker)
    if index >= 0:
        normalized = normalized[index + len(marker) :]
    return normalized


def normalize_members(names: Iterable[str]) -> dict[str, str]:
    """Index archive entries by their runtime-relative name.

    The value keeps the original archive name so extraction can open the
    exact ``ZipInfo``.
    """
    members: dict[str, str] = {}
    for name in names:
        relative = _relative_name(name)
        if relative:
            members[relative] = name
    return members


def select_nr_arch(gpu_name: str | None) -> str | None:
    """Pick a v6.0 Neural Rendering build from an nvidia-smi GPU name."""
    if not gpu_name:
        return None
    name = gpu_name.upper()
    if "RTX 50" in name or "BLACKWELL" in name:
        return "blackwell"
    if "RTX 40" in name or "ADA" in name:
        return "ada"
    if any(token in name for token in ("RTX 20", "RTX 30", "TURING", "AMPERE")):
        return "turing"
    return None


def _first_present(
    members: dict[str, str], candidates: Iterable[str]
) -> str | None:
    for candidate in candidates:
        if candidate in members:
            return candidate
    return None


def select_runtime_members(
    names: Iterable[str],
    *,
    nr_arch: str = "auto",
    gpu_name: str | None = None,
) -> dict[str, str]:
    """Return ``{logical file: archive member}`` for a supported release.

    Raises :class:`ArchiveLayoutError` with an actionable message when the
    archive is a v7+ layout or does not contain every file the worker needs.
    """
    if nr_arch not in NR_ARCHES:
        raise ValueError(f"Unknown NR architecture: {nr_arch}")

    members = normalize_members(names)
    if "host/nvngx.dll" not in members and "nvngx.dll" not in members:
        raise ArchiveLayoutError(
            "This archive has no standalone DLSS 5 worker "
            "(bin/runtime/host/nvngx.dll). Release v8.0 and newer ship the "
            "Neuroframe Engine instead, which this pack cannot drive. "
            "Install release v6.0."
        )
    if (
        "host/renodx-dlss5.addon64" not in members
        and "dlssnr/renodx-dlss5.addon64" in members
    ):
        raise ArchiveLayoutError(
            "This is a v7.0+ layout: the ReShade add-on lives in "
            "bin/runtime/dlssnr/ and the worker resolves it as '..\\\\dlssnr'. "
            "That requires a different staging directory. Install release "
            "v6.0, the newest release this pack can drive."
        )

    selected: dict[str, str] = {}
    missing: list[str] = []
    for target, candidates in RUNTIME_MEMBERS.items():
        source = _first_present(members, candidates)
        if source is None:
            missing.append(f"{target} ({' or '.join(candidates)})")
        else:
            selected[target] = members[source]

    if nr_arch == "auto":
        detected = select_nr_arch(gpu_name)
        nr_candidates = (
            (NR_VARIANTS[detected], *NR_FALLBACKS)
            if detected
            else NR_FALLBACKS
        )
    else:
        nr_candidates = (NR_VARIANTS[nr_arch], *NR_FALLBACKS)
    neural = _first_present(members, nr_candidates)
    if neural is None:
        missing.append(
            "nvngx_dlssnr.dll (" + " or ".join(nr_candidates) + ")"
        )
    else:
        selected["nvngx_dlssnr.dll"] = members[neural]

    if missing:
        raise ArchiveLayoutError(
            "The archive is missing runtime files this pack needs:\n  "
            + "\n  ".join(missing)
            + "\nInstall the v6.0 release, which contains the standalone "
            "host worker and its architecture-specific runtimes."
        )
    return selected


def selected_nr_arch(
    selected: dict[str, str],
    *,
    nr_arch: str = "auto",
    gpu_name: str | None = None,
) -> str:
    """Describe the NR build a selection used, for the installer's output."""
    name = _relative_name(selected["nvngx_dlssnr.dll"])
    for arch, member in NR_VARIANTS.items():
        if member == name:
            return arch
    if nr_arch != "auto":
        return nr_arch
    return select_nr_arch(gpu_name) or "host default"
