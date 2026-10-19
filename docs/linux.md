# Linux setup (experimental)

The Python nodes, PyTorch, optical flow, decoding and encoding run natively
on Linux. The Windows DLSS worker runs in a dedicated Wine prefix and
exchanges frames with Python over the same binary pipes as on Windows; the
wire protocol is unchanged, so the same runtime release (v6.0, protocol
version 4) is used on both platforms.

## Tested environment

| Component | Tested with |
| --- | --- |
| GPU | NVIDIA GeForce RTX 5090 (headless; display on an AMD iGPU) |
| Driver | 610.57.04 and 595.104.02 (proprietary, **with X userspace**, see below) |
| OS | Gentoo Linux, kernel 6.18 |
| Wine | GE-Proton 11-6's bundled Wine (`wine-proton-11.0.2` also starts the stack, but see the pairing note) |
| Graphics DLLs | DXVK / VKD3D-Proton / DXVK-NVAPI from **GE-Proton 11-6** (one coherent set) |
| Shader compiler | Microsoft `d3dcompiler_47.dll` x64 via Winetricks |
| The sibling pack | [ComfyUI-NVIDIA-DLSS-Frame-Interpolation](https://github.com/Konohamaru04/ComfyUI-NVIDIA-DLSS-Frame-Interpolation), whose patched ReShade carrier this pack stages |

## Fresh install

Everything below assumes ComfyUI lives at `$COMFYUI` with its virtualenv at
`$COMFYUI/venv`. The node pack folder is `custom_nodes/ComfyUI-DLSS5-Enhancer`.
Substitute your paths where they differ.

### 1. System prerequisites (Gentoo)

```bash
# NVIDIA driver: 610.57.04 and 595.104.02 are tested versions. CRITICAL: the X userspace
# must be enabled. A USE="-X" build (the usual CUDA-only render box setup)
# ships no nvidia_drv.so / libGLX_nvidia, and the NVIDIA Vulkan WSI then
# segfaults on surface queries — killing vulkaninfo and this pack's worker.
# /etc/portage/package.use/x11-drivers:
#   x11-drivers/nvidia-drivers kernel-open persistenced X
sudo emerge --ask =x11-drivers/nvidia-drivers-610.57.04

sudo emerge --ask app-emulation/wine-proton app-emulation/winetricks \
               dev-util/vulkan-tools git-lfs
```

Verify before continuing:

```bash
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
vulkaninfo --summary > /dev/null && echo "WSI OK"   # must not segfault
ffmpeg -version | head -1
```

### 2. Install GE-Proton 11-6

The prefix graphics DLLs must come from **one coherent set**; see
[Component pairing matters](#component-pairing-matters). Download
[GE-Proton11-6](https://github.com/GloriousEggroll/proton-ge-custom/releases/tag/GE-Proton11-6)
and unpack it anywhere persistent, e.g. `/spool/dev/dlss5`:

```bash
curl -sL -o GE-Proton11-6-x86_64.tar.gz \
  https://github.com/GloriousEggroll/proton-ge-custom/releases/download/GE-Proton11-6/GE-Proton11-6-x86_64.tar.gz
tar xzf GE-Proton11-6-x86_64.tar.gz
```

### 3. Fetch the native shader compiler

Run once (any Wine will do; the DLL is cached for the setup helper):

```bash
WINEPREFIX=/tmp/dlss-compiler WINE=<wine binary> winetricks -q d3dcompiler_47
# expected cache: ~/.cache/winetricks/d3dcompiler_47/d3dcompiler_47.dll
```

### 4. Install both node packs

This pack needs the sibling pack's checkout beside it as the source of the
patched ReShade carrier (or an explicit `--carrier`/`DLSS5_LINUX_CARRIER`).
Cloning both side by side is the simple path:

```bash
cd <your dev directory>
git clone https://github.com/Konohamaru04/ComfyUI-NVIDIA-DLSS-Frame-Interpolation
git clone https://github.com/Blueforcer/ComfyUI-DLSS5-Enhancer

ln -s <dev>/ComfyUI-DLSS5-Enhancer                $COMFYUI/custom_nodes/
ln -s <dev>/ComfyUI-NVIDIA-DLSS-Frame-Interpolation $COMFYUI/custom_nodes/
```

### 5. Install the DLSS 5 runtime

Downloads the v6.0 runtime (~856 MB) into the pack and writes
`config.json`. Only the five runtime files are extracted (~240 MB on Linux);
the installer picks the Neural Rendering build for the installed GPU:

```bash
$COMFYUI/venv/bin/python install_runtime.py --yes
```

### 6. Configure Wine (once per machine)

The helper creates (or adopts) the dedicated prefix shared with the sibling
pack (`~/.local/share/comfyui-dlss-wine`), installs the graphics DLLs and
native compiler into it, stages the patched carrier, links the driver
bridge, and saves the worker-only environment in `linux-runtime.json`.

On this PRIME-offload machine the NVIDIA driver reports its Vulkan
`deviceName` as `modesetting`, not the nvidia-smi name, so `--vulkan-name`
must be passed (otherwise every adapter is filtered out and the worker
fails with `CreateDXGIFactory1 failed`). Check `vulkaninfo --summary` for
what your driver reports.

```bash
python3 scripts/setup_linux.py \
  --proton <dev>/GE-Proton11-6-x86_64 \
  --comfyui $COMFYUI \
  --python $COMFYUI/venv/bin/python \
  --driver-dir /usr/lib64/nvidia/wine \
  --vulkan-name modesetting \
  --verify
```

`--driver-dir` is the directory containing `_nvngx.dll` from the NVIDIA
driver (Gentoo: `/usr/lib64/nvidia/wine`). If the sibling pack is also
installed, run **its** `scripts/setup_linux.py` once with the same
`--proton`/`--vulkan-name`; both helpers share the same marked prefix.

### 7. Verify

```bash
$COMFYUI/venv/bin/python selftest.py --frames 3 --mode "2x (Performance)"
```

Expect `feature 18 : verified` plus one line per frame. Then restart
ComfyUI and run `DLSS5 Settings -> DLSS5 Enhance Images` on a small batch
with `verify_neural_rendering` on.

## How the Linux path works

- `dlss5/workers.py` wraps the worker in Wine and applies the saved
  worker-only environment from `linux-runtime.json`. ComfyUI, PyTorch and
  FFmpeg never run under Wine.
- On Linux the worker does not run from `runtime/`. Instead
  `runtime-linux/` is prepared automatically: the worker `nvngx.dll` is
  copied there as a real file, the patched ReShade carrier `dxgi.dll` is
  staged, and the add-on, DLSS and NR runtimes plus the NVIDIA driver
  bridge `_nvngx.dll` are linked. Wine loads the carrier beside the
  executable, so the staging directory is the session working directory
  and holds its own `ReShade.ini`/`ReShade.log`.
- The stock ReShade carrier faults inside a CUDA descriptor lookup under
  VKD3D-Proton. A patched ReShade 6.8.0.1 build is required; it is shipped
  by the ComfyUI-NVIDIA-DLSS-Frame-Interpolation repository
  (`bin/runtime/host-linux/dxgi.dll`, SHA-256 `596e4a61…b25e80`) and
  auto-detected when that pack is checked out beside this one. No NVIDIA
  binary is modified.
- Files that exist only after setup (`config.json`, `linux-runtime.json`,
  `runtime/`, `runtime-linux/`) are ignored by Git; the machine-specific
  configuration survives a `git pull` and is regenerated by the setup
  helper after a fresh clone.

## Runtime versions

`install_runtime.py` pins **v6.0**, the newest upstream release that still
ships the standalone protocol-4 worker (`bin/runtime/host/nvngx.dll`) and
loads `renodx-dlss5.addon64` from beside it. The installer also selects the
architecture-optimized Neural Rendering runtime:

| GPU | Neural Rendering build |
| --- | --- |
| RTX 20 / 30 (Turing, Ampere) | `dlssnr/Turing+/nvngx_dlssnr.dll` |
| RTX 40 (Ada Lovelace) | `dlssnr/Ada Lovelace+/nvngx_dlssnr.dll` |
| RTX 50 (Blackwell) | `dlssnr/Blackwell+/nvngx_dlssnr.dll` |

The choice is automatic from `nvidia-smi`; `--nr-arch turing|ada|blackwell`
overrides it, and an unknown GPU falls back to the release's host runtime.

Newer releases are **not** drop-in updates:

- **v7.0** moved the add-on to `bin/runtime/dlssnr/` and resolves it as
  `..\dlssnr` from the worker, so the flat `runtime-linux/` staging no
  longer matches.
- **v8.0** replaced the standalone worker with the Neuroframe Engine
  (`neuroframe_engine_*.dll`); there is no `nvngx.dll` protocol-4 worker to
  start.
- **v13.0 and later** ship only the application engine and the NVIDIA
  runtime DLLs; the ReShade carrier and RenoDX add-on are gone.

The installer rejects those layouts with an explanatory message instead of
producing a runtime that fails at session setup.

## Component pairing matters

The worker got through the handshake and DLSS SR, but feature-18 evaluation
crashed with an access violation when the prefix was filled with separately
downloaded "latest" releases (DXVK 2.7.1 + VKD3D-Proton 2.14.1 + a much
newer DXVK-NVAPI 0.9.2). Switching the same prefix to the coherent
GE-Proton 11-6 set fixed neural rendering, frame generation and NGX
initialization in one step. The DXVK/DXVK-NVAPI pair speaks a private
interface; mixing versions across releases is not supported even when
every component individually works for plain D3D12. Prefer one coherent
set (a GE-Proton or Proton install) over hand-picked release DLLs.

## Hybrid systems need the NVIDIA X userspace

On a machine where the display runs on another GPU, the NVIDIA proprietary
Vulkan WSI segfaults inside `vkGetPhysicalDeviceSurfaceSupportKHR` when the
driver package lacks its X/GLX userspace components — system `vulkaninfo`
segfaults the same way, and the worker dies right after swapchain
creation. On Gentoo that means `x11-drivers/nvidia-drivers` must be built
with `USE="X"` (which installs `nvidia_drv.so` and `libGLX_nvidia`); the
default configuration used for CUDA-only render boxes disables exactly
those pieces. After a rebuild and restart, surface queries work and the
worker proceeds.

## Troubleshooting

| Message | Cause and fix |
| --- | --- |
| `No adapters found` / `CreateDXGIFactory1 failed` | The Vulkan device filter does not match. Pass `--vulkan-name` with what the driver reports, or remove a stale filter from `linux-runtime.json` |
| `The Linux DLSS 5 staging directory ... is incomplete` | Run `scripts/setup_linux.py`; it stages the patched carrier and links the driver bridge |
| `does not match the patched ReShade 6.8.0.1 build` | The sibling pack carrier changed. Rebuild it from the `patches/` of that repository or pin `--carrier` to a known build |
| `Wine was not found` | Install Wine or set `DLSS_WINE_PATH` to its executable |
| `nvidia-smi is unavailable` | Install the NVIDIA driver tools; until the kernel module and userspace match after a driver update, nvidia-smi reports a version mismatch |
| Worker exits before the handshake | Check the runtime version (release v6.0, protocol 4), then the driver; NGX is driver-sensitive |
| Crash right after swapchain creation; `vulkaninfo` also segfaults | Hybrid display setups need the NVIDIA X/GLX userspace installed (Gentoo: rebuild nvidia-drivers with `USE="X"`) and a restart |
| Feature-18 evaluation access violation on frame 0; NGX init `0xBAD00002` | The graphics DLL set is version-skewed (e.g. a newer DXVK-NVAPI against an older DXVK). Refill the prefix from one coherent GE-Proton set via `scripts/setup_linux.py --proton <GE-Proton>` |
| NR access violation | Verify the patched carrier is staged, the native `d3dcompiler_47` override is active and both `WINE_HEAP_*` flags are in the saved settings |

## Regression tests

CPU-only tests run without a GPU, Wine or runtime:

```bash
python -m unittest discover -s tests -v
```

They cover the worker environment validation, the Wine launch command,
LFS-pointer rejection, staging preparation and ffmpeg resolution. The
GPU test is the smoke test above; it is also exercised by the setup
helper's `--verify`.