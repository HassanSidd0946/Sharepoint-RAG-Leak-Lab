"""Replay bundles: the real benchmark journals, exported for the replay UI and the write-up figures.

  export.py    journal + per-run report -> one JSON bundle per (run, scenario, replicate), checked
               against docs/schemas/replay_bundle.schema.json before it is written
  summary.py   the summary of a bundle: copied from the report, recomputed from the bundle's own
               reads with the oracle's classify(), and asserted equal
  redact.py    the scanner that keeps tenant ids, UPNs, emails and URLs out of every bundle
  labels.py    system labels, document labels and the human text of a mutation
  figures.py   the two write-up figures (SVG + PNG), drawn from the bundles and nothing else
  cli.py       `python -m leaklab.replay export | figures`

Nothing here measures anything: verdicts and `expected` come from the oracle (IntentTimeline over
the run's mutation log), exactly as the scoring uses it, and the UI only renders what a bundle says.
Data the journals do not hold is left out, never made up (see export.py, "What a journal cannot give").
"""
