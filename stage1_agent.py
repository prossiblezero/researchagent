"""Compatibility shim for the focused package layout.

Use ``main.py`` for the command-line interface. Imports from the original
single-file stage 1 module continue to work while implementation lives in
``research_agent/``.
"""

from research_agent import *  # noqa: F401,F403


if __name__ == "__main__":
    from main import main

    raise SystemExit(main())
