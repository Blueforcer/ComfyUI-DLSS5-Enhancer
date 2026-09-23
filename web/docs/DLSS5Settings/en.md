# DLSS5 Settings

DLSS5 Settings collects the NVIDIA DLSS 5 neural-rendering controls and feeds them to a DLSS5 Enhance Images or DLSS5 Enhance Video File node. One settings node drives both the image and the video enhancer.

## Inputs

| Name | Type | Description |
| --- | --- | --- |
| `Upscaling Mode` | COMBO | DLSS mode. 1x (DLAA) enhances at the source resolution; the other modes also upscale by that factor. |
| `NR Preset` | COMBO | Neural rendering preset inside the DLSS model. Measured to produce identical output at every value on current builds. |
| `NR Style` | COMBO | Look of the neural pass: Natural stays closer to the source, Cinematic is stronger. |
| `NR Intensity` | FLOAT | Overall strength of the neural rendering pass. Measured on current runtime builds, values above 1.0 have no further effect; use below 1.0 to blend back towards the source. |
| `Local Tone Strength` | FLOAT | Local tone mapping strength. |
| `Local Structure Strength` | FLOAT | Local detail and structure reconstruction. Higher keeps more texture; 1.5 is a good balance for AI generated footage. |
| `Skin Structure Strength` | FLOAT | Skin and pore reconstruction. Requires Automatic Mask, which is what tells the model where skin is; with the mask off this control does nothing. -1 leaves it to the model. |
| `Automatic Mask` | BOOLEAN | Let the model detect the regions it treats as skin. Also the gate for Skin Structure Strength. |
| `DLSS Model Preset` | COMBO | Force a specific DLSS model instead of NVIDIA's choice. Measured detail retention: Default, J and K are the softest, L and M reconstruct markedly more skin and hair texture. Set to Default if the worker reports a different applied preset. |
| `Motion` | COMBO | Motion vectors for temporal accumulation. auto estimates optical flow for sequences and skips it for single images. |
| `Scene Change Threshold` | FLOAT | Mean luminance change above which temporal history is reset. |
| `Warmup Frames` | INT | Extra frames the worker renders before the first output settles. |
| `Runtime Directory` | STRING | Optional path to a DLSS 5 runtime directory (the folder holding nvngx.dll). Empty uses config.json, DLSS5_RUNTIME_DIR or the bundled runtime folder. |

## Outputs

| Name | Type | Description |
| --- | --- | --- |
| `Settings` | DLSS5_SETTINGS | The configured session passed to an enhancer node. |