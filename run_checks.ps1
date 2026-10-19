# Runs every automated check for ScriptDB and returns a non-zero exit code on
# the first failure. This is the single entry point used by CI, so a developer
# and a pipeline verify exactly the same things.
#
#   pwsh -File run_checks.ps1
#
# Layout:
#   1. unit tests (pytest, tests/)
#   2. verification scripts (verifications/) -- headless, offscreen Qt
#   3. syntax check on every module (ast.parse, no interpreter needed)
$ErrorActionPreference = 'Continue'

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

# The verification scripts build real widgets; offscreen keeps them headless.
$env:QT_QPA_PLATFORM = 'offscreen'

$failed = @()

function Step($name, $block) {
    Write-Host ''
    Write-Host "==> $name" -ForegroundColor Cyan
    & $block
    if ($LASTEXITCODE -ne 0) {
        $script:failed += $name
        Write-Host "    FAILED (exit $LASTEXITCODE)" -ForegroundColor Red
    } else {
        Write-Host '    passed' -ForegroundColor Green
    }
}

Step 'unit tests' { python -m pytest tests -q }

foreach ($check in Get-ChildItem (Join-Path $repoRoot 'verifications') -Filter 'verify_*.py' |
        Sort-Object Name) {
    Step "verification: $($check.Name)" { python $check.FullName }
}

Step 'syntax check' {
    python -c @"
import ast, pathlib, sys
bad = 0
for path in sorted(pathlib.Path('.').rglob('*.py')):
    parts = set(path.parts)
    if '__pycache__' in parts or '.aider' in parts:
        continue
    try:
        ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    except SyntaxError as exc:
        bad += 1
        print('SYNTAX ERROR %s: %s' % (path, exc))
sys.exit(1 if bad else 0)
"@
}

Write-Host ''
if ($failed.Count -gt 0) {
    Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host 'All checks passed.' -ForegroundColor Green
exit 0