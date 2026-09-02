# ComfyUI-DLSS5-Enhancer

Run NVIDIA DLSS 5 Neural Rendering over your frames and videos, inside ComfyUI.

DLSS 5 is NVIDIA's neural rendering pass. It reconstructs the lighting and material detail that
real-time rendering has to leave out: skin subsurface scattering, light transmission through hair,
contact shadows. This node pack drives that same renderer over video frames and image batches,
with optional 1.5x to 3x upscaling.

It is not a filter that imitates the look. Frames go to the native renderer and come back
reconstructed.

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Nodes](#nodes)
- [Recommended settings](#recommended-settings)
- [Example workflows](#example-workflows)
- [Verification](#verification)
- [Performance](#performance)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Licensing and attribution](#licensing-and-attribution)

## How it works

In games, DLSS 5 runs as "3D-Guided" Neural Rendering: the engine hands the model its rendered
frame together with geometry, texture and lighting buffers, and motion vectors. Video has none of
that. What reaches the model here is the decoded frame plus motion vectors estimated from the
footage itself, so the guidance is weaker than in a game, and the reconstruction is correspondingly
more conservative.

There is no Python API for any of it. The renderer lives in a native D3D12 process that hosts
ReShade and the RenoDX DLSS 5 add-on and evaluates NGX feature 18. This node pack implements the
client side of that worker's binary protocol:

```
  ComfyUI node
      |
      |  RGBA8 frame + FP16 motion vectors + history reset flag
      v
  native D3D12 worker
      (ReShade carrier -> RenoDX DLSS 5 add-on -> NGX feature 18)
      |
      |  RGBA8 reconstructed frame
      v
  ComfyUI node
```

Encoded video carries no motion vectors, so they are estimated per frame with dense optical flow
(OpenCV DIS) and supplied at the render resolution, including automatic history resets on scene
cuts. Everything else is handled here: session setup, dimension negotiation, frame ordering,
cancellation and diagnostics.

The neural components themselves are NVIDIA, ReShade and RenoDX binaries. They are not part of
this repository and are not redistributed by it. See
[Licensing and attribution](#licensing-and-attribution).

## Requirements

| Item | Requirement |
| --- | --- |
| OS | Windows 11 64-bit with DirectX 12. Windows 10 is untested |
| GPU | NVIDIA ships DLSS 5 for the RTX 50 series. Through this community runtime it also runs on RTX 40. RTX 20 and older are refused, and RTX 30 is refused unless the installed add-on and neural runtime match one specific verified pair by SHA-256; see [Troubleshooting](#troubleshooting) |
| Driver | Current NVIDIA GeForce driver |
| ComfyUI | A build with the V3 node API (`comfy_api.latest`) |
| Python | `numpy`, `av` and OpenCV. ComfyUI portable normally ships all three; `opencv-python` is deliberately not in `requirements.txt` so it cannot overwrite an existing `opencv-contrib-python` |

## Installation

All commands below are run from the ComfyUI portable root, the folder containing
`python_embeded` and `ComfyUI`.

### 1. Install the node pack

In ComfyUI-Manager, search for "DLSS5" and install it there. Or clone it by hand:

```bat
git clone https://github.com/Blueforcer/ComfyUI-DLSS5-Enhancer.git ComfyUI\custom_nodes\ComfyUI-DLSS5-Enhancer
python_embeded\python.exe -m pip install -r ComfyUI\custom_nodes\ComfyUI-DLSS5-Enhancer\requirements.txt
```

### 2. Provide the runtime

Nothing is downloaded automatically. Run the setup script once:

```bat
python_embeded\python.exe ComfyUI\custom_nodes\ComfyUI-DLSS5-Enhancer\install_runtime.py
```

It prints the licensing notice, asks for confirmation, downloads the third-party DLSS 5 Visual
Enhancer release v3.0 from its GitHub releases page (about 467 MB, roughly 700 MB on disk after
extraction) and extracts only its `bin/runtime` and `bin/ffmpeg/bin` contents into the node
folder.

If you already have that application installed, register it instead of downloading again:

```bat
python_embeded\python.exe ComfyUI\custom_nodes\ComfyUI-DLSS5-Enhancer\install_runtime.py ^
    --runtime-dir "D:\DLSS 5 Visual Enhancer\bin\runtime"
```

Release v3.0 is what this pack is built against; it speaks worker protocol version 4. An older
release fails at session setup with a protocol message.

Additional flags: `--url` overrides the download, `--yes` skips the prompt for unattended
installs, `--keep-archive` keeps the downloaded zip.

**Antivirus:** the worker is an executable that must keep the file name `nvngx.dll`, because the
signed-snippet caller contract checks that name. Running a `.dll` as a process is a pattern
Windows Defender and SmartScreen flag, so the file may be blocked or quarantined. If the node
reports that the worker could not be started, add the `runtime` folder to your exclusion list.

The runtime location is resolved like this:

1. the `runtime_dir` widget on the DLSS5 Settings node. When it is set, nothing else is tried
2. otherwise, in order: the `DLSS5_RUNTIME_DIR` environment variable, `config.json` written by
   `install_runtime.py`, and the bundled `runtime/` folder inside the node pack

ffmpeg and ffprobe are resolved separately and only when the video node runs: the
`DLSS5_FFMPEG_DIR` environment variable, the `ffmpeg_dir` key in `config.json`, the bundled
`ffmpeg/bin` folder, an `ffmpeg/bin` folder next to the runtime, and finally `PATH`.

### 3. Restart ComfyUI

The nodes appear under **image/upscaling**. If you restart before installing the runtime, they
load normally and only fail when executed, with a message that says how to install it.

## Nodes

### DLSS5 Settings

Collects the neural rendering controls once and feeds both processing nodes. Most of them are the
controls NVIDIA exposes to game developers: `local_structure_strength` and `local_tone_strength`
are the Structure and Tone intensities, `automatic_mask` is the semantic masking that tells the
model which regions are skin, and `dlss_model_preset` selects between models trained with
different weights.

| Widget | Options or range (default) | Effect |
| --- | --- | --- |
| `upscaling_mode` | `1x (DLAA / native)`, `1.5x (Quality)`, `1.724x (Balanced)`, `2x (Performance)`, `3x (Ultra Performance)` (1x) | 1x enhances at source resolution, the others also upscale |
| `nr_preset` | Default, Preset #1, Preset #2, Preset #3 (Default) | Preset inside the neural rendering model. Measured to have no effect on current runtime builds |
| `nr_style` | Default, Natural, Cinematic (Default) | Natural stays closer to the source, Cinematic pushes contrast |
| `nr_intensity` | 0.00 to 2.00 (1.00) | Strength of the neural pass. Values above 1.00 have no further effect on current builds; below 1.00 blends back towards the source |
| `local_tone_strength` | 0.00 to 2.00 (1.00) | Local tone mapping |
| `local_structure_strength` | 0.00 to 2.00 (1.50) | Local detail and structure reconstruction |
| `skin_structure_strength` | -1.00 to 2.00 (2.00) | Skin and pore reconstruction. Only active while `automatic_mask` is on, which is what locates the skin. `-1` leaves the decision to the model |
| `automatic_mask` | off, on (on) | Let the model detect the regions it treats as skin. Also the gate for `skin_structure_strength` |
| `dlss_model_preset` | Default, J, K, L, M (M) | Forces a specific model. Default, J and K are the softest; L and M reconstruct markedly more skin and hair texture. If the worker reports it applied a different preset, set this back to Default |
| `motion` | auto, optical_flow, none (auto) | Motion vectors for temporal accumulation. `auto` skips them for single images |
| `scene_change_threshold` | 0.01 to 1.00 (0.24) | Mean luminance change above which temporal history resets |
| `warmup_frames` | 0 to 16 (0) | Extra frames the worker renders before the first output settles |
| `runtime_dir` | path (empty) | Overrides runtime discovery |

`motion`, `scene_change_threshold`, `warmup_frames` and `runtime_dir` are marked advanced and are
hidden until advanced widgets are shown.

Output: `DLSS5_SETTINGS`.

### DLSS5 Enhance Images

`IMAGE` in, `IMAGE` out. The batch order is the temporal order, so feed video frames in playback
order for temporal accumulation to work. A four channel input keeps its alpha, which is carried
over rather than reconstructed. The node reports progress and honours the ComfyUI cancel button,
terminating the worker on interruption.

`verify_neural_rendering` (advanced, default on) fails the run when the ReShade log shows no
signed feature-18 execution.

### DLSS5 Enhance Video File

File in, file out. Decoding, neural rendering and encoding run concurrently through bounded
queues, so a long video never enters the workflow as one large batch. Original timestamps,
chapters and metadata are preserved. With MKV, audio and subtitles are stream copied unchanged;
with MP4 and MOV, audio is re-encoded to AAC 192 kbit/s and subtitle tracks are dropped.

| Input | Default | Notes |
| --- | --- | --- |
| `video_path` | empty | Absolute path to the source file |
| `codec` | HEVC | H.264 and HEVC use NVENC with a libx264/libx265 software fallback. AV1 needs NVENC and has no fallback. ProRes Proxy encodes in software |
| `container` | MKV | MP4, MKV, MOV. ProRes requires MOV or MKV |
| `quality` | Auto | Auto derives a bitrate from resolution and frame rate, Good doubles it, Best quadruples it, Max encodes at constant quality. Ignored for ProRes Proxy |
| `filename_prefix` | DLSS5 | A timestamp is appended, existing files are never overwritten. Path separators are rejected |
| `output_directory` | empty | Empty writes to the ComfyUI output directory; a relative path is resolved inside it |
| `max_frames` | 0 | 0 renders everything, any other value renders a preview |
| `copy_audio` | on | Mux the source audio into the result; subtitles only with MKV. Chapters and metadata are carried over either way |
| `verify_neural_rendering` | on (advanced) | Check the ReShade log after the render. The file is written either way |

Outputs: the written path (`STRING`) and the number of rendered frames (`INT`).

## Recommended settings

The defaults come from measurements on real generated footage, not from taste. The same clip was
run through the pack with one control changed at a time, comparing the rendered frame against the
source: mean absolute difference, Laplacian detail energy, and high frequency energy as a ringing
indicator.

What that showed on the current runtime build:

- The model reconstructs materials rather than sharpening edges. Overall detail energy drops,
  because generator noise disappears, while skin, hair and fabric gain structure that was not
  in the source.
- `dlss_model_preset` matters most. Default, J and K land around 0.56x source detail energy;
  L reaches 0.70x and M 0.73x, which is visible as pores, brow lines and separated beard hair
  instead of waxy skin. M is the default.
- `skin_structure_strength` only works while `automatic_mask` is on. With the mask off, every
  value produces bit identical output; with it on, 2.0 adds a further measurable step of skin
  detail. Both are on by default.
- `nr_intensity` is clamped at 1.0. Values of 1.0, 1.5 and 2.0 produce bit identical output;
  0.0 and 0.5 blend progressively back towards the source.
- `nr_preset` produces bit identical output at every value.
- `local_structure_strength` and `local_tone_strength` work across their full range.
- `nr_style = Cinematic` deepens shadows and shifts the grade, `Natural` softens. Both change the
  look rather than the amount of reconstruction.

Starting points:

| Goal | Settings |
| --- | --- |
| Clean up generated video, same resolution | Defaults: 1x DLAA, model M, mask on, skin 2.0, structure 1.5 |
| Clean up and upscale | Same, with `upscaling_mode = 2x (Performance)` |
| Most realistic faces | Defaults plus `local_structure_strength 2.0` |
| More contrast and punch | `nr_style = Cinematic`, but check the shadows |
| Too strong | `nr_intensity 0.6` to `0.7`, or `local_structure_strength 1.0` |

The neural pass works on what is in the frame. It reconstructs skin and material response, it
does not restore a face that the source no longer contains. For heavily degraded footage, run a
generative restoration pass first and use this node as the final cleanup.

## Example workflows

Enhance a clip inside a workflow, so the frames stay available for compositing. `Load Video` and
`Video Combine` come from
[ComfyUI-VideoHelperSuite](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite):

```
  VHS Load Video ----+
                     +--> DLSS5 Enhance Images --> VHS Video Combine
  DLSS5 Settings ----+
```

Enhance a long file, holding nothing in memory and keeping the audio:

```
  DLSS5 Settings --> DLSS5 Enhance Video File --> output path
```

Still images work with any `IMAGE` source. With `motion = auto`, a single image is rendered
without motion vectors automatically.

## Verification

Both processing nodes inspect the ReShade log after rendering and fail if signed feature 18
execution cannot be proven. This matters, because a silent fallback to plain upscaling would be
indistinguishable from success in the output itself. The check is controlled by
`verify_neural_rendering` and runs once the worker has exited, since ReShade rewrites its log on
start and flushes it on exit. The video node writes and muxes its file before verifying, so a
failed check never discards a finished render.

Two log observations are normal and not errors:

- `NR upscaling fell back to native`: current runtime builds decline the low resolution colour
  contract, so DLSS upscales first and the neural pass then runs at the output resolution. The
  frames are still upscaled and neurally rendered.
- `vtable::Hook(Failed to find NVSDK_NGX_D3D12_CreateFeature)`: the add-on probes hook points it
  does not always need. Successful renders contain these lines too.

Give the worker the GPU. Another process rendering at the same time, such as a second ComfyUI
queue item or a game, can keep the signed runtime from initialising, which surfaces as a failed
verification.

The protocol and runtime can also be exercised without ComfyUI:

```bat
python_embeded\python.exe ComfyUI\custom_nodes\ComfyUI-DLSS5-Enhancer\selftest.py ^
    --frames 5 --mode "2x (Performance)"
```

This starts the worker, performs the handshake, sends five synthetic frames and prints the
negotiated render and output sizes plus the feature 18 evidence. It is the fastest way to tell a
runtime or driver problem apart from a workflow problem. It also accepts `--width`, `--height`
and `--runtime-dir`.

## Performance

- The worker starts once per render, not once per frame. Prefer one long batch over many short
  ones.
- Frames are streamed to the worker one at a time, but DLSS5 Enhance Images allocates the whole
  output batch up front at the upscaled resolution. Peak memory is roughly the input batch plus
  the batch times the squared upscaling factor. For long videos use DLSS5 Enhance Video File,
  which never materialises the clip.
- Optical flow is computed at a reduced width of 640 pixels and scaled up, which keeps the guide
  cost small next to the neural pass. Set `motion = none` for unrelated stills.
- The output long edge is capped at 7680 pixels and the short edge at 4320, in either
  orientation. The node names a usable mode when a request exceeds that.

One measurement, RTX 5090 with driver 610.74, 640x360 to 1280x720 at 2x Performance: about
4 seconds of worker startup, then roughly 10 frames per second.

## Known limitations

- HDR sources are converted to 8-bit SDR without a tone-mapping operator, so highlights clip
  and the grade shifts. Convert HDR footage to SDR yourself first for a predictable result.
  The encoder writes `yuv420p`, or `yuv422p10le` for ProRes Proxy. The video node logs a
  warning when it detects an HDR transfer function.
- Sources without a frame count in their metadata, common for MKV, WebM and variable frame rate
  files, trigger a full counting pass with ffprobe before rendering starts. The node logs a line
  before doing so, but the queue looks idle while it runs.
- Only one worker runs at a time per render. Two DLSS nodes executing in parallel will compete
  for the GPU.
- `motion = none` sends zero motion and resets the temporal history once, which assumes the
  batch is one coherent sequence. For a batch of unrelated images, run them separately.
- Running the video node twice with identical inputs replays the cached result and writes no
  new file. Change an input, or the source file, to render again.
- Windows only. The worker, the carrier and the add-on are Windows binaries.

## Troubleshooting

| Message | Cause and fix |
| --- | --- |
| `No DLSS 5 runtime was found` | Runtime not installed or the path is wrong. Run `install_runtime.py` or set `DLSS5_RUNTIME_DIR` |
| `The DLSS 5 runtime in ... is incomplete` | Files were deleted or extracted partially. The message lists what is missing |
| `could not be started` | Antivirus is blocking the worker, or the file is not executable. Add the runtime folder to the exclusion list |
| `nvidia-smi is unavailable` | The NVIDIA driver tools are not on `PATH`, or no NVIDIA GPU is present |
| `is outside the supported RTX 30/40/50 scope` | RTX 20 or older, or a card whose name the detector cannot classify |
| `needs the tested experimental Ampere pair` | RTX 30 only runs with the verified RenoDX 4.70 and DLSS NR 310.8.SF-v2 files, checked by SHA-256. The message prints the hashes actually installed |
| `does not speak the version-4 protocol` | The registered runtime is from a different release. Use v3.0 |
| `ffmpeg/ffprobe were not found` | Only the video node needs them. Set `DLSS5_FFMPEG_DIR` or put them on `PATH` |
| `DLSS <mode> is unavailable for ...` | The driver rejected that output size. Choose a lower upscaling mode |
| `applied DLSS model preset X instead of the requested Y` | The runtime does not support that model. Set `dlss_model_preset` to Default |
| `crashed inside feature-18 evaluation (access violation)` | Driver and RenoDX or DLSS NR versions do not match. Update the driver and reinstall the runtime |
| `feature-18 execution was not verified` | The worker ran but produced no neural rendering evidence. Most often another process was using the GPU. Run it again with nothing else rendering |
| `AV1 NVENC cannot encode ...` | The GPU or driver cannot encode that resolution in AV1. Use H.264 or HEVC |

## Project layout

```
ComfyUI-DLSS5-Enhancer/
  install_runtime.py     Download or register the native runtime
  selftest.py            Protocol smoke test without ComfyUI
  dlss5/
    protocol.py          Binary wire format (version 4)
    session.py           Worker lifecycle, handshake, frame streaming
    motion.py            Optical flow temporal guides and scene cut resets
    settings.py          Public controls and their native translation
    paths.py             Runtime discovery and validation
    diagnostics.py       GPU checks, feature 18 proof, failure analysis
    imaging.py           IMAGE tensor and RGBA8 conversion, letterboxing
    media.py             Probing, decoding, NVENC encoding, muxing
    selftest.py
  nodes/                 The three ComfyUI nodes
```

To uninstall, delete the node folder. The runtime, `config.json` and any bundled ffmpeg live
inside it, so nothing is left behind.

## Licensing and attribution

The source code in this repository is MIT licensed. See [LICENSE](LICENSE).

The runtime it drives is not. Those files are NVIDIA, ReShade and RenoDX components plus the
upstream project's own worker executable, each under its own terms: NVIDIA's DLSS and neural
rendering libraries are proprietary, ReShade is BSD-3-Clause, the RenoDX DLSS 5 add-on carries its
own distribution terms, and the worker is built and licensed by the upstream project. Install only
components you are authorised to use, from sources their licences permit. Nothing of that kind is
hosted or redistributed here.

The worker protocol was reimplemented for this pack. The runtime and the original application come
from [Merserk/dlss5-visual-enhancer](https://github.com/Merserk/dlss5-visual-enhancer), which
makes NVIDIA's renderer usable outside a game engine. This project is not affiliated with or
endorsed by NVIDIA, ReShade, RenoDX or that project.
