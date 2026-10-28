"""The file share's launch point: starts share_server.py (same arguments)."""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
target = os.path.join(HERE, "share_server.py")
sys.argv = [target] + sys.argv[1:]
runpy.run_path(target, run_name="__main__")
