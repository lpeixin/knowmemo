"""Environment diagnostics backing ``knowmemo doctor``.

Each check reports one of four outcomes:

``OK``    the dependency is present and usable
``WARN``  usable but not ideal, or optional and absent
``FAIL``  required and broken — ``doctor`` exits non-zero
``SKIP``  not applicable given the current configuration

Three checks exist specifically because of findings recorded in
``docs/plan.md``:

* WeKnora is unreachable and Docker is absent on this machine (F6) — both are
  reported as ``WARN``, never ``FAIL``, because the MVP ingestion path is
  required to work without them (design doc §17).
* Ollama is running but has no embedding model pulled (F7).
* The WeChat data directory is protected by macOS TCC (F8), which is a ``FAIL``
  with an actionable remedy rather than a bare permission error.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from knowmemo.config.settings import Settings
from knowmemo.storage.database import create_db_engine, data_root, init_db

MINIMUM_PYTHON = (3, 11)
HTTP_PROBE_TIMEOUT = 3.0


class CheckStatus(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass
class CheckOutcome:
    name: str
    status: CheckStatus
    detail: str
    remedy: str | None = None

    @property
    def symbol(self) -> str:
        return {"OK": "✓", "WARN": "!", "FAIL": "✗", "SKIP": "·"}[str(self.status)]


@dataclass
class DoctorReport:
    outcomes: list[CheckOutcome] = field(default_factory=list)

    def add(
        self,
        name: str,
        status: CheckStatus,
        detail: str,
        remedy: str | None = None,
    ) -> None:
        self.outcomes.append(CheckOutcome(name, status, detail, remedy))

    @property
    def failures(self) -> list[CheckOutcome]:
        return [o for o in self.outcomes if o.status is CheckStatus.FAIL]

    @property
    def warnings(self) -> list[CheckOutcome]:
        return [o for o in self.outcomes if o.status is CheckStatus.WARN]

    @property
    def ok_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status is CheckStatus.OK)

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0


def _http_get(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    """Minimal HTTP GET. Returns ``(status, body)``; raises on transport failure."""
    request = urllib.request.Request(url, headers=headers or {})  # noqa: S310 - local only
    with urllib.request.urlopen(request, timeout=HTTP_PROBE_TIMEOUT) as response:  # noqa: S310
        return response.status, response.read().decode("utf-8", errors="replace")


def check_python(report: DoctorReport) -> None:
    current = sys.version_info
    if current[:2] >= MINIMUM_PYTHON:
        report.add("Python", CheckStatus.OK, f"{current.major}.{current.minor}.{current.micro}")
    else:
        report.add(
            "Python",
            CheckStatus.FAIL,
            f"{current.major}.{current.minor} is below the required "
            f"{MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}",
            remedy=f"Install Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer.",
        )


def check_config(report: DoctorReport, settings: Settings) -> None:
    if settings.source_path is None:
        report.add(
            "Configuration",
            CheckStatus.OK,
            "built-in defaults (no config file found)",
        )
        return
    report.add("Configuration", CheckStatus.OK, str(settings.source_path))


def check_data_dirs(report: DoctorReport, settings: Settings) -> None:
    """Verify the data tree exists and is writable.

    Deliberately does not write-then-delete a probe file: a diagnostic command
    should not mutate state, and a delete is the most intrusive kind of probe.
    Creating the four directories is itself the write test, and ``os.access``
    covers the case where they already exist.
    """
    root = data_root(settings)
    try:
        root.mkdir(parents=True, exist_ok=True)
        for name in ("raw", "normalized", "knowledge", "logs"):
            (root / name).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report.add(
            "Data directory",
            CheckStatus.FAIL,
            f"{root} is not writable: {exc}",
            remedy="Point storage.database_url at a writable directory.",
        )
        return

    if not os.access(root, os.W_OK | os.X_OK):
        report.add(
            "Data directory",
            CheckStatus.FAIL,
            f"{root} exists but is not writable by this user",
            remedy="Fix ownership or permissions on the data directory.",
        )
        return

    report.add("Data directory", CheckStatus.OK, f"{root} (writable)")


def check_database(report: DoctorReport, settings: Settings) -> None:
    try:
        engine = create_db_engine(settings)
        init_db(engine)
        with engine.connect() as connection:
            tables = [
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
        engine.dispose()
    except Exception as exc:  # noqa: BLE001 - doctor must never raise
        report.add(
            "Metadata database",
            CheckStatus.FAIL,
            f"cannot initialise: {exc}",
            remedy="Check storage.database_url and filesystem permissions.",
        )
        return
    report.add(
        "Metadata database",
        CheckStatus.OK,
        f"{len(tables)} tables at {settings.storage.database_url}",
    )


def check_ollama(report: DoctorReport, settings: Settings) -> None:
    """Probe the local LLM runtime and report which models are present."""
    base_url = settings.llm.base_url.rstrip("/")
    try:
        status, body = _http_get(f"{base_url}/api/tags")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        report.add(
            "LLM runtime",
            CheckStatus.WARN,
            f"{base_url} unreachable ({exc.__class__.__name__})",
            remedy="Start Ollama, or point llm.base_url elsewhere. Ingestion works without it.",
        )
        return

    try:
        models = [m["name"] for m in json.loads(body).get("models", [])]
    except (json.JSONDecodeError, KeyError, TypeError):
        report.add("LLM runtime", CheckStatus.WARN, f"unexpected response (HTTP {status})")
        return

    if not models:
        report.add(
            "LLM runtime",
            CheckStatus.WARN,
            "reachable but no models pulled",
            remedy=f"Run: ollama pull {settings.llm.model}",
        )
        return

    configured = settings.llm.model
    if configured in models:
        report.add("LLM runtime", CheckStatus.OK, f"{configured} present ({len(models)} models)")
    else:
        report.add(
            "LLM runtime",
            CheckStatus.WARN,
            f"configured model {configured!r} not pulled; available: {', '.join(models)}",
            remedy=f"Run: ollama pull {configured}   (or set llm.model to one of the above)",
        )


def check_embedding(report: DoctorReport, settings: Settings) -> None:
    """An embedding model is required by WeKnora, not by KnowMemo itself."""
    if not settings.embedding.model:
        report.add(
            "Embedding model",
            CheckStatus.WARN,
            "not configured (embedding.model is null)",
            remedy=(
                "Required before WeKnora ingestion. Pull one and set embedding.model, "
                "e.g. ollama pull bge-m3"
            ),
        )
        return

    base_url = settings.embedding.base_url.rstrip("/")
    try:
        _, body = _http_get(f"{base_url}/api/tags")
        models = [m["name"] for m in json.loads(body).get("models", [])]
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        report.add(
            "Embedding model",
            CheckStatus.WARN,
            f"cannot verify {settings.embedding.model!r}: {exc.__class__.__name__}",
        )
        return

    if settings.embedding.model in models:
        report.add("Embedding model", CheckStatus.OK, settings.embedding.model)
    else:
        report.add(
            "Embedding model",
            CheckStatus.WARN,
            f"{settings.embedding.model!r} not pulled",
            remedy=f"Run: ollama pull {settings.embedding.model}",
        )


def check_weknora(report: DoctorReport, settings: Settings) -> None:
    """Optional at MVP: the ingestion path must work without WeKnora (§17)."""
    base_url = settings.weknora.base_url.rstrip("/")
    headers = {"X-API-Key": settings.weknora.api_key} if settings.weknora.api_key else {}
    try:
        status, _ = _http_get(f"{base_url}/api/v1/health", headers)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        report.add(
            "WeKnora",
            CheckStatus.WARN,
            f"{base_url} unreachable ({exc.__class__.__name__})",
            remedy=("Optional until milestone M3. Needs Docker; see docs/plan.md finding F6."),
        )
        return
    if settings.weknora.api_key is None:
        report.add(
            "WeKnora",
            CheckStatus.WARN,
            f"reachable (HTTP {status}) but weknora.api_key is unset",
            remedy="Set the X-API-Key value from the WeKnora account page.",
        )
        return
    report.add("WeKnora", CheckStatus.OK, f"reachable (HTTP {status})")


def check_docker(report: DoctorReport) -> None:
    """Reported for planning only — KnowMemo never requires Docker itself."""
    docker = shutil.which("docker")
    if docker:
        report.add("Docker", CheckStatus.OK, docker)
    else:
        report.add(
            "Docker",
            CheckStatus.WARN,
            "not installed",
            remedy="Only needed to run WeKnora locally (milestone M3).",
        )


def _probe_directory(path: Path, *, label: str, report: DoctorReport) -> None:
    if not path.exists():
        report.add(
            label,
            CheckStatus.FAIL,
            f"{path} does not exist",
            remedy="Fix the configured path.",
        )
        return
    try:
        os.listdir(path)
    except PermissionError:
        report.add(
            label,
            CheckStatus.FAIL,
            f"{path} exists but the OS denied access (macOS TCC)",
            remedy=(
                "Grant Full Disk Access to your terminal: System Settings → "
                "Privacy & Security → Full Disk Access."
            ),
        )
        return
    except OSError as exc:
        report.add(label, CheckStatus.FAIL, f"{path} unreadable: {exc}")
        return
    report.add(label, CheckStatus.OK, f"{path} (readable)")


def check_wechat(report: DoctorReport, settings: Settings) -> None:
    if not settings.wechat.enabled:
        report.add("WeChat source", CheckStatus.SKIP, "disabled in configuration")
        return

    data_path = settings.wechat.data_path
    if data_path is None:
        report.add(
            "WeChat source",
            CheckStatus.WARN,
            "enabled but wechat.data_path is not set",
            remedy="Run `knowmemo source add wechat` to configure it.",
        )
    else:
        _probe_directory(data_path, label="WeChat data", report=report)

    snapshot = settings.wechat.snapshot_path
    if snapshot is None:
        report.add(
            "WeChat snapshot",
            CheckStatus.WARN,
            "not configured (wechat.snapshot_path is null)",
            remedy=(
                "The connector reads a plaintext SQLite snapshot, never the live "
                "encrypted store. See docs/plan.md decision D4."
            ),
        )
    else:
        _probe_directory(snapshot, label="WeChat snapshot", report=report)


def check_sqlite_runtime(report: DoctorReport) -> None:
    report.add("SQLite", CheckStatus.OK, sqlite3.sqlite_version)


def run_doctor(settings: Settings) -> DoctorReport:
    """Run every check and return the aggregated report."""
    report = DoctorReport()
    check_python(report)
    check_config(report, settings)
    check_sqlite_runtime(report)
    check_data_dirs(report, settings)
    check_database(report, settings)
    check_ollama(report, settings)
    check_embedding(report, settings)
    check_weknora(report, settings)
    check_docker(report)
    check_wechat(report, settings)
    return report
