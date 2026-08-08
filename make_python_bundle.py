# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

r"""Build the portable Python bundle that ships inside the Vedium installer.

Creates  python_embed\  next to this script:
  - official python.org "embeddable" Python (no registry, no PATH changes)
  - tkinter runtime grafted in from THIS interpreter (the embeddable build
    omits it, and the whole Vedium UI is Tk)
  - pip bootstrapped, UI packages (pystray, pillow, pynput) pre-installed
  - python312._pth set up so "python -m vedium" works from the install
    folder ('..' puts {app} on sys.path when the bundle lives in {app}\python)

llama-cpp-python is NOT bundled — the right wheel depends on the end user's
CPU/GPU, so the installer picks it at install time.

Run with the same minor version the bundle targets (tkinter files must match):

    py -3.12 make_python_bundle.py

Then compile Vedium.iss. Downloads are cached in build_cache\.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

PY_VERSION = "3.12.9"
EMBED_ZIP_NAME = f"python-{PY_VERSION}-embed-amd64.zip"
EMBED_ZIP_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/{EMBED_ZIP_NAME}"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
UI_PACKAGES = ["pystray", "pillow", "pynput"]

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "build_cache"
BUNDLE = ROOT / "python_embed"


def fetch(url: str, dest: Path) -> None:
    if dest.exists():
        print(f"  cached: {dest.name}")
        return
    print(f"  downloading {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.rename(dest)


def run_bundle_python(*args: str) -> None:
    subprocess.run([str(BUNDLE / "python.exe"), *args], check=True)


def main() -> None:
    if sys.version_info[:2] != (3, 12):
        sys.exit("Run with Python 3.12 (py -3.12 make_python_bundle.py) — "
                 "the tkinter runtime is copied from this interpreter.")

    print("[1/6] Fetching embeddable Python + get-pip")
    fetch(EMBED_ZIP_URL, CACHE / EMBED_ZIP_NAME)
    fetch(GET_PIP_URL, CACHE / "get-pip.py")

    print("[2/6] Extracting fresh bundle")
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    with zipfile.ZipFile(CACHE / EMBED_ZIP_NAME) as zf:
        zf.extractall(BUNDLE)

    print("[3/6] Writing python312._pth")
    # Relative entries resolve against the bundle dir:
    #   .   -> the bundle itself (_tkinter.pyd and friends)
    #   ..  -> the bundle's parent: {app} once installed, repo root in dev,
    #          which is what makes "-m vedium" importable
    (BUNDLE / "python312._pth").write_text(
        "python312.zip\n.\n..\nLib\\site-packages\nimport site\n",
        encoding="ascii",
    )

    print("[4/6] Grafting tkinter from", sys.base_prefix)
    base = Path(sys.base_prefix)
    for name in ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "zlib1.dll"):
        src = base / "DLLs" / name
        if src.exists():
            shutil.copy2(src, BUNDLE / name)
        elif name != "zlib1.dll":  # zlib1 is optional on some builds
            sys.exit(f"tkinter runtime file missing on this machine: {src}")
    shutil.copytree(base / "tcl", BUNDLE / "tcl",
                    ignore=shutil.ignore_patterns("demos", "__pycache__"))
    site = BUNDLE / "Lib" / "site-packages"
    site.mkdir(parents=True)
    shutil.copytree(base / "Lib" / "tkinter", site / "tkinter",
                    ignore=shutil.ignore_patterns("__pycache__"))

    print("[5/6] Bootstrapping pip + installing UI packages")
    run_bundle_python(str(CACHE / "get-pip.py"),
                      "--quiet", "--no-warn-script-location")
    run_bundle_python("-m", "pip", "install", "--quiet",
                      "--disable-pip-version-check",
                      "--no-warn-script-location", *UI_PACKAGES)

    print("[6/6] Smoke test (tkinter + UI packages + vedium import)")
    run_bundle_python(
        "-c",
        "import tkinter, tkinter.ttk, pystray, pynput; "
        "from PIL import Image; "
        "import vedium; "
        "print('bundle OK  tk', tkinter.TkVersion)",
    )

    size_mb = sum(f.stat().st_size for f in BUNDLE.rglob("*") if f.is_file()) / 2**20
    print(f"\nDone: {BUNDLE}  ({size_mb:.0f} MB uncompressed)")
    print("Now compile Vedium.iss.")


if __name__ == "__main__":
    main()
