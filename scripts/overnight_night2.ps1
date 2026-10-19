# Night 2 of the overnight benchmark (see docs/OVERNIGHT.md): runs 4 and 5 of the set (S1-entra and S2), then
# two extra runs (8 and 9) of the three fast scenarios (S1-direct, S1-spgroup, S4) so that those cells reach
# n = 3 replicates.
#
#   powershell -File scripts\overnight_night2.ps1
#
# The extra runs are appended to the plan HERE, when this script starts (not earlier), so that nothing the
# earlier nights use is touched before they have finished. The append is skipped if the plan already has 9
# runs. The runs are executed in this order: 4, 5, 8, 9. If the task's time limit ever cuts the night short,
# only the extra runs are lost, and they can be run later with `benchmark run --only 8 --only 9`.
# It needs Docker Desktop running and the usual .env.
param(
    [string]$Dir = "runs/benchmark_v2",
    [int]$ExtraRuns = 2
)

$ErrorActionPreference = "Continue"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$log = Join-Path $Dir "overnight_$stamp.log"

function Log($text) {
    "$(Get-Date -Format s) $text" | Out-File -FilePath $log -Append -Encoding utf8
}

Log "night 2: runs 4, 5 and $ExtraRuns extra runs of the fast scenarios, in $Dir"
docker compose up -d *>> $log

$plan = Get-Content (Join-Path $Dir "plan.json") -Raw | ConvertFrom-Json
$planned = @($plan.runs).Count
$wanted = 7 + $ExtraRuns
if ($planned -lt $wanted) {
    Log "plan has $planned runs; appending $($wanted - $planned) run(s) of S1-direct, S1-spgroup, S4"
    uv run python -m leaklab.benchmark extend --dir $Dir --runs ($wanted - $planned) `
        --scenario S1-direct --scenario S1-spgroup --scenario S4-move-into-restricted *>> $log
    Log "extend finished with exit code $LASTEXITCODE"
} else {
    Log "plan already has $planned runs; nothing to append"
}

$onlyArgs = @("--only", "4", "--only", "5")
for ($n = 8; $n -lt 8 + $ExtraRuns; $n++) { $onlyArgs += @("--only", "$n") }
uv run python -m leaklab.benchmark run --dir $Dir @onlyArgs *>> $log
Log "run command finished with exit code $LASTEXITCODE"
uv run python -m leaklab.benchmark report --dir $Dir *>> $log
Log "report command finished with exit code $LASTEXITCODE"
