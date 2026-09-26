# Vedium product teaser

A 12-second, 1280×720 launch-style teaser using Vedium's real Parchment theme,
F9 writing action, correction-toast interaction, and local-first positioning.
The soundtrack is procedurally generated and contains no third-party media.

## Rebuild

```powershell
py -3.12 -m pip install --target teaser\_vendor imageio-ffmpeg
py -3.12 teaser\make_teaser.py
py -3.12 teaser\encode_video.py
```

The final deliverable is `teaser/vedium-product-teaser.mp4`.
Generated frame PNGs and the local encoder package can be deleted after a
successful encode; they are intentionally not retained with this sample.
