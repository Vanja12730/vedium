"""Encode make_teaser.py's frame sequence with a project-local FFmpeg."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "_vendor"))

import imageio_ffmpeg


ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
output = ROOT / "vedium-product-teaser.mp4"
command = [
    ffmpeg,
    "-y",
    "-framerate", "24",
    "-i", str(ROOT / "frames" / "%04d.png"),
    "-i", str(ROOT / "soundtrack.wav"),
    "-c:v", "libx264",
    "-preset", "slow",
    "-crf", "17",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac",
    "-b:a", "192k",
    "-movflags", "+faststart",
    "-shortest",
    str(output),
]
subprocess.run(command, check=True)
print(f"ENCODED:{output}")
