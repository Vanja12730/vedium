"""Vedium (spell_daemon) package.

The package is composed of focused submodules that import one another with
ordinary explicit imports (``from spell_daemon.X import Y``).  Nothing here
rebinds ``sys.modules`` or proxies attribute access; reach into a submodule
directly when you need its internals (e.g. ``spell_daemon.llm._clean_llm_output``).

Public entry points:
    SpellDaemon -- the tray daemon (see spell_daemon.daemon)
    Key         -- pynput key enum re-exported for convenience
"""

# Import submodules in dependency order so the whole package is initialized on
# ``import spell_daemon`` and each is reachable as ``spell_daemon.<name>``.
from spell_daemon import constants
from spell_daemon import prompt_builder
from spell_daemon import config
from spell_daemon import buffer
from spell_daemon import ui_thread
from spell_daemon import ui_widgets
from spell_daemon import replacement
from spell_daemon import llm_state
from spell_daemon import llm_postprocess
from spell_daemon import llm_backends
from spell_daemon import llm
from spell_daemon import ui_settings
from spell_daemon import ui_chat
from spell_daemon import ui_toasts
from spell_daemon import ui_tray
from spell_daemon import daemon

from spell_daemon.daemon import SpellDaemon
from spell_daemon.constants import Key

__all__ = ["SpellDaemon", "Key"]
