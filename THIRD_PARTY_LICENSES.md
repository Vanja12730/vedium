# Third-party licenses

Vedium itself is licensed under the GNU General Public License v3.0
(see [LICENSE](LICENSE)). It depends on the components below, each under its
own license. The Windows installer additionally **bundles** the ones marked
*bundled*, either inside its private Python runtime or local-model engine.

Every component listed here is GPL-3.0 compatible, so the combined work can be
distributed under the GPL-3.0.

## Runtime dependencies

| Component | License | Bundled by the installer |
|---|---|---|
| [pynput](https://github.com/moses-palmer/pynput) | LGPL-3.0 | yes |
| [pystray](https://github.com/moses-palmer/pystray) | LGPL-3.0 | yes |
| [Pillow](https://python-pillow.org/) | MIT-CMU | yes |
| [CPython](https://www.python.org/) (embeddable build + Tcl/Tk) | PSF-2.0 | yes |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | MIT — see `licenses/llama.cpp-LICENSE.txt` | downloaded by setup when a local engine is selected |
| [NVIDIA CUDA runtime libraries](https://docs.nvidia.com/cuda/eula/index.html) | NVIDIA Software License Agreement — see `licenses/NVIDIA-CUDA-NOTICE.txt` | downloaded by setup only for the NVIDIA engine |

Models you download (e.g. Gemma 4, Gemma 3, Qwen3) carry their own separate licenses and
acceptable-use terms. Vedium neither includes nor relicenses any model weights.

## Note on the LGPL components

pynput and pystray are licensed under the **LGPL-3.0**, which section 2 of that
licence expressly permits being conveyed under the GPL-3.0. Combining them with
Vedium is therefore straightforward — no extra obligation beyond keeping their
own notices intact.

Because the installer redistributes them, you must be able to replace those
libraries with a modified version. You can: they are installed as readable
`.py` sources under

    <install dir>\python_embed\Lib\site-packages\

and may be edited or replaced in place. Their full licence texts ship in the
matching `*.dist-info` directories alongside them.

If you build the installer yourself, `make_python_bundle.py` fetches these
packages from PyPI at build time — it does not vendor modified copies.
