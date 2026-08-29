# Taiwan court-corpus scan: post-2015 transplant brokerage

Generated: 2026-08-29

## Scope

The script searched Taiwan's public judgment system with overlapping, narrowly defined terms for cross-border transplant brokerage, payments, travel, anti-rejection medication, named Chinese hospitals, and donor-provenance language.

## Results

- Queries succeeded: 14 / 14
- Unique post-2015 judgments queued: 33
- Full judgment texts retrieved: 33
- Mechanically qualifying case-level leads: 25
- Retrieval or parsing errors: 0

## Evidence rule

A match establishes only that a public judgment contains useful operational anchors. It does not establish the identity or custody status of an organ donor. The candidates file deliberately uses short, role-redacted excerpts and omits party names and medical diagnoses.

## Outputs

- `query_statistics.csv`: query-level result counts
- `detail_index.csv`: metadata and matched-term index for all fetched judgments
- `candidates.csv`: higher-scoring leads for manual authentication
- `errors.csv`: reproducibility log
- `summary.json`: machine-readable summary
