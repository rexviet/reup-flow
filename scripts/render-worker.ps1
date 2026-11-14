# The review worker on a Windows PC that only renders approved revisions
# (docs/windows-render-worker.md). Native Windows: no WSL, no Docker.
#
#   powershell -ExecutionPolicy Bypass -File scripts\render-worker.ps1 [-EnvFile <path>]
#
# Reads KEY=VALUE lines from .render-worker.env at the repository root, refuses keys this
# machine must never hold, and runs `python -m localization.review_worker.main` from the
# project's virtual environment. Ctrl+C reaches the worker, which waits up to 60 s for a
# running render and gives the row back to the queue before it exits.
param(
  [string]$EnvFile
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) { $EnvFile = Join-Path $Root '.render-worker.env' }
if (-not (Test-Path -LiteralPath $EnvFile)) {
  throw "render worker: $EnvFile is missing (see docs/windows-render-worker.md, section 5.3)"
}

$Required = @(
  'LOC_DATABASE_URL', 'S3_ENDPOINT', 'LOC_S3_ACCESS_KEY_ID', 'LOC_S3_SECRET_ACCESS_KEY',
  'REVIEW_WORKER_DATA_ROOT', 'HYPERFRAMES_BIN'
)
# Secrets of the Mac stack that a render machine has no use for.
$Forbidden = '^(POSTGRES_PASSWORD|MINIO_ROOT_.*|OPENROUTER_API_KEY|API_KEY|LANGFUSE_.*|REVIEW_S3_.*)$'

$Values = @{}
foreach ($Line in Get-Content -LiteralPath $EnvFile) {
  if ($Line -match '^\s*(#|$)') { continue }
  if ($Line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
    throw "render worker: cannot read line in ${EnvFile}: $Line"
  }
  $Key = $Matches[1]
  $Value = $Matches[2].Trim()
  if ($Value.Length -ge 2 -and (
      ($Value.StartsWith('"') -and $Value.EndsWith('"')) -or
      ($Value.StartsWith("'") -and $Value.EndsWith("'")))) {
    $Value = $Value.Substring(1, $Value.Length - 2)
  }
  if ($Key -match $Forbidden) {
    throw "render worker: $Key must not be on the render machine; remove it from $EnvFile"
  }
  $Values[$Key] = $Value
}

$Missing = @($Required | Where-Object { -not $Values[$_] })
if ($Missing.Count -gt 0) {
  throw "render worker: $EnvFile lacks $($Missing -join ', ')"
}
# A missing line must not turn this PC into a second Studio worker under the Mac's name.
if (-not $Values['REVIEW_WORKER_ROLES']) { $Values['REVIEW_WORKER_ROLES'] = 'render' }
if (-not $Values['REVIEW_WORKER_NAME']) { $Values['REVIEW_WORKER_NAME'] = 'render-worker-win' }
$Values['PYTHONUNBUFFERED'] = '1'

foreach ($Key in $Values.Keys) {
  [Environment]::SetEnvironmentVariable($Key, $Values[$Key], 'Process')
}

$Project = Join-Path $Root 'services\localization'
$Python = Join-Path $Project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
  throw "render worker: $Python is missing; run 'uv sync --frozen --no-dev' in services\localization"
}

# Same console, so Ctrl+C (and Ctrl+Break) reach the worker; WaitForExit keeps this script
# from returning while the worker is still giving its render back.
$Worker = Start-Process -FilePath $Python -ArgumentList '-m', 'localization.review_worker.main' `
  -WorkingDirectory $Project -NoNewWindow -PassThru
$null = $Worker.Handle  # without it ExitCode can come back empty
$Worker.WaitForExit()
exit $Worker.ExitCode
