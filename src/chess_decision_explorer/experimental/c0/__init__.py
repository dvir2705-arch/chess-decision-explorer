"""Step 4C.0 -- Calibration Evidence Pilot (EXPERIMENTAL).

C0 is an evidence-generation milestone. Its single research question is:

    How does the low-cost Step 4B measurement `S` differ from much stronger
    same-root, same-witness Stockfish analysis?

For one sampled canonical decision it records:

- `S` -- the qualifying `EngineRegret` produced by the *existing, unmodified*
  Step 4B procedure (`chess_decision_explorer.assessment`);
- `G_ref_j = U_ref_j(witness) - U_ref_j(observed_move)` at each explicitly
  supplied stronger reference node budget `j`, where `U(m) = 2W + D` is the
  Step 4B integer expected-score unit of a `FORCED_MOVE` evaluation of `m`
  from the same canonical root.

`R = S - G_reference` is deliberately NOT computed here: C0 does not freeze a
reference protocol. It collects the stronger-level trajectory needed to study
convergence, and reports diagnostics only.

C0 calibrates nothing. No epsilon, no tau, no drift limit, no B1/B2/B3
budget, no reliability claim, and no ML model comes out of this package.
"""

C0_PHASE_ID = "STEP_4C0_CALIBRATION_EVIDENCE_PILOT"

C0_SCOPE_NOTE = (
    "Step 4C.0 is an EXPERIMENTAL calibration-evidence pilot. NO epsilon was "
    "calibrated. NO tau was calibrated. NO ML model was trained. NO production "
    "reliability claim is made. NO personal games were used for fitting. The "
    "sample is a feasibility sample, not a population-representative "
    "calibration claim."
)

C0_EXPERIMENTAL_CALIBRATION_ID = "C0-EXPERIMENTAL-NOT-A-PRODUCTION-CALIBRATION"
"""TEST-ONLY parity fixture. NOT part of any pilot run.

A pilot run always uses an UNCALIBRATED `ComparisonPolicy`, so C0 never
attaches a `calibration_id` to anything: `cli.build_policy` cannot produce a
CALIBRATED policy and there is no command-line switch that makes it.

This id exists solely so a test can build a CALIBRATED policy directly and
check PARITY -- that the `S` re-derived from an UNCALIBRATED trace equals the
genuine `EngineRegret.units` the same measurement would have produced under a
CALIBRATED scope. Using it anywhere outside that parity check would let
experimental epsilon/tau inputs masquerade as a production conclusion, which
is precisely what C0 must never do.
"""
