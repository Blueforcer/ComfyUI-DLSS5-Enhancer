The direct NGX bridge and caller shim are adapted from
[kos94ok/ComfyUI-DLSS5-NR-Linux](https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux),
based on [lisitskyaa/ComfyUI-DLSS5-NR](https://github.com/lisitskyaa/ComfyUI-DLSS5-NR).
Their MIT license is preserved in `LICENSE`.

`frame_worker.cpp` implements this pack's version-4 pipe protocol. The bridge
runs the DLAA/SR carrier at every scale so the existing model-preset control
also applies at 1x. It calls Neural Rendering directly, without ReShade.

Build on Linux with MinGW-w64: `bash native/build_linux.sh`.
The outputs in `runtime/` are Windows binaries executed by Wine. NVIDIA
libraries and the Wine/VKD3D/DXVK-NVAPI installation remain external dependencies.

Follow the complete [Linux / Wine setup](../README.md#experimental-linux-backend)
for dependencies, prefix creation, DLL-copy commands, configuration, and tests.

To reuse an external DLSS 5 Visual Enhancer runtime, register it with
`python install_runtime.py --runtime-dir /absolute/path/to/bin/runtime`.
Build the worker here, then copy the generated `runtime/linux/` and
`runtime/caller/` directories into that external runtime. Set `runtime_dir`
in `config.json` to the external directory, and place the driver's `_nvngx.dll`
there too. `wine_prefix` can still point to the dedicated prefix created by
the main guide. Register the runtime before setting the Wine configuration:
`install_runtime.py` rewrites `config.json`.

Local validation on 2026-09-06 used an RTX 5090, NVIDIA driver 610.57.04,
Wine 11.17, and the graphics DLLs from Proton CachyOS 11.0 (20260521).
The installed ComfyUI was `/media/p5/Comfyui`, with Conda environment
`13_env_py313`. Image-batch rendering with warmup and 2x HEVC video rendering
both completed through ComfyUI with feature-18 verification enabled.

Run `DLSS5_RUN_GPU_TESTS=1 python -m unittest discover -s tests` for GPU pixel
checks at zero, partial, and full NR intensity. Below full intensity, the NR
backbuffer must contain the current DLAA/SR image used for blending.

The bridge also explicitly shuts down the directly initialized NR snippet
before unloading it. NGX core shutdown alone does not release that session.
Other GPU/driver/runtime combinations and pixel parity with RenoDX have not
been validated.
