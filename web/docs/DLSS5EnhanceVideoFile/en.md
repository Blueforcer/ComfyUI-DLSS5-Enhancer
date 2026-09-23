# DLSS5 Enhance Video File

DLSS5 Enhance Video File decodes a video file, runs every frame through NVIDIA DLSS 5 neural rendering and re-encodes it, keeping the original timestamps, audio, chapters and metadata. Long videos never enter the workflow as a batch.

## Inputs

| Name | Type | Description |
| --- | --- | --- |
| `Video Path` | STRING | Absolute path to the source video file. |
| `Settings` | DLSS5_SETTINGS | Connect a DLSS5 Settings node. |
| `Codec` | COMBO | H.264 and HEVC use NVENC with a software fallback. AV1 needs NVENC and has no fallback. ProRes Proxy encodes in software. |
| `Container` | COMBO | MKV stream copies audio and subtitles. MP4 and MOV re-encode audio to AAC and drop subtitles. ProRes needs MOV or MKV. |
| `Quality` | COMBO | Auto derives a bitrate from resolution and frame rate, Good doubles it, Best quadruples it, Max encodes at constant quality. Ignored for ProRes Proxy. |
| `Filename Prefix` | STRING | Prefix of the output file; a timestamp is appended. |
| `Output Directory` | STRING | Empty writes to the ComfyUI output directory. A relative path is resolved inside it; an absolute path is used as given. |
| `Max Frames` | INT | 0 renders the whole video; any other value renders a preview of that many frames. |
| `Copy Audio` | BOOLEAN | Mux the original audio, and with MKV the subtitles, into the result. Chapters and metadata are kept either way. |
| `Verify Neural Rendering` | BOOLEAN | Check the ReShade log for signed feature-18 execution after the render. The file is written either way. |

## Outputs

| Name | Type | Description |
| --- | --- | --- |
| `Video Path` | STRING | The re-encoded output video path. |
| `Frames` | INT | The number of frames actually rendered. |