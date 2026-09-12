"""Config loading (server/config.yaml) with env override."""

import os
from pathlib import Path

import yaml

DEFAULT_PATHS = [
    Path(__file__).resolve().parent.parent / "config.yaml",
]

# Weights live in a sibling "model" directory of the checkout.
_FALLBACK_MODEL_ROOT = Path(__file__).resolve().parents[2].parent / "model"


def model_root() -> Path:
    """Root directory holding the model weights.

    Override with TALKNEST_MODEL_ROOT when the weights live elsewhere.
    """
    if env := os.environ.get("TALKNEST_MODEL_ROOT"):
        return Path(env)
    return _FALLBACK_MODEL_ROOT


def load() -> dict:
    path = None
    if env := os.environ.get("TALKNEST_CONFIG"):
        path = Path(env)
    else:
        for candidate in DEFAULT_PATHS:
            if candidate.exists():
                path = candidate
                break
    if path is None:
        raise RuntimeError("config.yaml not found; set TALKNEST_CONFIG")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
