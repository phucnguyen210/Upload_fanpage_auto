$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

python -m PyInstaller `
  --noconfirm `
  --onefile `
  --name VideoPipeline `
  --add-data "pipeline_web/templates;pipeline_web/templates" `
  --add-data "pipeline_web/static;pipeline_web/static" `
  desktop_app.py

Write-Host "Built dist\VideoPipeline.exe"
