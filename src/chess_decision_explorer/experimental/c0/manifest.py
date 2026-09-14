"""The C0 run manifest: everything needed to reproduce or reject a run.

EXPERIMENTAL. The manifest is written before any engine work starts and
rewritten when the run finishes, so an interrupted run still leaves a record
of exactly what it was trying to do.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import chess

from ...assessment import ASSESSMENT_SEMANTICS_VERSION, ComparisonPolicy, SearchLevel
from ...engine import (
    ANALYSIS_PROFILE,
    ANALYSIS_PROFILE_FINGERPRINT,
    ENGINE_EVIDENCE_VERSION,
    POSITION_SEMANTICS_VERSION,
    EngineIdentity,
)
from . import C0_PHASE_ID, C0_SCOPE_NOTE
from .lowcost import policy_dict
from .sampling import GAME_IDENTITY_SCHEME, SAMPLING_REPRODUCIBILITY_GUARANTEE

MANIFEST_VERSION = "C0_RUN_MANIFEST_V1"

REQUIRED_MANIFEST_FIELDS: tuple[str, ...] = (
    "manifest_version",
    "phase",
    "scope_note",
    "code_commit",
    "code_tree_dirty",
    "source_identifier",
    "source_sampling_declaration",
    "sampling_source_id",
    "game_identity_scheme",
    "sampling_reproducibility_guarantee",
    "seed",
    "eligibility_filters",
    "sample_target",
    "engine_executable_sha256",
    "engine_uci_name",
    "engine_eval_file",
    "position_semantics_version",
    "evidence_contract_version",
    "assessment_semantics_version",
    "analysis_profile_fingerprint",
    "threads",
    "hash_mb",
    "production_policy",
    "production_budgets",
    "reference_ladder",
    "step_4b_calibration_status",
    "damage_conclusions_permitted",
    "started_at",
    "finished_at",
)
"""Every field a C0 manifest must carry. A test asserts a written manifest
contains all of them."""


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
class RunManifest:
    """One C0 run's complete provenance."""

    run_id: str
    code_commit: str | None
    code_tree_dirty: bool | None
    source_identifier: str
    sampling_source_id: str
    source_sampling_declaration: dict
    seed: int
    eligibility_filters: dict
    sample_target: int
    engine_identity: EngineIdentity
    threads: int
    hash_mb: int
    policy: ComparisonPolicy
    reference_ladder: tuple[SearchLevel, ...]
    audited_production_nodes: int
    discovery_audit_level: SearchLevel | None
    replay_only: bool
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    scan_termination: str | None = None
    sampling_counters: dict | None = None
    runtime: dict | None = None

    def as_dict(self) -> dict:
        return {
            "manifest_version": MANIFEST_VERSION,
            "phase": C0_PHASE_ID,
            "scope_note": C0_SCOPE_NOTE,
            "run_id": self.run_id,
            "code_commit": self.code_commit,
            "code_tree_dirty": self.code_tree_dirty,
            "source_identifier": self.source_identifier,
            # The exact identifier sampling used, kept as its own top-level
            # field: it is an INPUT to every deterministic draw, not just
            # provenance, so a run is only reproducible against this value.
            "sampling_source_id": self.sampling_source_id,
            "game_identity_scheme": GAME_IDENTITY_SCHEME,
            "sampling_reproducibility_guarantee": (
                SAMPLING_REPRODUCIBILITY_GUARANTEE
            ),
            "source_sampling_declaration": self.source_sampling_declaration,
            "seed": self.seed,
            "eligibility_filters": self.eligibility_filters,
            "sample_target": self.sample_target,
            "engine_executable_sha256": self.engine_identity.executable_sha256,
            "engine_uci_name": self.engine_identity.name,
            "engine_eval_file": self.engine_identity.eval_file,
            "engine_eval_file_provenance": (
                "Stockfish's reported default EvalFile. Step 4A does not support "
                "caller-selected external NNUE networks, so no external network "
                "digest participates in cache identity."
            ),
            "position_semantics_version": POSITION_SEMANTICS_VERSION,
            "evidence_contract_version": ENGINE_EVIDENCE_VERSION,
            "assessment_semantics_version": ASSESSMENT_SEMANTICS_VERSION,
            "analysis_profile_fingerprint": ANALYSIS_PROFILE_FINGERPRINT,
            "analysis_profile": dict(ANALYSIS_PROFILE),
            "threads": self.threads,
            "hash_mb": self.hash_mb,
            "production_policy": policy_dict(self.policy),
            "production_budgets": {
                "b1_nodes": self.policy.b1.nodes,
                "b2_nodes": self.policy.b2.nodes,
                "b3_nodes": self.policy.b3.nodes if self.policy.b3 else None,
            },
            "reference_ladder": [
                {"label": level.label, "nodes": level.nodes}
                for level in self.reference_ladder
            ],
            "audited_production_nodes": self.audited_production_nodes,
            "experimental_discovery_audit_level": (
                {
                    "label": self.discovery_audit_level.label,
                    "nodes": self.discovery_audit_level.nodes,
                }
                if self.discovery_audit_level is not None
                else None
            ),
            "replay_only": self.replay_only,
            # Structural, not a slogan: read straight off the policy this run
            # actually used. An UNCALIBRATED policy cannot yield
            # DAMAGE_SUPPORTED, so no pilot run can record an admissible
            # engine-damage conclusion.
            "step_4b_calibration_status": self.policy.calibration_status.value,
            "step_4b_calibration_id": self.policy.calibration_id,
            "damage_conclusions_permitted": self.policy.permits_damage_conclusion,
            "calibrated": False,
            "ml_trained": False,
            "personal_games_used": False,
            "environment": {
                "python": sys.version.split()[0],
                "python_chess": chess.__version__,
                "platform": platform.platform(),
                "processor": platform.processor(),
                "cpu_count": _cpu_count(),
            },
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "scan_termination": self.scan_termination,
            "sampling_counters": self.sampling_counters,
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
