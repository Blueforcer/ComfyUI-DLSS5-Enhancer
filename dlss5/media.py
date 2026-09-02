"""Decoding, encoding and muxing for the file-to-file video node."""

from __future__ import annotations

import json
import math
import subprocess
import threading
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

import av
import numpy as np

from .paths import RuntimeLayout

CODECS = ("H.264", "HEVC", "AV1", "ProRes Proxy")
CONTAINERS = ("MP4", "MKV", "MOV")
QUALITIES = ("Auto", "Good", "Best", "Max")

CONTAINER_EXTENSIONS = {"MP4": ".mp4", "MKV": ".mkv", "MOV": ".mov"}

# Bits per pixel-second divisors that produce the upstream automatic bitrates.
_AUTO_BITRATE_DIVISORS = {"H.264": 165_888, "HEVC": 331_776, "AV1": 414_720}
_QUALITY_MULTIPLIERS = {"Auto": 1, "Good": 2, "Best": 4}


def validate_codec_container(codec: str, container: str) -> None:
    if codec not in CODECS:
        raise ValueError(f"Unknown codec {codec!r}. Choose one of: {', '.join(CODECS)}.")
    if container not in CONTAINERS:
        raise ValueError(
            f"Unknown container {container!r}. Choose one of: {', '.join(CONTAINERS)}."
        )
    if codec == "ProRes Proxy" and container == "MP4":
        raise ValueError("ProRes Proxy needs the MOV or MKV container.")


def _run_json(command: list[str]) -> dict:
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Media probe failed.")
    return json.loads(result.stdout)


def probe_video(layout: RuntimeLayout, path: Path, *, exact_frames: bool = False) -> dict[str, Any]:
    """Read stream geometry, timing and frame count, honouring display rotation."""
    data = _run_json(
        [
            str(layout.ffprobe),
            "-v",
            "error",
            *(["-count_frames"] if exact_frames else []),
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate,r_frame_rate,time_base,duration,"
            "nb_frames,nb_read_frames,codec_name,color_transfer:stream_tags=rotate:"
            "stream_side_data=rotation",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    streams = data.get("streams") or []
    if not streams:
        raise ValueError(f"{path} contains no decodable video stream.")
    stream = streams[0]

    rotation = int((stream.get("tags") or {}).get("rotate", 0) or 0)
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            rotation = int(side["rotation"] or 0)
    rotation %= 360

    width, height = int(stream["width"]), int(stream["height"])
    if rotation in (90, 270):
        width, height = height, width

    if exact_frames:
        frames = int(stream.get("nb_read_frames") or stream.get("nb_frames") or 0)
    else:
        frames = int(stream.get("nb_frames") or 0)

    rate_text = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "30/1"
    rate = Fraction(rate_text) if rate_text not in {"0/0", None} else Fraction(30, 1)
    transfer = stream.get("color_transfer") or "unknown"

    return {
        "width": width,
        "height": height,
        "rotation": rotation,
        "frames": frames,
        "fps": float(rate),
        "rate": rate,
        "time_base": Fraction(stream.get("time_base") or "1/1000"),
        "duration": float(
            (data.get("format") or {}).get("duration") or stream.get("duration") or 0
        ),
        "codec": stream.get("codec_name") or "unknown",
        "hdr": transfer in {"smpte2084", "arib-std-b67"},
    }


def rotate_frame(frame: np.ndarray, rotation: int) -> np.ndarray:
    """Apply container rotation metadata so the render matches what players show."""
    if rotation == 90:
        return np.ascontiguousarray(np.rot90(frame, 3))
    if rotation == 180:
        return np.ascontiguousarray(np.rot90(frame, 2))
    if rotation == 270:
        return np.ascontiguousarray(np.rot90(frame, 1))
    return frame


def decode_frames(path: Path, rotation: int, limit: int = 0) -> Iterator[tuple[int, np.ndarray, int]]:
    """Yield ``(index, rgba, pts)`` for up to ``limit`` frames (0 = all)."""
    container = av.open(str(path))
    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        for index, frame in enumerate(container.decode(stream)):
            if limit and index >= limit:
                break
            rgba = rotate_frame(frame.to_ndarray(format="rgba"), rotation)
            pts = int(frame.pts if frame.pts is not None else index)
            yield index, rgba, pts
    finally:
        container.close()


def auto_bitrate_kbps(width: int, height: int, fps: float, codec: str) -> int:
    divisor = _AUTO_BITRATE_DIVISORS[codec]
    if width <= 0 or height <= 0 or not math.isfinite(fps) or fps <= 0:
        raise ValueError("Automatic bitrate needs positive dimensions and frame rate.")
    return max(1, int(math.floor(width * height * fps * 8 * 2 / divisor + 0.5)))


def resolve_quality(quality: str, codec: str, width: int, height: int, fps: float) -> dict:
    """Translate the quality label into encoder rate-control settings."""
    if quality not in QUALITIES:
        raise ValueError(f"Unknown quality {quality!r}. Choose one of: {', '.join(QUALITIES)}.")
    if codec == "ProRes Proxy":
        return {"selection": quality, "mode": "fixed-prores-proxy-profile"}
    if quality == "Max":
        return {"selection": quality, "mode": "constant-quality"}
    bitrate = auto_bitrate_kbps(width, height, fps, codec) * _QUALITY_MULTIPLIERS[quality]
    return {"selection": quality, "mode": "target-bitrate", "target_bitrate_kbps": bitrate}


@lru_cache(maxsize=64)
def _encoder_available(ffmpeg: str, encoder: str, width: int, height: int) -> bool:
    """Check that this encoder can really take the requested size on this GPU."""
    command = [
        ffmpeg,
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=size={width}x{height}:rate=1",
        "-frames:v",
        "1",
        "-c:v",
        encoder,
        "-f",
        "null",
        "-",
    ]
    return (
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        == 0
    )


def _codec_arguments(
    layout: RuntimeLayout, codec: str, quality: dict, width: int, height: int
) -> tuple[list[str], str]:
    if codec == "ProRes Proxy":
        return ["-c:v", "prores_ks", "-profile:v", "0", "-pix_fmt", "yuv422p10le"], "prores_ks"

    if quality["mode"] == "constant-quality":
        nvenc_rate = ["-rc", "vbr", "-cq", "0", "-b:v", "0"]
        software_rate = ["-crf", "0"]
    else:
        bitrate = f"{quality['target_bitrate_kbps']}k"
        nvenc_rate = ["-rc", "vbr", "-b:v", bitrate]
        software_rate = ["-b:v", bitrate]

    ffmpeg = str(layout.ffmpeg)
    plans = {
        "H.264": ("h264_nvenc", "libx264"),
        "HEVC": ("hevc_nvenc", "libx265"),
        "AV1": ("av1_nvenc", None),
    }
    hardware, software = plans[codec]
    if _encoder_available(ffmpeg, hardware, width, height):
        arguments = ["-c:v", hardware, "-preset", "p6", *nvenc_rate, "-pix_fmt", "yuv420p"]
        if codec != "AV1":
            arguments[4:4] = ["-tune", "hq"]
        return arguments, hardware
    if software is None:
        raise RuntimeError(
            f"AV1 NVENC cannot encode {width}x{height} on this GPU/driver. "
            "Choose H.264 or HEVC, or a lower upscaling mode."
        )
    return (
        ["-c:v", software, "-preset", "slow", *software_rate, "-pix_fmt", "yuv420p"],
        software,
    )


class RawFrameEncoder:
    """ffmpeg fed with raw RGBA frames through a NUT stream on stdin."""

    def __init__(
        self,
        layout: RuntimeLayout,
        target: Path,
        *,
        codec: str,
        quality: str,
        width: int,
        height: int,
        rate: Fraction,
        time_base: Fraction,
    ) -> None:
        self.quality = resolve_quality(quality, codec, width, height, float(rate))
        arguments, self.encoder_name = _codec_arguments(
            layout, codec, self.quality, width, height
        )
        command = [
            str(layout.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-f",
            "nut",
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-an",
            *arguments,
            "-fps_mode",
            "passthrough",
            str(target),
        ]
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        self.logs: list[str] = []
        # PyAV containers are not thread safe: the writer thread and a cancelling
        # main thread must never touch this one at the same time.
        self._lock = threading.Lock()
        self._finished = False
        self._log_thread = threading.Thread(
            target=self._drain, name="dlss5-encoder-log", daemon=True
        )
        self._log_thread.start()

        self._container = av.open(self.process.stdin, mode="w", format="nut")
        self._stream = self._container.add_stream("rawvideo", rate=rate)
        self._stream.width = width
        self._stream.height = height
        self._stream.pix_fmt = "rgba"
        self._stream.time_base = time_base
        self._time_base = time_base

    def _drain(self) -> None:
        for raw in iter(self.process.stderr.readline, b""):
            self.logs.append(raw.decode("utf-8", errors="replace").rstrip())
        self.process.stderr.close()

    def write(self, rgba: np.ndarray, pts: int) -> None:
        frame = av.VideoFrame.from_ndarray(rgba, format="rgba")
        frame.pts = pts
        frame.time_base = self._time_base
        with self._lock:
            if self._finished:
                return
            for packet in self._stream.encode(frame):
                self._container.mux(packet)

    def close(self) -> None:
        """Flush the encoder and require a clean ffmpeg exit."""
        with self._lock:
            if self._finished:
                return
            self._finished = True
            for packet in self._stream.encode(None):
                self._container.mux(packet)
            self._container.close()
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        code = self.process.wait(timeout=120)
        self._log_thread.join(timeout=5)
        if code:
            raise RuntimeError("ffmpeg encoding failed:\n" + "\n".join(self.logs[-40:]))

    def abort(self) -> None:
        """Tear the encoder down without flushing. Join writer threads first."""
        with self._lock:
            if self._finished:
                return
            self._finished = True
            # Kill ffmpeg before closing the container, so writing the trailer
            # cannot block on a pipe nobody reads any more.
            if self.process.poll() is None:
                self.process.kill()
            try:
                self._container.close()
            except Exception:
                pass
        self._log_thread.join(timeout=5)


def _rendered_duration(layout: RuntimeLayout, path: Path) -> float:
    data = _run_json(
        [
            str(layout.ffprobe),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=duration:format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    streams = data.get("streams") or []
    for raw in ((streams[0] if streams else {}).get("duration"), (data.get("format") or {}).get("duration")):
        try:
            duration = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    raise RuntimeError("Could not determine the rendered duration for the final mux.")


def mux(
    layout: RuntimeLayout,
    rendered: Path,
    source: Path,
    output: Path,
    container: str,
    *,
    copy_audio: bool = True,
) -> None:
    """Combine the rendered video with the source audio, chapters and metadata."""
    duration = _rendered_duration(layout, rendered)
    if not copy_audio:
        maps = ["-map", "0:v:0"]
        streams = ["-c:v", "copy", "-an"]
    elif container == "MKV":
        maps = ["-map", "0:v:0", "-map", "1:a?", "-map", "1:s?"]
        streams = ["-c:v", "copy", "-c:a", "copy", "-c:s", "copy"]
    else:
        maps = ["-map", "0:v:0", "-map", "1:a?"]
        streams = ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]

    command = [
        str(layout.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(rendered),
        "-t",
        f"{duration:.9f}",
        "-i",
        str(source),
        *maps,
        "-map_metadata",
        "1",
        "-map_chapters",
        "1",
        *streams,
        str(output),
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode:
        raise RuntimeError("The final audio/metadata mux failed:\n" + result.stderr[-4000:])
