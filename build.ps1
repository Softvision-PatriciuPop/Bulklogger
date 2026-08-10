<#
    Builds dist\Bulklogger.exe - a single windowed executable with no console.

        pip install -r requirements-dev.txt
        .\build.ps1

    tickets.toml and credentials.toml are deliberately NOT bundled. The exe
    reads them from its own directory at runtime, so each person keeps their
    own token beside their own copy and a rebuild never overwrites either.
#>

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    throw "pyinstaller not found. Run: pip install -r requirements-dev.txt"
}

Write-Host "Building Bulklogger.exe..." -ForegroundColor Cyan

pyinstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name Bulklogger `
    --icon bulklogger.ico `
    --add-data "bulklogger.ico;." `
    --add-data "bulklogger.png;." `
    --collect-all tzdata `
    --hidden-import theme `
    bulklogger.py

if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed with exit code $LASTEXITCODE" }

$exe = Join-Path $PSScriptRoot "dist\Bulklogger.exe"
$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "Built $exe ($size MB)" -ForegroundColor Green

# Running the exe from dist\ writes personal state right next to it, and dist\
# is the folder people zip up. Strip it, loudly, so a token cannot ride along.
$personal = @("credentials.toml", "draft.json", "usage.json")
$found = $personal | ForEach-Object { Join-Path $PSScriptRoot "dist\$_" } |
         Where-Object { Test-Path $_ }
if ($found) {
    Write-Host ""
    Write-Host "REMOVED personal files from dist\ before packaging:" -ForegroundColor Red
    $found | ForEach-Object {
        Write-Host "    $(Split-Path $_ -Leaf)" -ForegroundColor Red
        Remove-Item $_ -Force
    }
    Write-Host "  credentials.toml holds YOUR API token - it must never be shared." -ForegroundColor Red
    Write-Host "  If you have already sent out a build, revoke that token at" -ForegroundColor Red
    Write-Host "  id.atlassian.com -> Security -> API tokens." -ForegroundColor Red
}

Write-Host ""
Write-Host "To distribute, ship exactly these two files:" -ForegroundColor Yellow
Write-Host "    dist\Bulklogger.exe" -ForegroundColor Yellow
Write-Host "    dist\tickets.toml" -ForegroundColor Yellow
Write-Host "Each person runs it once and enters their own API token." -ForegroundColor Yellow
Write-Host ""
Write-Host "Do NOT ship credentials.toml, draft.json or usage.json." -ForegroundColor Yellow
