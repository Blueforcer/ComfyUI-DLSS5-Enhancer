# ComfyUI-DLSS5-Enhancer

NVIDIA DLSS 5 Neural Rendering (NGX feature 18) as ComfyUI nodes. Enhance video frames and images
with the real neural renderer, with optional 1.5x to 3x upscaling.

This is not a filter that imitates the look. Frames are streamed to NVIDIA's native neural
renderer and come back reconstructed.

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Nodes](#nodes)
- [Example workflows](#example-workflows)
- [Verification](#verification)
- [Performance](#performance)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Licensing and attribution](#licensing-and-attribution)

## How it works

DLSS neural rendering has no Python API. It runs inside a native D3D12 process that hosts ReShade
and the RenoDX DLSS 5 add-on and evaluates NGX feature 18. This node pack implements the client
side of that worker's binary protocol:

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
(OpenCV DIS) at the render resolution, including automatic history resets on scene cuts.
Everything else is handled here: session setup, dimension negotiation, frame ordering,
cancellation and diagnostics.

The neural components themselves are NVIDIA, ReShade and RenoDX binaries. They are not part of
this repository and are not redistributed by it. See
[Licensing and attribution](#licensing-and-attribution).

## Requirements

| Item | Requirement |
| --- | --- |
| OS | Windows 11 64-bit with DirectX 12 |
| GPU | NVIDIA RTX 40 or 50 series. RTX 30 is experimental and slow, RTX 20 and older are rejected |
| Driver | Current NVIDIA GeForce driver |
| ComfyUI | A build with the V3 node API (`comfy_api.latest`) |
| Python | `numpy`, `opencv-python`, `av`. Usually already present in ComfyUI portable |

## Installation

### 1. Install the node pack

Clone into `ComfyUI/custom_nodes/`:

```bat
cd ComfyUI\custom_nodes
git clone https://github.com/Blueforcer/ComfyUI-DLSS5-Enhancer.git
```

Install the Python dependencies if they are missing:

```bat
python_embeded\python.exe -m pip install -r ComfyUI\custom_nodes\ComfyUI-DLSS5-Enhancer\requirements.txt
```

### 2. Provide the DLSS 5 runtime

Nothing is downloaded automatically. Run the setup script once:

```bat
python_embeded\python.exe ComfyUI\custom_nodes\ComfyUI-DLSS5-Enhancer\install_runtime.py
```

It prints the licensing notice, asks for confirmation, downloads the official DLSS 5 Visual
Enhancer release (about 467 MB) and extracts only `bin/runtime` and `bin/ffmpeg` into the node
folder.

If you already have that application installed, register it instead of downloading again:

```bat
python_embeded\python.exe ComfyUI\custom_nodes\ComfyUI-DLSS5-Enhancer\install_runtime.py ^
    --runtime-dir "D:\DLSS 5 Visual Enhancer\bin\runtime"
```

The runtime location is resolved in this order:

1. the `runtime_dir` widget on the DLSS5 Settings node
2. the `DLSS5_RUNTIME_DIR` environment variable
3. `config.json` written by `install_runtime.py`
4. the bundled `runtime/` folder inside the node pack

### 3. Restart ComfyUI

The nodes appear under **image/upscaling**.

## Nodes

### DLSS5 Settings

Collects the neural rendering controls once and feeds both processing nodes.

| Widget | Range (default) | Effect |
| --- | --- | --- |
| `upscaling_mode` | 1x DLAA, 1.5x Quality, 1.724x Balanced, 2x Performance, 3x Ultra Performance (1x) | 1x enhances at source resolution, the others also upscale |
| `nr_preset` | Default, #1 to #3 (Default) | Preset selected inside the neural rendering model |
| `nr_style` | Default, Natural, Cinematic (Default) | Natural stays closer to the source, Cinematic is stronger |
| `nr_intensity` | 0.00 to 2.00 (1.00) | Overall strength of the neural pass |
| `local_tone_strength` | 0.00 to 2.00 (1.00) | Local tone mapping |
| `local_structure_strength` | 0.00 to 2.00 (1.00) | Local detail and structure reconstruction |
| `skin_structure_strength` | -1.00 to 2.00 (-1.00) | Skin detail. -1 keeps the model's own behaviour |
| `automatic_mask` | off, on (off) | Let the model mask regions it should not alter |
| `dlss_model_preset` | Default, J, K, L, M (Default) | Force a specific DLSS model instead of NVIDIA's choice |
| `motion` | auto, optical_flow, none (auto) | Motion vectors for temporal accumulation. `auto` skips them for single images |
| `scene_change_threshold` | 0.00 to 1.00 (0.24) | Mean luminance change above which temporal history resets |
| `warmup_frames` | 0 to 16 (0) | Extra frames the worker renders before the first output settles |
| `runtime_dir` | path (empty) | Overrides runtime discovery |

Output: `DLSS5_SETTINGS`.

### DLSS5 Enhance Images

`IMAGE` in, `IMAGE` out. The batch order is the temporal order, so feed video frames in playback
order for temporal accumulation to work. A four channel input keeps its alpha, which is carried
over rather than reconstructed. The node reports progress and honours the ComfyUI cancel button,
terminating the worker on interruption.

### DLSS5 Enhance Video File

File in, file out. Decoding, neural rendering and encoding run concurrently through bounded
queues, so a long video never enters the workflow as one large batch. Original timestamps, audio,
subtitles, chapters and metadata are preserved.

| Input | Default | Notes |
| --- | --- | --- |
| `video_path` | empty | Absolute path to the source file |
| `codec` | HEVC | H.264, HEVC, AV1 (NVENC with software fallback), ProRes Proxy |
| `container` | MKV | MP4, MKV, MOV. ProRes requires MOV or MKV |
| `quality` | Auto | Auto derives a bitrate from resolution and frame rate, Good doubles it, Best quadruples it, Max encodes at constant quality |
| `filename_prefix` | DLSS5 | A timestamp is appended, existing files are never overwritten |
| `output_directory` | empty | Empty writes to the ComfyUI output directory |
| `max_frames` | 0 | 0 renders everything, any other value renders a preview |
| `copy_audio` | on | Mux the source audio, subtitles and chapters into the result |

Outputs: the written path (`STRING`) and the number of rendered frames (`INT`).

## Example workflows

Enhance a clip inside a workflow, so the frames stay available for compositing:

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
start and flushes it on exit.

Two log observations are normal and not errors:

- `NR upscaling fell back to native`: current runtime builds decline the low resolution colour
  contract, so DLSS upscales first and the neural pass then runs at the output resolution. The
  frames are still upscaled and neurally rendered.
- `vtable::Hook(Failed to find NVSDK_NGX_D3D12_CreateFeature)`: the add-on probes hook points it
  does not always need. Successful renders contain these lines too.

Give the worker the GPU. Another process rendering at the same time, such as a second ComfyUI
queue item or a game, can keep the signed DLSSNR runtime from initialising, which surfaces as a
failed verification.

The protocol and runtime can also be exercised without ComfyUI:

```bat
python_embeded\python.exe ComfyUI\custom_nodes\ComfyUI-DLSS5-Enhancer\selftest.py ^
    --frames 5 --mode "2x (Performance)"
```

This starts the worker, performs the handshake, sends five synthetic frames and prints the
negotiated render and output sizes plus the feature 18 evidence. It is the fastest way to tell a
runtime or driver problem apart from a workflow problem.

## Performance

- The worker starts once per render, not once per frame. Prefer one long batch over many short
  ones.
- Frames are streamed one at a time, so peak memory is dominated by the ComfyUI batch itself. For
  long videos use DLSS5 Enhance Video File, which never materialises the whole clip.
- Optical flow is computed at a reduced width of 640 pixels and scaled up, which keeps the guide
  cost small next to the neural pass. Set `motion = none` for unrelated stills.
- Output size is capped at 7680x4320. The node names a usable mode when a request exceeds it.

Reference numbers on an RTX 5090, 640x360 to 1280x720 at 2x Performance: about 4 seconds of
worker startup, then roughly 10 frames per second.

## Troubleshooting

| Message | Cause and fix |
| --- | --- |
| `No DLSS 5 runtime was found` | Runtime not installed or the path is wrong. Run `install_runtime.py` or set `DLSS5_RUNTIME_DIR` |
| `The DLSS 5 runtime in ... is incomplete` | Files were deleted or extracted partially. The message lists what is missing |
| `DLSS <mode> is unavailable for ...` | The driver rejected that output size. Choose a lower upscaling mode |
| `crashed inside feature-18 evaluation (access violation)` | Driver and RenoDX or DLSS NR versions do not match. Update the driver and reinstall the runtime |
| `feature-18 execution was not verified` | The worker ran but produced no neural rendering evidence. Most often another process was using the GPU. Run it again with nothing else rendering |
| `needs the tested experimental Ampere pair` | RTX 30 only works with the verified RenoDX 4.70 and DLSS NR 310.8.SF-v2 pair |
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

## Licensing and attribution

The source code in this repository is MIT licensed. See [LICENSE](LICENSE).

The runtime it drives is not. Those files are NVIDIA, ReShade and RenoDX components with their own
terms: NVIDIA's DLSS and neural rendering libraries are proprietary, ReShade is BSD-3-Clause, and
the RenoDX DLSS 5 add-on carries its own distribution terms. Install only components you are
authorised to use, from sources their licences permit. Nothing of that kind is hosted or
redistributed here.

The worker protocol was reimplemented for this pack. The runtime and the original application come
from [Merserk/dlss5-visual-enhancer](https://github.com/Merserk/dlss5-visual-enhancer). This
project is not affiliated with or endorsed by NVIDIA, ReShade, RenoDX or that project.
