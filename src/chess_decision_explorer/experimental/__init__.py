"""EXPERIMENTAL code. NOT production, NOT approved, NOT calibrated.

Everything under this package exists to *generate evidence* for decisions
that have not been made yet. It is deliberately kept out of the production
modules (`domain`, `aggregation`, `ingestion`, `personal`, `engine`,
`engine_cache`, `assessment`):

- no production module may import from `chess_decision_explorer.experimental`
  (a test enforces this);
- nothing here defines, changes, or reinterprets production semantics;
- nothing here may be quoted as a calibrated, reliable, or production result.

Experimental code *consumes* the production interfaces exactly as they are.
"""
