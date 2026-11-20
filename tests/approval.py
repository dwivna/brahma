"""Tests stand in for the device owner: the system prompt (presence._authenticate) is replaced, nothing else."""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def owner_confirms(answer: bool = True):
    with patch("brahma.presence._authenticate", return_value=(answer, "" if answer else "Authentication cancelled.")) as prompt:
        yield prompt


def approve(session) -> str:
    with owner_confirms():
        return session.approve()
