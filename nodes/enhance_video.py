"""Node that enhances a video file end to end, keeping audio and metadata."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import folder_paths
from comfy_api.latest import io
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
_STOP = object()


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
            inputs=[
                io.String.Input(
                    "video_path",
                    default="",
                    tooltip="Absolute path to the source video file.",
                ),
                DLSS5_SETTINGS.Input("settings"),
                io.Combo.Input("codec", options=list(CODECS), default="HEVC"),
                io.Combo.Input("container", options=list(CONTAINERS), default="MKV"),
                io.Combo.Input(
                    "quality",
                    options=list(QUALITIES),
                    default="Auto",
                    tooltip="Auto picks a bitrate from resolution and frame rate; Good/Best multiply it; Max encodes at constant quality.",
                ),
                io.String.Input(
                    "filename_prefix",
                    default="DLSS5",
                    tooltip="Prefix of the output file; a timestamp is appended.",
                ),
                io.String.Input(
                    "output_directory",
                    default="",
                    tooltip="Empty writes to the ComfyUI output directory.",
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
                    tooltip="Fail when the ReShade log shows no signed feature-18 execution.",
                    advanced=True,
                ),
            ],
            outputs=[
                io.String.Output("video_path"),
                io.Int.Output("frames"),
            ],
        )

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
        source = Path(video_path.strip().strip('"')).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"No video file at {source}")
        validate_codec_container(codec, container)

        layout = resolve_layout(settings)
        options = settings.options
        metadata = probe_video(layout, source)
        frame_count = int(metadata["frames"])
        if frame_count <= 0:
            frame_count = int(probe_video(layout, source, exact_frames=True)["frames"])
        if frame_count <= 0:
            raise RuntimeError(f"Could not determine a frame count for {source}.")
        if max_frames:
            frame_count = min(frame_count, int(max_frames))

        destination = Path(output_directory.strip()) if output_directory.strip() else Path(
            folder_paths.get_output_directory()
        )
        final_path = _unique_output(
            destination, filename_prefix.strip() or "DLSS5", CONTAINER_EXTENSIONS[container]
        )
        rendered_path = _unique_output(
            Path(folder_paths.get_temp_directory()), f"{final_path.stem}_raw", ".mkv"
        )

        written = cls._render(
            layout=layout,
            options=options,
            source=source,
            rendered_path=rendered_path,
            metadata=metadata,
            frame_count=frame_count,
            codec=codec,
            quality=quality,
            verify=verify_neural_rendering,
        )

        try:
            mux(layout, rendered_path, source, final_path, container, copy_audio=copy_audio)
        finally:
            rendered_path.unlink(missing_ok=True)

        logger.info("DLSS5: wrote %d frames to %s", written, final_path)
        return io.NodeOutput(str(final_path), written)

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
        verify: bool,
    ) -> int:
        """Decode, render and encode concurrently; returns the frames written."""
        with DlssSession(
            layout,
            options,
            input_width=metadata["width"],
            input_height=metadata["height"],
            frame_count=frame_count,
        ) as session:
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
            slots = max(1, min(3, _QUEUE_BUDGET_BYTES // max(1, prepared_bytes + rendered_bytes)))
            prepared: queue.Queue = queue.Queue(maxsize=slots)
            finished: queue.Queue = queue.Queue(maxsize=slots)
            stop = threading.Event()
            errors: queue.Queue = queue.Queue()

            def offer(target: queue.Queue, item) -> bool:
                while not stop.is_set():
                    try:
                        target.put(item, timeout=0.1)
                        return True
                    except queue.Full:
                        continue
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
                        fitted = fit_frame(rgba, session.render_width, session.render_height)
                        if not offer(prepared, (index, fitted, guide.process(fitted), pts)):
                            return
                except BaseException as exc:  # surfaced on the main thread
                    errors.put(exc)
                finally:
                    offer(prepared, _STOP)

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
            try:
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
                    if not offer(finished, (enhanced, out_pts)):
                        break
                    written += 1
                    progress.update(1)

                offer(finished, _STOP)
                writer.join(timeout=300)
                producer.join(timeout=30)
                if not errors.empty():
                    raise errors.get()
                encoder.close()
            except BaseException:
                # Join the writer before touching the encoder: while it runs it
                # owns the PyAV container.
                stop.set()
                writer.join(timeout=30)
                producer.join(timeout=10)
                encoder.abort()
                raise

            if written == 0:
                raise RuntimeError(f"No frames were decoded from {source}.")

        # The ReShade log is only complete once the worker has exited, so the
        # feature-18 proof is read after the session closed.
        confirm_feature_18(session, verify)
        return written
