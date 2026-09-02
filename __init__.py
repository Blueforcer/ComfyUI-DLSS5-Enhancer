"""ComfyUI-DLSS5-Enhancer: NVIDIA DLSS 5 neural rendering for frames and videos."""

from typing_extensions import override

from comfy_api.latest import ComfyExtension, io

from .nodes import DLSS5EnhanceImages, DLSS5EnhanceVideoFile, DLSS5SettingsNode


class Dlss5Extension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [
            DLSS5SettingsNode,
            DLSS5EnhanceImages,
            DLSS5EnhanceVideoFile,
        ]


async def comfy_entrypoint() -> Dlss5Extension:
    return Dlss5Extension()


# The node registry scans for this mapping to learn the node names; the V3
# entrypoint above is what actually registers them.
if False:
    NODE_CLASS_MAPPINGS = {
        "DLSS5Settings": DLSS5SettingsNode,
        "DLSS5EnhanceImages": DLSS5EnhanceImages,
        "DLSS5EnhanceVideoFile": DLSS5EnhanceVideoFile,
    }
