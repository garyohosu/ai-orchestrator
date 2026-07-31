"""Test package bootstrap: make sibling orchestrator/*.py importable.

orchestrator.py is meant to be run directly as a script (SPEC.md 13章), so
the other orchestrator/*.py modules are plain flat modules, not a Python
package. Test discovery (``python -m unittest discover``) imports this
file before any test module, so inserting orchestrator/'s own directory
here makes ``import config``, ``import paths``, etc. resolve the same way
they do when orchestrator.py bootstraps itself at startup.
"""

import sys
from pathlib import Path

_ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent
if str(_ORCHESTRATOR_DIR) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRATOR_DIR))
