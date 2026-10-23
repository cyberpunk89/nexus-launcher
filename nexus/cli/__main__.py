"""`python -m nexus.cli`, or this file run by path (how the Windows build
starts the CLI from source: no shebang, any working directory)."""
import sys
from pathlib import Path

if not __package__:
    # Run as a file: Python put this folder first on the path; the source
    # folder belongs there instead.
    sys.path[0] = str(Path(__file__).resolve().parents[2])

from nexus.cli import main  # noqa: E402

main()
