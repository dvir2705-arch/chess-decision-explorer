"""Run provenance for a Step 5C-lite human-reference run.

The manifest is written **before** the scan starts and rewritten when it
finishes, so an interrupted run still leaves a record of exactly what it was
trying to do and how far it got. A reader who has only the manifest must be
able to tell whether the run is reproducible and what it is allowed to claim
-- in particular whether the accepted-game target was actually met.

Nothing here is invented: a missing Git commit is recorded as `null` rather
than guessed at, and the corpus is declared, never hashed.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chess

from . import MANIFEST_VERSION, PHASE_ID, POSITION_KEY_SEMANTICS_VERSION, SCOPE_NOTE

REQUIRED_MANIFEST_FIELDS: tuple[str, ...] = (
    "manifest_version",
    "phase",
    "scope_note",
    "run_id",
    "code_commit",
    "code_tree_dirty",
    "source_id",
    "source_declaration",
    "eligibility_filters",
    "target_eligible_games",
    "records_scanned",
    "eligible_games_accepted",
    "target_met",
    "rejections",
    "scan_termination",
    "recurring_position_threshold",
    "personal_cohort",
    "personal_target_set",
    "position_key_semantics_version",
    "started_at",
    "finished_at",
)
"""Every field a human-reference manifest must carry. A test asserts a
written manifest contains all of them."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git_commit(repo_root: Path) -> tuple[str | None, bool | None]:
    """``(commit, dirty)`` for ``repo_root``, or ``(None, None)`` when this is
    not a usable Git checkout. A missing commit is recorded, never invented."""
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None, None
    return commit, bool(status.strip())


@dataclass(slots=True)
class ReferenceRunManifest:
    """One human-reference run's complete provenance."""

    run_id: str
    code_commit: str | None
    code_tree_dirty: bool | None
    source_id: str
    source_declaration: dict[str, Any]
    eligibility_filters: dict[str, Any]
    target_eligible_games: int
    scanner_declaration: dict[str, Any]
    target_set_declaration: dict[str, Any]
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    scan_result: dict[str, Any] | None = None
    runtime: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        scan = self.scan_result or {}
        counters = scan.get("counters", {})
        return {
            "manifest_version": MANIFEST_VERSION,
            "phase": PHASE_ID,
            "scope_note": SCOPE_NOTE,
            "run_id": self.run_id,
            "code_commit": self.code_commit,
            "code_tree_dirty": self.code_tree_dirty,
            "source_id": self.source_id,
            "source_declaration": self.source_declaration,
            "scanner": self.scanner_declaration,
            "eligibility_filters": self.eligibility_filters,
            # The target is a count of ELIGIBLE ACCEPTED games. It is kept
            # next to the scanned count and the accepted count on purpose:
            # no reader of this manifest should ever confuse the three.
            "target_eligible_games": self.target_eligible_games,
            "records_scanned": counters.get("records_scanned"),
            "eligible_games_accepted": counters.get("games_accepted"),
            "target_met": scan.get("target_met"),
            "shortfall": scan.get("shortfall"),
            "rejections": counters.get("rejections"),
            "scan_termination": scan.get("scan_termination"),
            "scanned_prefix_declared": scan.get("scanned_prefix_declared"),
            "decisions_examined": counters.get("decisions_examined"),
            "decisions_matched": counters.get("decisions_matched"),
            "match_rate": counters.get("match_rate"),
            "target_positions": scan.get("target_positions"),
            "matched_positions": scan.get("matched_positions"),
            "position_coverage_rate": scan.get("position_coverage_rate"),
            "recurring_position_threshold": self.target_set_declaration.get(
                "min_distinct_games"
            ),
            "recurrence_criterion": self.target_set_declaration.get(
                "recurrence_criterion"
            ),
            "personal_cohort": self.target_set_declaration.get("personal_cohort"),
            "personal_target_set": self.target_set_declaration,
            "position_key_semantics_version": POSITION_KEY_SEMANTICS_VERSION,
            # Structural disclaimers, read off what this milestone can do at
            # all rather than asserted as a slogan.
            "engine_evidence_used": False,
            "calibrated": False,
            "ml_trained": False,
            "rating_matched_cohort": False,
            "significance_tested": False,
            "environment": {
                "python": sys.version.split()[0],
                "python_chess": chess.__version__,
                "platform": platform.platform(),
                "cpu_count": _cpu_count(),
            },
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "runtime": self.runtime,
        }

    def write(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _cpu_count() -> int | None:
    try:
        import os

        return os.cpu_count()
    except Exception:  # pragma: no cover - defensive only
        return None
