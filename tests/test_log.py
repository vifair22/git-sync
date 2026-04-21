"""Tests for logging setup."""
from __future__ import annotations

import logging

import pytest

from git_sync import log


@pytest.fixture(autouse=True)
def _reset_configured(monkeypatch):
    monkeypatch.setattr(log, "_CONFIGURED", False)
    original_handlers = logging.getLogger().handlers[:]
    original_level = logging.getLogger().level
    yield
    logging.getLogger().handlers[:] = original_handlers
    logging.getLogger().setLevel(original_level)


def test_configure_sets_level():
    log.configure("DEBUG")
    assert logging.getLogger().level == logging.DEBUG


def test_configure_is_idempotent():
    log.configure("DEBUG")
    handler_count = len(logging.getLogger().handlers)
    log.configure("WARNING")
    assert len(logging.getLogger().handlers) == handler_count
    assert logging.getLogger().level == logging.DEBUG


def test_get_returns_named_logger():
    logger = log.get("git_sync.test")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "git_sync.test"
