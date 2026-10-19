# Overnight benchmark runs

Extra replicates of the two slow Entra-group scenarios (S1-entra and S2 nested removal), run at night on
different days and appended to the same benchmark set, `runs/benchmark_v2/`.

## What is scheduled

Three Windows scheduled tasks (Task Scheduler, your account, "run only when logged on"):

| Task | When (local) | Runs of the set |
|---|---|---|
| `LeakLab-Benchmark-Night1` | 2026-10-19 22:00 | 2, 3 |
| `LeakLab-Benchmark-Night2` | 2026-10-21 22:00 | 4, 5 |
| `LeakLab-Benchmark-Night3` | 2026-10-23 22:00 | 6, 7 |

Each task runs `scripts/overnight_benchmark.ps1 -Only a,b`: `docker compose up -d`, then
`python -m leaklab.benchmark run --dir runs/benchmark_v2 --only a --only b`, then `... report`. Every run tears
the test tenant down, generates it again (a new generation), cleans both search indexes of the old
generation's documents, and polls S1-entra and S2 on all three systems. About 70-80 minutes per run, so about
2.5-3 hours a night. The log of a night is `runs/benchmark_v2/overnight_<date>_<time>.log`.

## Before the first night

The runs 2-7 must exist in the set. They are added once the verification run (run 1) has finished and was
checked:

    uv run python -m leaklab.benchmark extend --dir runs/benchmark_v2 --runs 6

Until then the tasks do nothing harmful: `run --only 2` refuses a run number that is not in the plan, the
tenant is not touched, and the log says so. Baseline lengths continue to alternate by run number (run 2: 10
minutes, run 3: 1 minute, ...). Journals and the manifest are appended to the same folder; `report` rebuilds
`report.json` and `report.html` over everything.

## What it needs at 22:00

- The computer on and awake (the tasks do not wake it), you logged in (the screen may be locked).
- Docker Desktop running (Setup B's state is in the compose Postgres).
- Nobody else using the test tenant: a run deletes and recreates its users, groups and files.

## Stopping or changing it

    Get-ScheduledTask -TaskName 'LeakLab-Benchmark-*'
    Unregister-ScheduledTask -TaskName 'LeakLab-Benchmark-Night2' -Confirm:$false

An interrupted run is continued from its journal the next time `run` is called for it; if the runner refuses
to resume, `run --only N --redo N` abandons that journal (kept as `*.abandoned`) and starts the run again.

## Schedule as of 2026-10-19 (Prompt 27)

Night 2 now also runs two extra runs of the three fast scenarios, so that S1-direct, S1-spgroup and S4 reach n = 3 replicates
(they have n = 1 from run 1). The tasks:

| Task | When (local) | Script | Runs of the set |
|---|---|---|---|
| `LeakLab-Benchmark-Night1` | 2026-10-19 22:00 | `overnight_benchmark.ps1 -Only 2,3` | 2, 3 (S1-entra, S2) |
| `LeakLab-Benchmark-Night2` | 2026-10-21 22:00 | `overnight_night2.ps1` | 4, 5 (S1-entra, S2), then 8, 9 (S1-direct, S1-spgroup, S4) |
| `LeakLab-Benchmark-Night3` | 2026-10-23 22:00 | `overnight_benchmark.ps1 -Only 6,7` | 6, 7 (S1-entra, S2) |

`overnight_night2.ps1` appends runs 8 and 9 to the plan when it starts (`benchmark extend --runs 2 --scenario S1-direct
--scenario S1-spgroup --scenario S4-move-into-restricted`), so nothing the earlier nights use is touched before they have
finished, then runs 4, 5, 8, 9 in that order. Replicate numbers in the exported bundles follow the plan (S1-direct is
replicate 1, 2, 3 in runs 1, 8, 9), not the run number.

Time: runs 4 and 5 take about 75 minutes each (run 1 took 65 minutes with all five scenarios); a run of only the fast scenarios
took about 21 minutes of polling in run 1, plus generation, cleanup and teardown, so about 30 to 35 minutes each. Expected
total about 3.7 hours; the task limit is 6 hours. The planner's upper bound (every scenario polled to the 60 minute cap) is
5.3 hours of polling plus overhead, which is why the extra runs come last: if the limit ever cut the night short, only they
would be lost, and `benchmark run --only 8 --only 9` runs them later.
