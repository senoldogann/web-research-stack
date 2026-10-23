"""The .env a run of this package reads: the one in the folder it was started in, and no other.

python-dotenv's ``load_dotenv()`` with no path takes the first ``.env`` above the folder of the module that calls
it, so an installed package read whatever ``.env`` lay above its install folder: in Dot's own layout Dot's key file,
and elsewhere a ``.env`` that a program could leave in a writable parent folder (a ``NODE_OPTIONS`` in it reached the
headless browser's driver). Only the working folder's ``.env`` is read now, never one above it, and
``PYTHON_DOTENV_DISABLED`` turns even that off, as Dot does for the server it starts.
"""

from __future__ import annotations

from pathlib import Path

import dotenv


def load_local_env() -> bool:
    """Read the working folder's ``.env`` into the environment, never over what is already set.

    Returns whether one was read.
    """
    return dotenv.load_dotenv(Path.cwd() / ".env", override=False)
