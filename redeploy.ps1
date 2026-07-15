# Lädt den aktuellen app-Code auf die EC2 und startet die Container neu.
# Aufruf (im Projektordner):  .\redeploy.ps1
$ErrorActionPreference = "Stop"
$KEY = ".\pbigen-key.pem"
$EC2 = "ec2-user@63.186.71.33"

Write-Host "==> Code hochladen (app/)" -ForegroundColor Cyan
scp -i $KEY -r app "$($EC2):~/powerbi-generator/"

Write-Host "==> Build-/Infra-Dateien hochladen (requirements.txt etc.)" -ForegroundColor Cyan
# Wichtig: requirements.txt muss mit hoch, sonst cached Docker den pip-Layer
# und neue Abhängigkeiten (z. B. openpyxl, python-multipart) fehlen -> 502.
foreach ($f in @("requirements.txt", "Dockerfile", "docker-compose.yml", "Caddyfile")) {
    if (Test-Path $f) { scp -i $KEY $f "$($EC2):~/powerbi-generator/" }
}

Write-Host "==> Container neu bauen & starten" -ForegroundColor Cyan
ssh -i $KEY $EC2 "cd ~/powerbi-generator && docker compose up -d --build"

Write-Host "`nFertig. Prüfe: https://pbigen.ai-gutachten.com/" -ForegroundColor Green
