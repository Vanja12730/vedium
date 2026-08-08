# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Vedium (vedium) package.

The package is composed of focused submodules that import one another with
ordinary explicit imports (``from vedium.X import Y``).  Nothing here
rebinds ``sys.modules`` or proxies attribute access; reach into a submodule
directly when you need its internals (e.g. ``vedium.llm._clean_llm_output``).

Public entry points:
    SpellDaemon -- the tray daemon (see vedium.daemon)
    Key         -- pynput key enum re-exported for convenience
"""

# Import submodules in dependency order so the whole package is initialized on
# ``import vedium`` and each is reachable as ``vedium.<name>``.
from vedium import constants
from vedium import prompt_builder
from vedium import config
from vedium import buffer
from vedium import ui_thread
from vedium import ui_widgets
from vedium import replacement
from vedium import llm_state
from vedium import llm_postprocess
from vedium import llm_backends
from vedium import llm
from vedium import ui_settings
from vedium import ui_chat
from vedium import ui_toasts
from vedium import ui_tray
from vedium import daemon

from vedium.daemon import SpellDaemon
from vedium.constants import Key

__all__ = ["SpellDaemon", "Key"]
