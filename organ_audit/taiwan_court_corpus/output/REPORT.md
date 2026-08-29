# Taiwan court-corpus scan: post-2015 transplant brokerage

Generated: 2026-08-29

## Version 2 correction

The first scan scored terms anywhere in a full judgment and therefore overmatched unrelated cases. Version 2 requires the transplant, cross-border/hospital, and operational evidence to occur in the same local narrative window. It also excludes non-geographic uses such as 中國信託 from the China anchor.

## Results

- Queries succeeded: 19 / 19
- Unique post-2015 judgments queued: 17
- Full judgment texts retrieved: 17
- Strict local-co-occurrence leads: 4
- Excluded after local review: 13
- Retrieval or parsing errors: 0
- Known brokerage control qualified: True

## Evidence rule

A match establishes only that a public judgment contains a locally coherent transplant-brokerage or transplant-payment narrative. It does not establish donor identity or custody status. Short excerpts are role-redacted and party names or medical diagnoses are not intentionally exported.

## Outputs

- `candidates.csv`: strict local-co-occurrence leads
- `excluded_false_positives.csv`: transparent rejection log
- `detail_index.csv`: all fetched judgments and qualification reasons
- `query_statistics.csv`: query-level retrieval counts
- `errors.csv`: reproducibility log
- `summary.json`: machine-readable summary
