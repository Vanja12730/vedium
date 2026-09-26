"""Generate Vedium's short product teaser as a PNG sequence and WAV track.

The animation is intentionally dependency-light: Pillow + NumPy render every
frame, then Blender's bundled encoder turns the sequence into an H.264 MP4.
"""

from __future__ import annotations

import math
import random
import shutil
import wave
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parent
FRAMES = ROOT / "frames"
W, H = 1280, 720
FPS = 24
DURATION = 12
FRAME_COUNT = FPS * DURATION

NAVY = "#111b2e"
NAVY_2 = "#182642"
PARCHMENT = "#fbf8f2"
SURFACE = "#fffdf8"
SURF2 = "#ece5da"
GREEN = "#2f6f55"
GREEN_2 = "#3f8266"
INK = "#25221d"
MUTED = "#746c62"
GOLD = "#d7b65f"
WHITE = "#fffdf8"
RED = "#a03030"

FONT_DIR = Path("C:/Windows/Fonts")
FONT_REGULAR = FONT_DIR / "segoeui.ttf"
FONT_MEDIUM = FONT_DIR / "seguisb.ttf"
FONT_BOLD = FONT_DIR / "segoeuib.ttf"
FONT_SERIF = FONT_DIR / "georgia.ttf"
FONT_SERIF_BOLD = FONT_DIR / "georgiab.ttf"


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def smooth(v: float) -> float:
    v = clamp(v)
    return v * v * (3 - 2 * v)


def out_cubic(v: float) -> float:
    return 1 - (1 - clamp(v)) ** 3


def in_out_quint(v: float) -> float:
    v = clamp(v)
    return 16 * v**5 if v < 0.5 else 1 - (-2 * v + 2) ** 5 / 2


def between(t: float, start: float, end: float) -> float:
    return clamp((t - start) / (end - start))


def alpha_window(t: float, start: float, end: float, fade: float = 0.35) -> float:
    return min(smooth(between(t, start, start + fade)), 1 - smooth(between(t, end - fade, end)))


@lru_cache(maxsize=None)
def font(size: int, face: str = "regular") -> ImageFont.FreeTypeFont:
    path = {
        "regular": FONT_REGULAR,
        "medium": FONT_MEDIUM,
        "bold": FONT_BOLD,
        "serif": FONT_SERIF,
        "serif_bold": FONT_SERIF_BOLD,
    }[face]
    return ImageFont.truetype(str(path), size)


def rgba(hex_color: str, a: int = 255) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[:2], 16), int(h[2:4], 16), int(h[4:], 16), a


def rounded(layer: Image.Image, box, radius: int, fill, outline=None, width=1):
    ImageDraw.Draw(layer).rounded_rectangle(box, radius, fill=fill, outline=outline, width=width)


def add_shadow(base: Image.Image, box, radius=24, opacity=80, blur=25, offset=(0, 10)):
    shadow = Image.new("RGBA", base.size)
    x0, y0, x1, y1 = box
    rounded(shadow, (x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]), radius, (0, 0, 0, opacity))
    base.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(blur)))


def draw_text(layer, xy, text, fnt, fill, anchor="la", spacing=4, align="left"):
    ImageDraw.Draw(layer).multiline_text(xy, text, font=fnt, fill=fill, anchor=anchor, spacing=spacing, align=align)


def cover_icon(base: Image.Image, cx: float, cy: float, size: float, opacity=255, glow=0):
    icon = Image.open(ROOT / "app-icon.png").convert("RGBA")
    s = max(1, int(size))
    icon = icon.resize((s, s), Image.Resampling.LANCZOS)
    if opacity < 255:
        icon.putalpha(icon.getchannel("A").point(lambda p: p * opacity // 255))
    x, y = int(cx - s / 2), int(cy - s / 2)
    if glow:
        halo = Image.new("RGBA", base.size)
        mask = icon.getchannel("A")
        tinted = Image.new("RGBA", icon.size, rgba(GOLD, min(180, glow)))
        tinted.putalpha(mask.point(lambda p: p * min(180, glow) // 255))
        halo.alpha_composite(tinted, (x, y))
        base.alpha_composite(halo.filter(ImageFilter.GaussianBlur(26)))
    base.alpha_composite(icon, (x, y))


def background(t: float) -> Image.Image:
    yy, xx = np.mgrid[0:H, 0:W]
    cx = W * (0.24 + 0.05 * math.sin(t * 0.22))
    cy = H * (0.22 + 0.04 * math.cos(t * 0.27))
    radius = np.sqrt(((xx - cx) / W) ** 2 + ((yy - cy) / H) ** 2)
    glow = np.clip(1 - radius * 1.85, 0, 1)[..., None]
    a = np.array([17, 27, 46], dtype=np.float32)
    b = np.array([26, 43, 72], dtype=np.float32)
    rgb = a + glow * (b - a)
    vignette = np.clip(((xx - W / 2) / (W * 0.72)) ** 2 + ((yy - H / 2) / (H * 0.8)) ** 2, 0, 1)[..., None]
    rgb *= 1 - vignette * 0.27
    img = Image.fromarray(np.uint8(np.clip(rgb, 0, 255)), "RGB").convert("RGBA")

    d = ImageDraw.Draw(img, "RGBA")
    random.seed(719)
    for i in range(34):
        px = (random.random() * W + t * (6 + i % 5)) % (W + 80) - 40
        py = random.random() * H
        r = 1 + (i % 3) * 0.55
        pulse = 0.45 + 0.45 * math.sin(t * (0.7 + i % 4 * 0.08) + i)
        d.ellipse((px - r, py - r, px + r, py + r), fill=(215, 182, 95, int(18 + 35 * pulse)))
    return img


def intro(base: Image.Image, t: float):
    a = alpha_window(t, 0.0, 2.15, 0.45)
    if a <= 0:
        return
    layer = Image.new("RGBA", base.size)
    p = out_cubic(between(t, 0.05, 0.95))
    cover_icon(layer, 640, 256 - (1 - p) * 22, 154 + 18 * p, int(255 * a), int(150 * a))
    title_p = out_cubic(between(t, 0.45, 1.18))
    draw_text(layer, (640, 405 + (1 - title_p) * 24), "Your words. Sharper.", font(46, "serif_bold"), rgba(WHITE, int(255 * a * title_p)), anchor="ma")
    draw_text(layer, (640, 466), "VEDIUM", font(15, "bold"), rgba(GOLD, int(230 * a * title_p)), anchor="ma")
    d = ImageDraw.Draw(layer)
    line_w = int(112 * title_p)
    d.line((640 - line_w / 2, 498, 640 + line_w / 2, 498), fill=rgba(GREEN_2, int(190 * a)), width=2)
    base.alpha_composite(layer)


def app_icon_badge(layer, x, y, size=30):
    cover_icon(layer, x, y, size, 255, 0)


def editor_scene(base: Image.Image, t: float):
    a = alpha_window(t, 1.65, 6.0, 0.4)
    if a <= 0:
        return
    layer = Image.new("RGBA", base.size)
    enter = out_cubic(between(t, 1.65, 2.25))
    card = (118, int(92 + (1 - enter) * 45), 1162, int(628 + (1 - enter) * 45))
    add_shadow(layer, card, radius=26, opacity=int(130 * a), blur=30)
    rounded(layer, card, 26, rgba(PARCHMENT, int(255 * a)), rgba("#ffffff", int(45 * a)), 1)
    x0, y0, x1, y1 = card
    d = ImageDraw.Draw(layer, "RGBA")
    d.rounded_rectangle((x0, y0, x1, y0 + 58), 26, fill=rgba(SURFACE, int(255 * a)))
    d.rectangle((x0, y0 + 31, x1, y0 + 58), fill=rgba(SURFACE, int(255 * a)))
    d.line((x0, y0 + 58, x1, y0 + 58), fill=rgba(SURF2, int(255 * a)), width=1)
    for i, c in enumerate(("#da6a5e", "#dfb64d", "#55a96c")):
        cx = x0 + 25 + i * 22
        d.ellipse((cx - 5, y0 + 29 - 5, cx + 5, y0 + 29 + 5), fill=rgba(c, int(220 * a)))
    draw_text(layer, (x0 + 106, y0 + 30), "Project brief — Notes", font(14, "medium"), rgba(MUTED, int(255 * a)), anchor="lm")
    draw_text(layer, (x0 + 72, y0 + 112), "A clearer way to say it", font(17, "bold"), rgba(GREEN, int(255 * a)))
    d.line((x0 + 72, y0 + 149, x1 - 72, y0 + 149), fill=rgba(SURF2, int(255 * a)), width=1)

    bad = "This are the idea I was trying to explain."
    typed_p = between(t, 2.08, 3.22)
    typed = bad[: int(len(bad) * typed_p)]
    draw_text(layer, (x0 + 72, y0 + 208), typed, font(30, "serif"), rgba(INK, int(255 * a)))
    cursor_x = x0 + 72 + ImageDraw.Draw(layer).textlength(typed, font=font(30, "serif"))
    if typed_p < 1 or int(t * 3) % 2 == 0:
        d.line((cursor_x + 2, y0 + 205, cursor_x + 2, y0 + 243), fill=rgba(GREEN_2, int(255 * a)), width=2)

    hint_a = smooth(between(t, 2.65, 3.15)) * (1 - smooth(between(t, 3.72, 4.08)))
    if hint_a:
        rounded(layer, (x1 - 184, y1 - 81, x1 - 70, y1 - 39), 13, rgba(NAVY, int(245 * a * hint_a)))
        draw_text(layer, (x1 - 127, y1 - 60), "F9", font(17, "bold"), rgba(WHITE, int(255 * a * hint_a)), anchor="mm")
        draw_text(layer, (x1 - 198, y1 - 60), "Improve writing", font(13, "medium"), rgba(MUTED, int(255 * a * hint_a)), anchor="rm")

    # A green scan passes over the sentence at the moment the hotkey lands.
    scan = between(t, 3.46, 4.12)
    if 0 < scan < 1:
        sx = int((x0 + 54) + (x1 - x0 - 108) * in_out_quint(scan))
        glow = Image.new("RGBA", base.size)
        gd = ImageDraw.Draw(glow)
        gd.rectangle((sx - 4, y0 + 172, sx + 4, y0 + 268), fill=rgba(GREEN_2, 105))
        layer.alpha_composite(glow.filter(ImageFilter.GaussianBlur(15)))
        d.line((sx, y0 + 177, sx, y0 + 263), fill=rgba(GREEN_2, 150), width=2)
    base.alpha_composite(layer)


def toast_scene(base: Image.Image, t: float):
    a = alpha_window(t, 3.85, 6.86, 0.28)
    if a <= 0:
        return
    layer = Image.new("RGBA", base.size)
    p = out_cubic(between(t, 3.85, 4.45))
    x1 = 1194 + (1 - p) * 420
    x0 = x1 - 528
    y0, y1 = 185, 566
    add_shadow(layer, (x0, y0, x1, y1), radius=22, opacity=int(155 * a), blur=30, offset=(0, 13))
    rounded(layer, (x0, y0, x1, y1), 22, rgba(SURFACE, int(255 * a)), rgba(GOLD, int(150 * a)), 1)
    d = ImageDraw.Draw(layer, "RGBA")
    d.rounded_rectangle((x0, y0, x1, y0 + 7), 22, fill=rgba(GOLD, int(255 * a)))
    d.rectangle((x0, y0 + 4, x1, y0 + 8), fill=rgba(GOLD, int(255 * a)))
    app_icon_badge(layer, x0 + 39, y0 + 42, 34)
    draw_text(layer, (x0 + 65, y0 + 36), "VEDIUM", font(14, "bold"), rgba(NAVY, int(255 * a)), anchor="lm")
    draw_text(layer, (x1 - 25, y0 + 36), "IMPROVE WRITING", font(11, "bold"), rgba(GREEN_2, int(255 * a)), anchor="rm")
    d.line((x0 + 24, y0 + 68, x1 - 24, y0 + 68), fill=rgba(SURF2, int(255 * a)), width=1)
    draw_text(layer, (x0 + 28, y0 + 94), "ORIGINAL", font(10, "bold"), rgba(MUTED, int(220 * a)))
    draw_text(layer, (x0 + 28, y0 + 119), "This are the idea I was trying to explain.", font(18, "regular"), rgba(MUTED, int(255 * a)))
    d.line((x0 + 27, y0 + 152, x1 - 27, y0 + 152), fill=rgba(SURF2, int(210 * a)), width=1)
    draw_text(layer, (x0 + 28, y0 + 178), "SUGGESTION", font(10, "bold"), rgba(GREEN, int(255 * a)))
    reveal = smooth(between(t, 4.25, 5.12))
    corrected = "This is the idea I was trying to express."
    shown = corrected[: int(len(corrected) * reveal)]
    draw_text(layer, (x0 + 28, y0 + 206), shown, font(21, "serif_bold"), rgba(INK, int(255 * a)))
    if reveal < 1:
        tx = x0 + 28 + d.textlength(shown, font=font(21, "serif_bold"))
        d.line((tx + 1, y0 + 205, tx + 1, y0 + 235), fill=rgba(GREEN_2, int(255 * a)), width=2)
    rounded(layer, (x0 + 28, y1 - 63, x0 + 146, y1 - 25), 10, rgba(SURF2, int(255 * a)))
    draw_text(layer, (x0 + 87, y1 - 44), "Dismiss", font(13, "medium"), rgba(MUTED, int(255 * a)), anchor="mm")
    rounded(layer, (x1 - 152, y1 - 63, x1 - 28, y1 - 25), 10, rgba(GREEN_2, int(255 * a)))
    draw_text(layer, (x1 - 90, y1 - 44), "Replace", font(13, "bold"), rgba(WHITE, int(255 * a)), anchor="mm")
    if 5.35 < t < 5.85:
        pulse = 1 - between(t, 5.35, 5.85)
        d.rounded_rectangle((x1 - 156, y1 - 67, x1 - 24, y1 - 21), 12, outline=rgba(GREEN_2, int(150 * pulse * a)), width=3)
    base.alpha_composite(layer)


def orbit_card(layer, box, label, detail, accent, opacity, progress):
    x0, y0, x1, y1 = box
    y_shift = int((1 - out_cubic(progress)) * 32)
    box = (x0, y0 + y_shift, x1, y1 + y_shift)
    add_shadow(layer, box, radius=22, opacity=int(90 * opacity), blur=25)
    rounded(layer, box, 22, rgba("#f9f7f1", int(245 * opacity)), rgba("#ffffff", int(85 * opacity)), 1)
    d = ImageDraw.Draw(layer, "RGBA")
    cx, cy = x0 + 48, y0 + y_shift + 48
    d.ellipse((cx - 19, cy - 19, cx + 19, cy + 19), fill=rgba(accent, int(245 * opacity)))
    # Minimal in-app glyph.
    d.line((cx - 8, cy - 5, cx + 8, cy - 5), fill=(255, 255, 255, int(230 * opacity)), width=2)
    d.line((cx - 8, cy + 1, cx + 6, cy + 1), fill=(255, 255, 255, int(230 * opacity)), width=2)
    d.line((cx - 8, cy + 7, cx + 3, cy + 7), fill=(255, 255, 255, int(230 * opacity)), width=2)
    draw_text(layer, (x0 + 82, y0 + y_shift + 39), label, font(15, "bold"), rgba(INK, int(255 * opacity)))
    draw_text(layer, (x0 + 82, y0 + y_shift + 63), detail, font(12, "regular"), rgba(MUTED, int(240 * opacity)))


def anywhere_scene(base: Image.Image, t: float):
    a = alpha_window(t, 6.6, 9.05, 0.35)
    if a <= 0:
        return
    layer = Image.new("RGBA", base.size)
    title_p = out_cubic(between(t, 6.65, 7.18))
    draw_text(layer, (640, 118 + (1 - title_p) * 18), "Write better. Anywhere.", font(40, "serif_bold"), rgba(WHITE, int(255 * a * title_p)), anchor="ma")
    draw_text(layer, (640, 168), "One shortcut follows you across every app.", font(16, "regular"), rgba("#b9c2cf", int(255 * a * title_p)), anchor="ma")
    p1 = between(t, 6.9, 7.5)
    p2 = between(t, 7.05, 7.65)
    p3 = between(t, 7.2, 7.8)
    orbit_card(layer, (110, 260, 430, 372), "EMAIL", "Clearer replies", "#4f7cae", a, p1)
    orbit_card(layer, (480, 314, 800, 426), "NOTES", "Polished ideas", GREEN_2, a, p2)
    orbit_card(layer, (850, 260, 1170, 372), "BROWSER", "Understand anything", "#8f5965", a, p3)
    d = ImageDraw.Draw(layer, "RGBA")
    ring_p = smooth(between(t, 7.35, 8.15))
    d.ellipse((582, 482, 698, 598), outline=rgba(GOLD, int(210 * a * ring_p)), width=2)
    d.ellipse((594, 494, 686, 586), fill=rgba(NAVY_2, int(250 * a * ring_p)), outline=rgba(GREEN_2, int(255 * a * ring_p)), width=2)
    draw_text(layer, (640, 540), "F9", font(25, "bold"), rgba(WHITE, int(255 * a * ring_p)), anchor="mm")
    draw_text(layer, (640, 632), "SELECT  •  PRESS  •  REPLACE", font(12, "bold"), rgba(GOLD, int(230 * a * ring_p)), anchor="ma")
    base.alpha_composite(layer)


def shield(layer, cx, cy, size, opacity):
    d = ImageDraw.Draw(layer, "RGBA")
    s = size
    pts = [(cx, cy - s * .56), (cx + s * .42, cy - s * .37), (cx + s * .36, cy + s * .17), (cx, cy + s * .57), (cx - s * .36, cy + s * .17), (cx - s * .42, cy - s * .37)]
    d.polygon(pts, fill=rgba(GREEN, int(235 * opacity)), outline=rgba(GOLD, int(255 * opacity)))
    d.line((cx - s * .19, cy, cx - s * .05, cy + s * .14, cx + s * .23, cy - s * .18), fill=rgba(WHITE, int(255 * opacity)), width=max(2, int(s * .06)), joint="curve")


def privacy_scene(base: Image.Image, t: float):
    a = alpha_window(t, 8.55, 10.25, 0.25)
    if a <= 0:
        return
    layer = Image.new("RGBA", base.size)
    p = out_cubic(between(t, 8.62, 9.2))
    shield(layer, 362, 360 + (1 - p) * 30, 190, a * p)
    draw_text(layer, (568, 234), "YOUR WORDS\nSTAY YOURS.", font(47, "serif_bold"), rgba(WHITE, int(255 * a * p)), spacing=1)
    draw_text(layer, (572, 357), "Run fully offline with a local AI model.", font(18, "medium"), rgba("#c2cad5", int(255 * a * p)))
    for i, label in enumerate(("NO TELEMETRY", "NO ANALYTICS", "LOCAL-FIRST")):
        x = 572 + (i % 2) * 190
        y = 418 + (i // 2) * 54
        rounded(layer, (x, y, x + 168, y + 36), 11, rgba("#223554", int(220 * a * p)), rgba(GREEN_2, int(100 * a * p)), 1)
        draw_text(layer, (x + 84, y + 18), label, font(11, "bold"), rgba(GOLD if i == 2 else WHITE, int(255 * a * p)), anchor="mm")
    base.alpha_composite(layer)


def end_card(base: Image.Image, t: float):
    a = smooth(between(t, 10.12, 10.38))
    if a <= 0:
        return
    layer = Image.new("RGBA", base.size)
    # Dark veil makes the final brand lockup land cleanly.
    ImageDraw.Draw(layer).rectangle((0, 0, W, H), fill=(9, 15, 27, int(205 * a)))
    p = out_cubic(between(t, 10.2, 10.72))
    cover_icon(layer, 640, 238 - (1 - p) * 24, 138, int(255 * a * p), int(160 * a * p))
    draw_text(layer, (640, 377), "VEDIUM", font(42, "serif_bold"), rgba(WHITE, int(255 * a * p)), anchor="ma")
    draw_text(layer, (640, 430), "BETTER WORDS. ONE KEY AWAY.", font(14, "bold"), rgba(GOLD, int(250 * a * p)), anchor="ma")
    d = ImageDraw.Draw(layer, "RGBA")
    lw = int(194 * p)
    d.line((640 - lw / 2, 473, 640 + lw / 2, 473), fill=rgba(GREEN_2, int(220 * a)), width=2)
    draw_text(layer, (640, 520), "A local-first writing assistant for Windows", font(16, "regular"), rgba("#bfc7d3", int(255 * a * p)), anchor="ma")
    # Final fade to black in the last 8 frames.
    fade_out = smooth(between(t, 11.65, 12.0))
    if fade_out:
        d.rectangle((0, 0, W, H), fill=(0, 0, 0, int(255 * fade_out)))
    base.alpha_composite(layer)


def render_frame(index: int):
    t = index / FPS
    frame = background(t)
    intro(frame, t)
    editor_scene(frame, t)
    toast_scene(frame, t)
    anywhere_scene(frame, t)
    privacy_scene(frame, t)
    end_card(frame, t)
    frame.convert("RGB").save(FRAMES / f"{index:04d}.png", compress_level=2)


def make_audio():
    sr = 48_000
    n = int(DURATION * sr)
    tt = np.arange(n, dtype=np.float64) / sr
    audio = np.zeros(n, dtype=np.float64)

    # Warm, restrained ambient bed in a C-major voicing.
    pad_env = np.sin(np.pi * np.clip(tt / DURATION, 0, 1)) ** 0.65
    for freq, amp, phase in ((65.41, .055, 0), (98.0, .032, .4), (130.81, .023, 1.1), (164.81, .018, 1.8)):
        audio += amp * np.sin(2 * np.pi * freq * tt + phase + .08 * np.sin(2 * np.pi * .12 * tt)) * pad_env

    rng = np.random.default_rng(12730)

    def add_chime(at, freq, length=.62, amp=.17):
        start, count = int(at * sr), int(length * sr)
        x = np.arange(count) / sr
        env = (1 - np.exp(-x * 80)) * np.exp(-x * 6.2)
        tone = np.sin(2 * np.pi * freq * x) + .42 * np.sin(2 * np.pi * freq * 2.01 * x)
        audio[start:start + count] += tone * env * amp

    def add_click(at, amp=.22):
        start, count = int(at * sr), int(.055 * sr)
        x = np.arange(count) / sr
        tone = (rng.standard_normal(count) * .36 + np.sin(2 * np.pi * 620 * x)) * np.exp(-x * 58)
        audio[start:start + count] += tone * amp

    def add_whoosh(at, length=.52, amp=.11):
        start, count = int(at * sr), int(length * sr)
        noise = rng.standard_normal(count)
        kernel = np.ones(27) / 27
        soft = np.convolve(noise, kernel, mode="same")
        x = np.linspace(0, 1, count)
        env = np.sin(np.pi * x) ** 1.5
        audio[start:start + count] += soft * env * amp

    add_chime(.22, 392, .85, .14)
    add_chime(.34, 523.25, .8, .09)
    add_whoosh(1.55, .65, .14)
    add_click(3.45, .23)
    add_whoosh(3.7, .65, .18)
    add_chime(4.12, 659.25, .72, .12)
    add_click(5.48, .18)
    add_whoosh(6.48, .7, .13)
    add_chime(7.35, 523.25, .68, .09)
    add_whoosh(8.42, .62, .12)
    add_chime(10.02, 392, 1.05, .13)
    add_chime(10.14, 587.33, .95, .1)

    fade = np.ones(n)
    fade[: int(.18 * sr)] = np.linspace(0, 1, int(.18 * sr))
    fade[-int(.55 * sr):] = np.linspace(1, 0, int(.55 * sr))
    audio *= fade
    peak = max(.001, np.max(np.abs(audio)))
    audio = np.tanh(audio / peak * 1.35) * .72
    stereo = np.column_stack((audio, np.roll(audio, 83)))
    pcm = np.int16(np.clip(stereo, -1, 1) * 32767)
    with wave.open(str(ROOT / "soundtrack.wav"), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        wav.writeframes(pcm.tobytes())


def main():
    if FRAMES.exists():
        shutil.rmtree(FRAMES)
    FRAMES.mkdir(parents=True)
    make_audio()
    for i in range(FRAME_COUNT):
        render_frame(i)
        if i % FPS == 0:
            print(f"Rendered {i // FPS:02d}/{DURATION}s", flush=True)
    print(f"Rendered {FRAME_COUNT} frames to {FRAMES}")


if __name__ == "__main__":
    main()
