$ErrorActionPreference = "Stop"
$Python = if ($env:DEFEATURE_PYTHON) { $env:DEFEATURE_PYTHON } else { "python" }
& $Python (Join-Path $PSScriptRoot "start.py") @args
exit $LASTEXITCODE
