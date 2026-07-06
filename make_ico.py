"""
make_ico.py — GrammarAnywhere Icon Generator (Midnight Blue & Champagne Gold)
──────────────────────────────────────────────────────────────────────────────
Run this once before building the Inno Setup installer:

    python make_ico.py

Produces a polished, high-quality app.ico in the same folder.
Automatically optimizes the 16px and 32px sizes for system tray legibility.
Requires: Pillow  (pip install pillow)
"""

import struct
from io import BytesIO
from PIL import Image, ImageDraw, ImageFilter

SIZES = [256, 48, 32, 16]

# Handle backwards compatibility for older Pillow versions
try:
    LANCZOS = Image.Resampling.LANCZOS
    BILINEAR = Image.Resampling.BILINEAR
except AttributeError:
    LANCZOS = Image.ANTIALIAS
    BILINEAR = Image.BILINEAR


def make_diagonal_midnight_gradient(w: int) -> Image.Image:
    """Creates a multi-stop diagonal gradient representing reflective deep midnight blue."""
    grad = Image.new("RGB", (3, 3))
    c_dark  = (12, 16, 26)      # Deep midnight black-blue
    c_mid   = (22, 32, 52)      # Rich navy/indigo
    c_light = (36, 52, 82)      # Sleek slate blue
    
    grad.putpixel((0, 0), c_light)
    grad.putpixel((1, 0), c_mid)
    grad.putpixel((2, 0), c_dark)
    
    grad.putpixel((0, 1), c_mid)
    grad.putpixel((1, 1), c_dark)
    grad.putpixel((2, 1), c_light)
    
    grad.putpixel((0, 2), c_dark)
    grad.putpixel((1, 2), c_light)
    grad.putpixel((2, 2), c_mid)
    
    return grad.resize((w, w), BILINEAR)


def make_diagonal_gold_gradient(w: int) -> Image.Image:
    """Creates a multi-stop diagonal gradient representing polished reflective gold."""
    grad = Image.new("RGB", (3, 3))
    c_dark  = (160, 120, 45)    # Muted dark bronze
    c_mid   = (225, 185, 90)    # Warm polished gold
    c_light = (255, 240, 195)   # Sparkling champagne gold
    
    grad.putpixel((0, 0), c_light)
    grad.putpixel((1, 0), c_mid)
    grad.putpixel((2, 0), c_dark)
    
    grad.putpixel((0, 1), c_mid)
    grad.putpixel((1, 1), c_dark)
    grad.putpixel((2, 1), c_light)
    
    grad.putpixel((0, 2), c_dark)
    grad.putpixel((1, 2), c_light)
    grad.putpixel((2, 2), c_mid)
    
    return grad.resize((w, w), BILINEAR)


def draw_pen_nib_mask(w: int, s: float) -> Image.Image:
    """Draws a sleek pen nib and orbital swoop."""
    mask = Image.new("L", (w, w), 0)
    draw = ImageDraw.Draw(mask)
    
    # 1. Sweep/Swoosh orbit around the nib base (uses Bezier math)
    p0 = (14 * s, 46 * s)
    p1 = (32 * s, 58 * s)
    p2 = (50 * s, 36 * s)
    width = max(1, int(3.5 * s))
    
    points = []
    for i in range(24 + 1):
        t = i / 24
        x = (1 - t)**2 * p0[0] + 2 * (1 - t) * t * p1[0] + t**2 * p2[0]
        y = (1 - t)**2 * p0[1] + 2 * (1 - t) * t * p1[1] + t**2 * p2[1]
        points.append((x, y))
    draw.line(points, fill=255, width=width)
    
    # 2. Sleek Pen Nib Geometry
    nib_pts = [
        (32 * s, 12 * s),   # Tip
        (46 * s, 30 * s),   # Right Shoulder
        (42 * s, 44 * s),   # Right waist
        (44 * s, 52 * s),   # Right Base
        (20 * s, 52 * s),   # Left Base
        (22 * s, 44 * s),   # Left waist
        (18 * s, 30 * s),   # Left Shoulder
    ]
    draw.polygon(nib_pts, fill=255)
    
    # 3. Breather Hole (subtract)
    br = 2.2 * s
    draw.ellipse([32 * s - br, 32 * s - br, 32 * s + br, 32 * s + br], fill=0)
    
    # 4. Slit (subtract)
    slit_w = max(1, int(0.8 * s))
    draw.line([(32 * s, 12 * s), (32 * s, 30 * s)], fill=0, width=slit_w)
    
    return mask


def draw_sparkles_mask(w: int, s: float) -> Image.Image:
    """Draws primary and secondary four-pointed sparkles."""
    mask = Image.new("L", (w, w), 0)
    draw = ImageDraw.Draw(mask)
    
    # --- Sparkle 1 (Large, top right) ---
    cx1, cy1 = 48 * s, 18 * s
    p1 = 9 * s    # peak length
    i1 = 2.2 * s  # inner pinch
    star1_pts = [
        (cx1, cy1 - p1),
        (cx1 + i1, cy1 - i1),
        (cx1 + p1, cy1),
        (cx1 + i1, cy1 + i1),
        (cx1, cy1 + p1),
        (cx1 - i1, cy1 + i1),
        (cx1 - p1, cy1),
        (cx1 - i1, cy1 - i1),
    ]
    draw.polygon(star1_pts, fill=255)
    
    # --- Sparkle 2 (Small, mid left) ---
    cx2, cy2 = 18 * s, 24 * s
    p2 = 5.5 * s
    i2 = 1.3 * s
    star2_pts = [
        (cx2, cy2 - p2),
        (cx2 + i2, cy2 - i2),
        (cx2 + p2, cy2),
        (cx2 + i2, cy2 + i2),
        (cx2, cy2 + p2),
        (cx2 - i2, cy2 + i2),
        (cx2 - p2, cy2),
        (cx2 - i2, cy2 - i2),
    ]
    draw.polygon(star2_pts, fill=255)
    
    return mask


def make_frame(size: int) -> Image.Image:
    """Generate a single frame. Optimizes sizes <= 32px for system tray legibility."""
    # 4x Supersampling
    ss_factor = 4
    w = size * ss_factor
    s = w / 64

    # Rounded Rect (Squircle) Background Mask
    pad = max(4, w // 32)
    radius = 14 * s
    squircle_mask = Image.new("L", (w, w), 0)
    squircle_draw = ImageDraw.Draw(squircle_mask)
    squircle_draw.rounded_rectangle(
        [pad, pad, w - pad - 1, w - pad - 1], radius=radius, fill=255
    )

    img = Image.new("RGBA", (w, w), (0, 0, 0, 0))

    if size <= 32:
        # ─────────────────────────────────────────────────────────────────
        # SYSTEM TRAY LEGIBILITY (Sizes <= 32px)
        # Flat base with high-contrast dual-tone foreground
        # ─────────────────────────────────────────────────────────────────
        dark_solid = Image.new("RGBA", (w, w), (12, 16, 26, 255))
        img.paste(dark_solid, (0, 0), mask=squircle_mask)

        # Single-pixel crisp gold border
        border_draw = ImageDraw.Draw(img)
        border_draw.rounded_rectangle(
            [pad, pad, w - pad - 1, w - pad - 1],
            radius=radius,
            outline=(225, 185, 90, 255),  # Flat gold
            width=max(1, int(1.0 * s))
        )

        # Draw Pen Nib in Flat Gold
        pen_mask = draw_pen_nib_mask(w, s)
        gold_color = Image.new("RGBA", (w, w), (225, 185, 90, 255))
        img.paste(gold_color, (0, 0), mask=pen_mask)

        # Draw Sparkles in Flat White
        star_mask = draw_sparkles_mask(w, s)
        white_color = Image.new("RGBA", (w, w), (255, 255, 255, 255))
        img.paste(white_color, (0, 0), mask=star_mask)

    else:
        # ─────────────────────────────────────────────────────────────────
        # PREMIUM LARGE RENDERING (Desktop shortcuts, Explorer, Installer)
        # Deep space gradients, metallic accents, and soft drop shadows
        # ─────────────────────────────────────────────────────────────────
        # Background Gradient
        bg_gradient = make_diagonal_midnight_gradient(w)
        img.paste(bg_gradient, (0, 0), mask=squircle_mask)

        # Draw Dual Minted Borders (Bronze outer, Champagne inner)
        border_draw = ImageDraw.Draw(img)
        outer_width = max(1, int(1.2 * s))
        
        # Outer dark gold outline
        border_draw.rounded_rectangle(
            [pad, pad, w - pad - 1, w - pad - 1],
            radius=radius,
            outline=(160, 120, 45, 200),
            width=outer_width
        )
        
        # Inner shiny gold highlight line
        inner_pad = pad + outer_width
        border_draw.rounded_rectangle(
            [inner_pad, inner_pad, w - inner_pad - 1, w - inner_pad - 1],
            radius=radius - outer_width,
            outline=(255, 240, 195, 255),
            width=max(1, int(0.8 * s))
        )

        # Generate separate element masks
        pen_mask = draw_pen_nib_mask(w, s)
        star_mask = draw_sparkles_mask(w, s)

        # Combine vector shapes for combined drop shadow
        combined_mask = Image.new("L", (w, w), 0)
        combined_mask.paste(pen_mask, (0, 0), mask=pen_mask)
        combined_mask.paste(star_mask, (0, 0), mask=star_mask)

        # Soft Drop Shadow under the graphics
        shadow_mask = combined_mask.filter(ImageFilter.GaussianBlur(1.0 * s))
        shadow_color = Image.new("RGBA", (w, w), (5, 8, 14, 230))  # Deep midnight shadow
        
        offset_x = int(0.7 * s)
        offset_y = int(1.2 * s)
        img.paste(shadow_color, (offset_x, offset_y), mask=shadow_mask)

        # Paste Reflective Gold Gradient into Pen Nib + Swoosh
        gold_gradient = make_diagonal_gold_gradient(w)
        img.paste(gold_gradient, (0, 0), mask=pen_mask)

        # Paste pure white over sparkles
        white_color = Image.new("RGBA", (w, w), (255, 255, 255, 255))
        img.paste(white_color, (0, 0), mask=star_mask)

    # Downsample to target size
    return img.resize((size, size), LANCZOS)


def build_ico(frames: list) -> bytes:
    """Build a Windows ICO file manually to avoid PIL size bugs."""
    png_blobs = []
    for img in frames:
        buf = BytesIO()
        img.save(buf, format="PNG")
        png_blobs.append(buf.getvalue())

    n = len(frames)
    dir_size = 6 + n * 16
    offsets = []
    pos = dir_size
    for blob in png_blobs:
        offsets.append(pos)
        pos += len(blob)

    ico = struct.pack("<HHH", 0, 1, n)

    for i, img in enumerate(frames):
        w = img.width
        bw = 0 if w >= 256 else w
        bh = 0 if w >= 256 else w
        ico += struct.pack(
            "<BBBBHHII",
            bw,
            bh,
            0,
            0,
            1,
            32,
            len(png_blobs[i]),
            offsets[i],
        )

    for blob in png_blobs:
        ico += blob

    return ico


def main():
    frames = [make_frame(s) for s in SIZES]
    ico_data = build_ico(frames)

    with open("app.ico", "wb") as f:
        f.write(ico_data)

    kb = len(ico_data) / 1024
    print(
        f"app.ico written — {len(SIZES)} sizes "
        f"({', '.join(str(s) for s in SIZES)} px), {kb:.1f} KB"
    )


if __name__ == "__main__":
    main()