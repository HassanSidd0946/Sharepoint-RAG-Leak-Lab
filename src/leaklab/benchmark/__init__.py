"""The benchmark: a fixed matrix of scenario runs, an aggregate over their journals, a report.

  matrix.py     which scenarios, how many replicates, which baseline, packed into runs   (plan)
  execute.py    run the planned runs one after the other, resumably, with a manifest      (run)
  aggregate.py  journals -> per scenario x system statistics + generated caveats          (report)
  render.py     the summary as JSON and as static HTML (Jinja2)                           (report)
  cli.py        `python -m leaklab.benchmark plan | run | report`

Every number in the report is computed from the journals in the benchmark folder. The runner does
the measuring (leaklab.runner); this package only schedules runs and summarizes what they recorded.
"""
