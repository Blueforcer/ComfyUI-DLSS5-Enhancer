"""Node that enhances a video file end to end, keeping audio and metadata."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import folder_paths
from comfy_api.latest import io, ui
from comfy.model_management import throw_exception_if_processing_interrupted
from comfy.utils import ProgressBar

from ..dlss5.imaging import fit_frame
from ..dlss5.media import (
    CODECS,
    CONTAINER_EXTENSIONS,
    CONTAINERS,
    QUALITIES,
    RawFrameEncoder,
    decode_frames,
    mux,
    probe_video,
    validate_codec_container,
)
from ..dlss5.motion import TemporalGuide
from ..dlss5.session import DlssSession
from ..dlss5.settings import SessionConfig
from .common import confirm_feature_18, logger, resolve_layout
from .settings_node import DLSS5_SETTINGS

# Memory budget for the two in-flight frame queues.
_QUEUE_BUDGET_BYTES = 384 * 1024 * 1024
# A queue that has not moved for this long means a stage is wedged.
_STALL_TIMEOUT = 600.0
_WRITER_TIMEOUT = 300.0
_STOP = object()

_UNSAFE_PREFIX = set('<>:"/\\|?*')


def _safe_prefix(prefix: str) -> str:
    """Reject a prefix that would traverse directories or look like a CLI flag."""
    cleaned = prefix.strip() or "DLSS5"
    if any(char in _UNSAFE_PREFIX for char in cleaned) or cleaned.startswith("-"):
        raise ValueError(
            f"filename_prefix {prefix!r} may not contain path separators, drive "
            "letters or a leading dash."
        )
    if cleaned in {".", ".."}:
        raise ValueError("filename_prefix must be a file name.")
    return cleaned


def _resolve_directory(raw: str) -> Path:
    """Resolve the output directory, keeping a relative path inside the output dir."""
    output_root = Path(folder_paths.get_output_directory()).resolve()
    value = raw.strip().strip('"').strip("'")
    if not value:
        return output_root
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = output_root / candidate
    return candidate.resolve()


def _unique_output(directory: Path, prefix: str, extension: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = directory / f"{prefix}_{stamp}{extension}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{prefix}_{stamp}_{counter:03d}{extension}"
        counter += 1
    return candidate


class DLSS5EnhanceVideoFile(io.ComfyNode):
    """Stream a video file through the worker without holding it all in memory."""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="DLSS5EnhanceVideoFile",
            display_name="DLSS5 Enhance Video File",
            category="image/upscaling",
            description=(
                "Decodes a video file, runs every frame through NVIDIA DLSS 5 neural "
                "rendering and re-encodes it, keeping the original timestamps, audio, "
                "chapters and metadata. Long videos never enter the workflow as a batch."
            ),
            search_aliases=["dlss", "dlss5", "video", "enhance", "upscale"],
            is_output_node=True,
            not_idempotent=True,
            inputs=[
                io.String.Input(
                    "video_path",
                    default="",
                    tooltip="Absolute path to the source video file.",
                ),
                DLSS5_SETTINGS.Input(
                    "settings",
                    tooltip="Connect a DLSS5 Settings node.",
                ),
                io.Combo.Input(
                    "codec",
                    options=list(CODECS),
                    default="HEVC",
                    tooltip=(
                        "H.264 and HEVC use NVENC with a software fallback. AV1 needs "
                        "NVENC and has no fallback. ProRes Proxy encodes in software."
                    ),
                ),
                io.Combo.Input(
                    "container",
                    options=list(CONTAINERS),
                    default="MKV",
                    tooltip=(
                        "MKV stream copies audio and subtitles. MP4 and MOV re-encode "
                        "audio to AAC and drop subtitles. ProRes needs MOV or MKV."
                    ),
                ),
                io.Combo.Input(
                    "quality",
                    options=list(QUALITIES),
                    default="Auto",
                    tooltip=(
                        "Auto derives a bitrate from resolution and frame rate, Good "
                        "doubles it, Best quadruples it, Max encodes at constant "
                        "quality. Ignored for ProRes Proxy."
                    ),
                ),
                io.String.Input(
                    "filename_prefix",
                    default="DLSS5",
                    tooltip="Prefix of the output file; a timestamp is appended.",
                ),
                io.String.Input(
                    "output_directory",
                    default="",
                    tooltip=(
                        "Empty writes to the ComfyUI output directory. A relative path "
                        "is resolved inside it."
                    ),
                ),
                io.Int.Input(
                    "max_frames",
                    default=0,
                    min=0,
                    max=1_000_000,
                    tooltip="0 renders the whole video; any other value renders a preview of that many frames.",
                ),
                io.Boolean.Input(
                    "copy_audio",
                    default=True,
                    tooltip="Mux the original audio, subtitles and chapters into the result.",
                ),
                io.Boolean.Input(
                    "verify_neural_rendering",
                    default=True,
                    tooltip=(
                        "Check the ReShade log for signed feature-18 execution after "
                        "the render. The file is written either way."
                    ),
                    advanced=True,
                ),
            ],
            outputs=[
                io.String.Output("video_path"),
                io.Int.Output("frames"),
            ],
        )

    @classmethod
    def fingerprint_inputs(cls, video_path: str = "", **kwargs):
        """Re-run when the source file changed, so a second Run is never a no-op."""
        try:
            status = Path(video_path.strip().strip('"')).stat()
        except OSError:
            return float("nan")
        return (status.st_mtime_ns, status.st_size)

    @classmethod
    def execute(
        cls,
        video_path: str,
        settings: SessionConfig,
        codec: str,
        container: str,
        quality: str,
        filename_prefix: str,
        output_directory: str,
        max_frames: int,
        copy_audio: bool,
        verify_neural_rendering: bool = True,
    ) -> io.NodeOutput:
        if not video_path.strip():
            raise ValueError(
                "video_path is empty. Enter the absolute path of the source video file."
            )
        source = Path(video_path.strip().strip('"').strip("'")).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"No video file at {source}")
        validate_codec_container(codec, container)

        layout = resolve_layout(settings)
        options = settings.options
        metadata = probe_video(layout, source)
        if metadata["hdr"]:
            logger.warning(
                "DLSS5: %s is HDR. The neural renderer works on 8 bit SDR, so the "
                "result is tone mapped to SDR.",
                source.name,
            )

        frame_count = int(metadata["frames"])
        if frame_count <= 0:
            logger.info(
                "DLSS5: %s carries no frame count, counting frames. This decodes the "
                "whole file once and can take a while.",
                source.name,
            )
            frame_count = int(probe_video(layout, source, exact_frames=True)["frames"])
        if frame_count <= 0:
            raise RuntimeError(f"Could not determine a frame count for {source}.")
        if max_frames:
            frame_count = min(frame_count, int(max_frames))

        final_path = _unique_output(
            _resolve_directory(output_directory),
            _safe_prefix(filename_prefix),
            CONTAINER_EXTENSIONS[container],
        )
        rendered_path = _unique_output(
            Path(folder_paths.get_temp_directory()), f"{final_path.stem}_raw", ".mkv"
        )

        try:
            written, session = cls._render(
                layout=layout,
                options=options,
                source=source,
                rendered_path=rendered_path,
                metadata=metadata,
                frame_count=frame_count,
                codec=codec,
                quality=quality,
            )
            try:
                mux(layout, rendered_path, source, final_path, container, copy_audio=copy_audio)
            except BaseException:
                final_path.unlink(missing_ok=True)
                raise
        except BaseException:
            rendered_path.unlink(missing_ok=True)
            raise
        rendered_path.unlink(missing_ok=True)

        # Verified only after the file exists, so a verification failure never
        # throws away a finished render.
        confirm_feature_18(session, verify_neural_rendering)

        logger.info("DLSS5: wrote %d frames to %s", written, final_path)
        return io.NodeOutput(
            str(final_path), written, ui=ui.PreviewText(f"{final_path} ({written} frames)")
        )

    @classmethod
    def _render(
        cls,
        *,
        layout,
        options,
        source: Path,
        rendered_path: Path,
        metadata: dict,
        frame_count: int,
        codec: str,
        quality: str,
    ):
        """Decode, render and encode concurrently. Returns (frames, session)."""
        with DlssSession(
            layout,
            options,
            input_width=metadata["width"],
            input_height=metadata["height"],
            frame_count=frame_count,
        ) as session:
            encoder = None
            producer = None
            writer = None
            stop = threading.Event()
            errors: queue.Queue = queue.Queue()

            try:
                encoder = RawFrameEncoder(
                    layout,
                    rendered_path,
                    codec=codec,
                    quality=quality,
                    width=session.output_width,
                    height=session.output_height,
                    rate=metadata["rate"],
                    time_base=metadata["time_base"],
                )

                prepared_bytes = session.render_width * session.render_height * 8
                rendered_bytes = session.output_width * session.output_height * 4
                slots = max(
                    1, min(3, _QUEUE_BUDGET_BYTES // max(1, prepared_bytes + rendered_bytes))
                )
                prepared: queue.Queue = queue.Queue(maxsize=slots)
                finished: queue.Queue = queue.Queue(maxsize=slots)

                def offer(target: queue.Queue, item, interruptible: bool) -> bool:
                    """Hand an item on, staying responsive to cancellation."""
                    deadline = time.monotonic() + _STALL_TIMEOUT
                    while not stop.is_set():
                        if interruptible:
                            throw_exception_if_processing_interrupted()
                        try:
                            target.put(item, timeout=0.1)
                            return True
                        except queue.Full:
                            if time.monotonic() > deadline:
                                raise RuntimeError(
                                    "A pipeline stage stopped consuming frames for "
                                    f"{_STALL_TIMEOUT:.0f} seconds."
                                )
                    return False

                def produce() -> None:
                    try:
                        guide = TemporalGuide(
                            session.render_width,
                            session.render_height,
                            flow_width=options.flow_width,
                            scene_change_threshold=options.scene_change_threshold,
                            enabled=options.wants_motion(frame_count),
                        )
                        for index, rgba, pts in decode_frames(
                            source, metadata["rotation"], limit=frame_count
                        ):
                            if stop.is_set():
                                return
                            fitted = fit_frame(
                                rgba, session.render_width, session.render_height
                            )
                            if not offer(
                                prepared, (index, fitted, guide.process(fitted), pts), False
                            ):
                                return
                    except BaseException as exc:  # surfaced on the main thread
                        errors.put(exc)
                        stop.set()
                    finally:
                        offer(prepared, _STOP, False)

                def consume() -> None:
                    try:
                        while True:
                            try:
                                item = finished.get(timeout=0.1)
                            except queue.Empty:
                                if stop.is_set():
                                    return
                                continue
                            if item is _STOP:
                                return
                            encoder.write(*item)
                    except BaseException as exc:
                        errors.put(exc)
                        stop.set()

                producer = threading.Thread(target=produce, name="dlss5-decode", daemon=True)
                writer = threading.Thread(target=consume, name="dlss5-encode", daemon=True)
                producer.start()
                writer.start()

                progress = ProgressBar(frame_count)
                written = 0
                while True:
                    throw_exception_if_processing_interrupted()
                    if not errors.empty():
                        raise errors.get()
                    try:
                        item = prepared.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if item is _STOP:
                        break
                    index, rgba, guide, pts = item
                    enhanced, out_pts = session.submit(
                        index=index,
                        rgba=rgba,
                        motion=guide.motion,
                        reset=guide.reset,
                        pts=pts,
                    )
                    if not offer(finished, (enhanced, out_pts), True):
                        break
                    written += 1
                    progress.update(1)

                offer(finished, _STOP, True)
                writer.join(timeout=_WRITER_TIMEOUT)
                if writer.is_alive():
                    raise RuntimeError(
                        "The encoder did not finish flushing; the output would be "
                        "truncated."
                    )
                producer.join(timeout=30)
                if not errors.empty():
                    raise errors.get()
                if written != frame_count:
                    raise RuntimeError(
                        f"Only {written} of the expected {frame_count} frames were "
                        f"rendered from {source.name}; the output would be truncated."
                    )
                encoder.close()
            except BaseException:
                # Join the writer before touching the encoder: while it runs it
                # owns the PyAV container.
                stop.set()
                if writer is not None:
                    writer.join(timeout=30)
                if producer is not None:
                    producer.join(timeout=10)
                if encoder is not None:
                    encoder.abort()
                raise

            return written, session
