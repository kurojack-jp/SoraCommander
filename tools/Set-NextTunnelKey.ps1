# DEV_ONLY: store or check this Windows user's own tunnel API key file.
# The key is never printed, logged, or accepted as a command-line argument.
# Exit codes: 0 OK, 2 cancelled or refused, 4 missing, 5 invalid, 6 unsafe path.
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$FromStdin
)
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    Write-Output '[HOLD] USERPROFILE is not set.'
    exit 6
}
$directory = Join-Path $env:USERPROFILE '.config\tunnel-client'
$keyFile = Join-Path $directory 'sora-next.key'

function Test-IsLink([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $item = Get-Item -LiteralPath $Path -Force
    return [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Test-KeyText([string]$Text) {
    # One line of printable ASCII, so a stray BOM, space or newline is caught.
    return ($null -ne $Text) -and ($Text -cmatch '\A[\x21-\x7E]{8,4096}\z')
}

if ((Test-IsLink $directory) -or (Test-IsLink $keyFile)) {
    Write-Output '[HOLD] The key file or its folder is a link. Nothing was read or written.'
    exit 6
}

if ($Check) {
    if (-not (Test-Path -LiteralPath $keyFile -PathType Leaf)) {
        Write-Output ('[HOLD] No key file at {0}. Run tools\Set-NextTunnelKey.ps1 or create it by hand.' -f $keyFile)
        exit 4
    }
    if ((Get-Item -LiteralPath $keyFile -Force).Length -gt 8192) {
        Write-Output '[HOLD] The key file is too large to be a key.'
        exit 5
    }
    $bytes = [IO.File]::ReadAllBytes($keyFile)
    $text = [Text.Encoding]::GetEncoding('iso-8859-1').GetString($bytes)
    if (-not (Test-KeyText $text)) {
        Write-Output '[HOLD] The key file must hold the key only: one line, no spaces, no line break, no BOM.'
        exit 5
    }
    Write-Output '[OK] The key file has a valid form. Its value was not shown or checked against the service.'
    exit 0
}

if ($FromStdin) {
    $key = [Console]::In.ReadToEnd()
} else {
    if (Test-Path -LiteralPath $keyFile) {
        $answer = Read-Host 'A key file already exists. Replace it? (y/N)'
        if ($answer -cne 'y') {
            Write-Output '[INFO] Cancelled. The key file was not changed.'
            exit 2
        }
    }
    $secure = Read-Host 'Paste your API key (input is hidden)' -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

if (-not (Test-KeyText $key)) {
    Write-Output '[HOLD] That does not look like one key. The key file was not changed.'
    exit 5
}

if (-not (Test-Path -LiteralPath $directory)) {
    New-Item -ItemType Directory -Path $directory | Out-Null
}
$temporary = Join-Path $directory ('sora-next.key.' + [Guid]::NewGuid().ToString('N') + '.tmp')
try {
    # Create empty, restrict to this user, and only then write the key.
    [IO.File]::WriteAllBytes($temporary, [byte[]]@())
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
        $identity, [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow)))
    [IO.File]::SetAccessControl($temporary, $acl)
    [IO.File]::WriteAllBytes($temporary, [Text.Encoding]::ASCII.GetBytes($key))
    Move-Item -LiteralPath $temporary -Destination $keyFile -Force
} finally {
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Force
    }
}
Write-Output ('[OK] Saved to {0}. Only this Windows user can open it.' -f $keyFile)
exit 0
