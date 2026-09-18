"""Test-suite bootstrap: making ``tmp_path`` work in restricted environments.

pytest's ``tmp_path`` factory computes its root as
``<tempdir>/pytest-of-<user>`` and creates it with::

    rootdir.mkdir(exist_ok=True)

``parents`` defaults to ``False`` there, and some sandboxed environments route
that particular call through a broker which performs a raw ``mkdir`` and so
raises ``EEXIST`` whenever the directory already exists — ``exist_ok=True`` is
not honoured. The symptom is nasty: the first run passes and every subsequent
run fails at fixture setup with a traceback pointing at pytest internals.

This module redirects the temp root when that happens. Two mechanisms, in
order:

1. ``KNOWMEMO_TEST_TMPDIR`` — an explicit override, always honoured.
2. A probe that mirrors pytest's exact call on a session-unique path. If it
   fails, the temp root moves to a session-unique directory under the
   project's git-ignored ``.tmp/``.

Session-unique paths are deliberate: nothing here ever deletes anything, so
there is no cleanup step that could go wrong. On a normal machine neither
mechanism changes anything.
"""

from __future__ import annotations

import getpass
import os
import tempfile
import time
from pathlib import Path

import pytest

OVERRIDE_ENV_VAR = "KNOWMEMO_TEST_TMPDIR"
FALLBACK_DIR_NAME = ".tmp"


def _current_user() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):  # pragma: no cover - unusual environments
        return "unknown"


def _session_suffix() -> str:
    return f"{int(time.time())}-{os.getpid()}"


def _system_temp_can_host_pytest() -> bool:
    """Reproduce pytest's exact failure mode: ``mkdir(exist_ok=True)`` on an
    *existing* directory.

    The probe creates a directory and then repeats the call. A fresh path alone
    would always succeed and therefore prove nothing — the broker only
    misbehaves when the target already exists. Nothing is removed afterwards.
    """
    probe = Path(tempfile.gettempdir()) / f"pytest-of-{_current_user()}-probe-{_session_suffix()}"
    try:
        probe.mkdir(exist_ok=True)
    except OSError:
        return False
    try:
        probe.mkdir(exist_ok=True)
    except OSError:
        return False
    return True


def pytest_configure(config: pytest.Config) -> None:
    if config.option.basetemp:
        return

    override = os.environ.get(OVERRIDE_ENV_VAR)
    if override:
        Path(override).mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = override
        return

    if _system_temp_can_host_pytest():
        return

    fallback = (
        Path(__file__).resolve().parent.parent / FALLBACK_DIR_NAME / f"pytest-{_session_suffix()}"
    )
    fallback.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(fallback)
