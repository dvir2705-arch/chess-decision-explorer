"""C0 pilot orchestration: sample -> Step 4B -> stronger reference ladder.

EXPERIMENTAL. One sampled root at a time, streamed: a root is measured,
serialised, and dropped before the next one is read. Nothing accumulates the
corpus, and nothing here computes `R = S - G_reference` -- C0 does not freeze
a reference protocol.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass

from ...assessment import (
    CachedEvaluatorPool,
    ComparisonPolicy,
    DecisionAssessor,
    LeveledEvidenceProvider,
    MoveAssessment,
    SearchLevel,
)
from ...engine import (
    EngineAnalysisConfig,
    RequestValidationError,
    StockfishEvaluator,
    reconstruct_board,
)
from ...engine_cache import EvaluationCache
from .lowcost import WitnessResolution, assessment_dict, resolve_fixed_witness
from .reference import (
    ReferenceTrajectory,
    measure_reference_trajectory,
    unrestricted_discovery_audit,
)
from .sampling import SampledDecision


class ReplayMiss(RuntimeError):
    """Raised in replay-only mode when an evaluation is not already cached.

    Replay-only exists to prove the reproducibility requirement: the same
    stored primitive evidence, replayed through the same analysis code, must
    produce the same derived pilot measurements. Any engine call would mean
    the replay was not a replay.
    """


class CountingEvaluator:
    """Wraps a started Step 4A evaluator and counts real engine analyses.

    Cache hits never reach this wrapper's `evaluate`... they do not reach the
    engine at all, because `CachedEvaluator` sits above it. Counting here
    therefore separates engine work from cache reuse without touching Step 4A
    or Step 4B.
    """

    def __init__(self, inner: StockfishEvaluator, *, replay_only: bool = False) -> None:
        self._inner = inner
        self._replay_only = replay_only
        self.analyses = 0

    @property
    def config(self) -> EngineAnalysisConfig:
        return self._inner.config

    @property
    def identity(self):
        return self._inner.identity

    def evaluate(self, position, search_mode, root_move=None):
        if self._replay_only:
            raise ReplayMiss(
                f"replay-only run needs an uncached evaluation of "
                f"{position.epd!r} ({search_mode.value}, "
                f"{root_move.uci if root_move is not None else '-'}) at "
                f"{self.config.nodes} nodes"
            )
        self.analyses += 1
        return self._inner.evaluate(position, search_mode, root_move)


class CountingProvider:
    """A `LeveledEvidenceProvider` that also counts requests per level.

    Pure bookkeeping around an existing provider: it forwards every call
    unchanged, so the evidence a root is measured from is exactly what the
    wrapped provider returned.
    """

    def __init__(self, inner: LeveledEvidenceProvider) -> None:
        self._inner = inner
        self.requests = 0

    def request_for(self, level, position, search_mode, root_move):
        return self._inner.request_for(level, position, search_mode, root_move)

    def evaluate(self, request):
        self.requests += 1
        return self._inner.evaluate(request)


def build_engine_pool(
    executable_path: str,
    levels: Iterable[SearchLevel],
    cache: EvaluationCache,
    *,
    threads: int = 1,
    hash_mb: int = 64,
    replay_only: bool = False,
) -> tuple[CachedEvaluatorPool, dict[SearchLevel, CountingEvaluator], list[StockfishEvaluator]]:
    """Start one Step 4A evaluator per level over one shared Step 4A cache.

    Step 4A binds the node budget per evaluator for its lifetime, so every
    level -- production and reference alike -- needs its own bound evaluator.
    `threads` and `hash_mb` are identical across levels: node budget is the
    only thing that varies, so no level's "independence" is manufactured by
    changing its hash size.

    Cost note: this starts ONE Stockfish process per bound level (B1, B2, any
    B3, every reference level, and the optional discovery audit), and each
    process allocates its own `hash_mb` hash table, so the configured hash is
    paid per level rather than once. For a first real benchmark keep the
    defaults, `threads=1` and `hash_mb=64`, and raise either only after a
    run's actual memory footprint has been measured.

    Returns the pool, the per-level counters, and the started evaluators the
    caller must close.
    """
    started: list[StockfishEvaluator] = []
    counters: dict[SearchLevel, CountingEvaluator] = {}
    try:
        for level in levels:
            evaluator = StockfishEvaluator(
                executable_path,
                EngineAnalysisConfig(nodes=level.nodes, threads=threads, hash_mb=hash_mb),
            )
            evaluator.start()
            started.append(evaluator)
            counters[level] = CountingEvaluator(evaluator, replay_only=replay_only)
        pool = CachedEvaluatorPool(counters, cache)
    except Exception:
        for evaluator in started:
            evaluator.close()
        raise
    return pool, counters, started


@dataclass(frozen=True, slots=True)
class PilotRootRecord:
    """Everything C0 measured for one sampled root."""

    sample: SampledDecision
    assessment: MoveAssessment | None
    assessment_error: str | None
    witness: WitnessResolution
    trajectory: ReferenceTrajectory | None
    discovery_audit: Mapping[str, object] | None
    assessment_seconds: float
    reference_seconds: float
    assessment_requests: int
    reference_requests: int

    def as_dict(self) -> dict:
        return {
            "sample": self.sample.as_dict(),
            "assessment": (
                assessment_dict(self.assessment)
                if self.assessment is not None
                else None
            ),
            "assessment_error": self.assessment_error,
            "witness_resolution": self.witness.as_dict(),
            "reference_trajectory": (
                self.trajectory.as_dict() if self.trajectory is not None else None
            ),
            "experimental_discovery_audit": (
                dict(self.discovery_audit) if self.discovery_audit is not None else None
            ),
            "r_target_available": self.witness.has_target
            and self.trajectory is not None,
            "cost": {
                "assessment_seconds": self.assessment_seconds,
                "reference_seconds": self.reference_seconds,
                "assessment_requests": self.assessment_requests,
                "reference_requests": self.reference_requests,
            },
            "note": (
                "R = S - G_reference is deliberately NOT computed: C0 collects "
                "the stronger-level trajectory and freezes no reference protocol."
            ),
        }


def run_pilot(
    samples: Iterable[SampledDecision],
    provider: LeveledEvidenceProvider,
    policy: ComparisonPolicy,
    ladder: Iterable[SearchLevel],
    *,
    discovery_audit_level: SearchLevel | None = None,
) -> Iterator[PilotRootRecord]:
    """Measure every sampled root and yield its record, one at a time."""
    ladder = tuple(ladder)
    for sample in samples:
        yield measure_root(
            sample,
            provider,
            policy,
            ladder,
            discovery_audit_level=discovery_audit_level,
        )


def measure_root(
    sample: SampledDecision,
    provider: LeveledEvidenceProvider,
    policy: ComparisonPolicy,
    ladder: Iterable[SearchLevel],
    *,
    discovery_audit_level: SearchLevel | None = None,
) -> PilotRootRecord:
    """Run the low-cost Step 4B trajectory and, when a fixed witness exists,
    the stronger same-root same-witness reference trajectory."""
    counted = CountingProvider(provider)
    position = sample.decision.position_before

    started = time.perf_counter()
    try:
        assessment = DecisionAssessor(counted, policy).assess(sample.decision)
        assessment_error = None
    except RequestValidationError as exc:
        assessment = None
        assessment_error = f"{type(exc).__name__}: {exc}"
    assessment_seconds = time.perf_counter() - started
    assessment_requests = counted.requests

    if assessment is None:
        return PilotRootRecord(
            sample=sample,
            assessment=None,
            assessment_error=assessment_error,
            witness=WitnessResolution(
                witness=None,
                s_units=None,
                source="none",
                conservative_margin_units=None,
                qualification_levels=(),
                no_r_target_reason=f"root_rejected:{assessment_error}",
            ),
            trajectory=None,
            discovery_audit=None,
            assessment_seconds=assessment_seconds,
            reference_seconds=0.0,
            assessment_requests=assessment_requests,
            reference_requests=0,
        )

    witness = resolve_fixed_witness(assessment)
    board = reconstruct_board(position)

    reference_started = time.perf_counter()
    trajectory = None
    if witness.has_target:
        trajectory = measure_reference_trajectory(
            counted,
            board,
            position,
            sample.decision.move,
            witness.witness,
            ladder,
        )

    audit = None
    if discovery_audit_level is not None:
        audit = unrestricted_discovery_audit(counted, position, discovery_audit_level)
    reference_seconds = time.perf_counter() - reference_started

    return PilotRootRecord(
        sample=sample,
        assessment=assessment,
        assessment_error=None,
        witness=witness,
        trajectory=trajectory,
        discovery_audit=audit,
        assessment_seconds=assessment_seconds,
        reference_seconds=reference_seconds,
        assessment_requests=assessment_requests,
        reference_requests=counted.requests - assessment_requests,
    )
