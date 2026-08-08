# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

import os
from PIL import Image, ImageDraw

from . import constants as c


def _make_icon(enabled: bool) -> Image.Image:
    """Return a 64×64 RGBA PIL Image for the system tray."""
    ico_path = os.path.join(c._HERE, "app.ico")
    if os.path.isfile(ico_path):
        try:
            img = Image.open(ico_path)
            # A .ico usually bundles several resolutions; pick the largest frame.
            # (The previous one-liner indexed the max() int result with [0],
            # raising TypeError on any multi-frame icon and silently falling
            # back to the drawn icon below.)
            n_frames = getattr(img, "n_frames", 1)
            if n_frames > 1:
                def _frame_width(i):
                    img.seek(i)
                    return img.size[0]
                img.seek(max(range(n_frames), key=_frame_width))
            img = img.convert("RGBA").resize((64, 64), Image.LANCZOS)
            if not enabled:
                r, g, b, a = img.split()
                grey = Image.merge("RGB", (r, g, b)).convert("L")
                grey_rgba = Image.merge("RGBA", (grey, grey, grey, a))
                img = grey_rgba
            return img
        except Exception:
            pass

    img    = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d      = ImageDraw.Draw(img)
    colour = c.ACCENT if enabled else "#8fa68a"
    d.ellipse([2, 2, 62, 62], fill=colour)
    d.rectangle([22, 20, 42, 28], fill="white")
    d.rectangle([22, 28, 30, 36], fill="white")
    d.rectangle([22, 36, 42, 44], fill="white")
    d.rectangle([34, 44, 42, 52], fill="white")
    d.rectangle([22, 52, 42, 60], fill="white")
    return img
