"""Run the DLSS 5 protocol smoke test without ComfyUI.

    python_embeded/python.exe ComfyUI/custom_nodes/ComfyUI-DLSS5-Enhancer/selftest.py

The embedded Python does not put the working directory on sys.path, so this
wrapper exists instead of relying on `python -m dlss5.selftest`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dlss5.selftest import main  # noqa: E402  (path bootstrap must run first)

if __name__ == "__main__":
    sys.exit(main())
