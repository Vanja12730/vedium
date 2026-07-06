import sys
import os

# Add the parent directory of 'spell_daemon' to sys.path so that 'import spell_daemon' works correctly
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from spell_daemon.daemon import SpellDaemon

if __name__ == "__main__":
    SpellDaemon().run()
