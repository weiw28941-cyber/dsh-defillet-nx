$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path $PSScriptRoot -Parent
$Python = if ($env:DEFEATURE_PYTHON) { $env:DEFEATURE_PYTHON } else { "python" }
Set-Location $RepoRoot
& $Python -m uvicorn webapp.backend.app.main:app --host 127.0.0.1 --port 8000 --reload
