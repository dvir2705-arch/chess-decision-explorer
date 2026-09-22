"""Step 5C-lite -- External Human Reference Comparison.

This package compares the player's recurring decisions against **human**
evidence: what a population of other human players actually played in the
same position, and how those games actually ended.

It is NOT the C0/C1 strong-engine reference. Nothing here produces or
consumes `G_ref`, `R`, `epsilon`, a search budget, or any search-noise
calibration quantity, and nothing here imports
`chess_decision_explorer.engine`, `...assessment`, or
`...experimental.c0`. Human reference evidence and strong-engine reference
evidence are two separate architectures that happen to share the same
`PositionKey`; this milestone keeps them separate.

What it does:

1. build (or load) a small set of the user's recurring `PositionKey`s;
2. stream an external PGN corpus, accepting exactly a configured number of
   ELIGIBLE games;
3. for every position before a move in an accepted game, keep statistics
   **only** when that `PositionKey` is in the target set, and discard the
   position otherwise;
4. aggregate human move frequencies and actor-relative outcomes into a
   second, separate `PositionIndex`;
5. derive a Personal-vs-Reference comparison without mutating either
   aggregate.

The architectural consequence of step 3 is the point of the milestone: the
external corpus may hold tens of millions of decision positions, and none of
them is stored. Memory scales with the personal target set and the moves
actually matched inside it, not with corpus size.

This milestone makes **no** statistical-significance claim. It reports
counts, rates, and ranks. A difference between a personal rate and a
reference rate is a description of two populations, not evidence that a move
is good or bad.
"""

PHASE_ID = "STEP_5C_LITE_EXTERNAL_HUMAN_REFERENCE"

SCOPE_NOTE = (
    "Step 5C-lite is HUMAN REFERENCE evidence: observed human move "
    "frequencies and actor-relative human outcomes in the player's own "
    "recurring positions. It is NOT engine evaluation, NOT the C0/C1 "
    "strong-engine reference, NOT a calibration, and NOT a move-quality "
    "judgement. No statistical-significance claim is made. The reference "
    "population is whatever corpus was streamed in, filtered by the recorded "
    "eligibility filters -- it is not a rating-matched cohort and must never "
    "be described as one."
)

POSITION_KEY_SEMANTICS_VERSION = "POSITION_KEY_V1"
"""The `domain.PositionKey` identity contract this run matched on:
``board.epd(en_passant="legal")`` -- piece placement, side to move, castling
rights, and only legally relevant en-passant state; no halfmove clock, no
fullmove number, no repetition history.

Deliberately a separate name from `engine.POSITION_SEMANTICS_VERSION`
(``CANONICAL_POSITION_V1``), which is the *engine reconstruction* contract.
Matching a personal position to a reference position needs only the identity
contract, so this package never imports the engine module. If `PositionKey`
identity ever changes, this version changes and previously written reference
artefacts stop being comparable.
"""

TARGET_SET_FORMAT_VERSION = "HUMAN_REFERENCE_TARGET_SET_V1"
MANIFEST_VERSION = "HUMAN_REFERENCE_RUN_MANIFEST_V1"
