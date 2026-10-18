# Overnight benchmark runs, started by Windows Task Scheduler (see docs/OVERNIGHT.md).
#
#   powershell -File scripts\overnight_benchmark.ps1 -Only 2,3
#
# Runs the given run numbers of the benchmark set in $Dir (they must exist in its plan: add them
# with `python -m leaklab.benchmark extend` first; if they do not, the run command refuses and
# nothing touches the tenant), then rebuilds report.json / report.html. Everything is appended to a
# log next to the journals. It needs Docker Desktop running (Postgres for Setup B's state) and the
# usual .env. Each run tears the test tenant down and generates it again.
param(
    [Parameter(Mandatory = $true)][int[]]$Only,
    [string]$Dir = "runs/benchmark_v2"
)

$ErrorActionPreference = "Continue"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$log = Join-Path $Dir "overnight_$stamp.log"

function Log($text) {
    "$(Get-Date -Format s) $text" | Out-File -FilePath $log -Append -Encoding utf8
}

Log "overnight benchmark: runs $($Only -join ', ') in $Dir"
docker compose up -d *>> $log
$onlyArgs = @()
foreach ($n in $Only) { $onlyArgs += @("--only", "$n") }
uv run python -m leaklab.benchmark run --dir $Dir @onlyArgs *>> $log
Log "run command finished with exit code $LASTEXITCODE"
uv run python -m leaklab.benchmark report --dir $Dir *>> $log
Log "report command finished with exit code $LASTEXITCODE"
