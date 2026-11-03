"""Allows `python -m data_plane.data_edge_agent`."""

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
