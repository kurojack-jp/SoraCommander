# Compatibility entry point for Windows PowerShell 5.1 and PowerShell 7.
$flags = @()
foreach ($flag in $args) {
    switch -Exact ($flag) {
        '-Check'     { $flags += '--check' }
        '-FromStdin' { $flags += '--from-stdin' }
        default {
            Write-Output '[HOLD] Use only -Check or -FromStdin.'
            exit 2
        }
    }
}
$python = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Output '[HOLD] Run tools\setup.cmd to prepare the virtual environment.'
    exit 2
}
& $python -I -B (Join-Path $PSScriptRoot 'scn_tunnel_key.py') @flags
exit $LASTEXITCODE
