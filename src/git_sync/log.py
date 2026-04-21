"""Logging setup for git-sync.

All log output goes to stderr so stdout can stay reserved for tool-style
consumers without interleaved noise.
"""
from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def configure(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        ),
    )
    root.handlers[:] = [handler]
    _CONFIGURED = True


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
