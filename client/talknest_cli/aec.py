"""Echo cancellation backends.

The implementation lives in ``server/orchestrator/aec.py`` so the server
(single copy) and this client share one module; this file only re-exports
it. The repository layout is required: the server package sits two
directories up from this one.
"""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "server"))

from orchestrator.aec import (BLOCK_LEN, BLOCK_SHIFT, DtlnAec,  # noqa: E402,F401
                              EchoCanceller, NullAec, erle_db)

sys.path.pop(0)
