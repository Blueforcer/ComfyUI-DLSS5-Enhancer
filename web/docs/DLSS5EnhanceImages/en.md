# DLSS5 Enhance Images

DLSS5 Enhance Images runs every image through NVIDIA DLSS 5 neural rendering (NGX feature 18) on the native worker. Feed video frames in playback order so temporal accumulation works.

## Inputs

| Name | Type | Description |
| --- | --- | --- |
| `Images` | IMAGE | Frames in playback order; the batch order is the temporal order. |
| `Settings` | DLSS5_SETTINGS | Connect a DLSS5 Settings node. |
| `Verify Neural Rendering` | BOOLEAN | Fail the render when the ReShade log shows no signed feature-18 execution, instead of silently returning plain upscaled frames. |

## Outputs

| Name | Type | Description |
| --- | --- | --- |
| `Images` | IMAGE | The frames reconstructed by the DLSS 5 neural rendering pass. |