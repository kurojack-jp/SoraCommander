# DEV_ONLY. Owner runs this once. It copies Send-ToVm.ps1 to C:\Program Files\SoraVmTools
# (so that ordinary programs cannot change a script that runs as administrator) and makes two shortcuts:
# the desktop icon and the "Send to" menu. Nothing else is changed. Remove: delete the folder and the shortcuts.
param(
    [Parameter(Mandatory = $true)][string]$VmName,
    [switch]$Elevated
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Folder = Join-Path $env:ProgramFiles 'SoraVmTools'
$Target = Join-Path $Folder 'Send-ToVm.ps1'
$ConfigPath = Join-Path $Folder 'send-to-vm.json'
$Source = Join-Path $PSScriptRoot 'Send-ToVm.ps1'
$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$JapaneseLanguage = [Globalization.CultureInfo]::CurrentUICulture.Name -like 'ja*'

function LocalText([string]$Japanese, [string]$English) {
    if ($JapaneseLanguage) { return $Japanese }
    return $English
}

function Test-VmName([string]$Name) {
    $count = 0
    for ($i = 0; $i -lt $Name.Length; $i++) {
        $category = [Globalization.CharUnicodeInfo]::GetUnicodeCategory($Name, $i)
        if ($Name[$i] -ne ' ' -and $category -match '^(Control|Format|Surrogate|PrivateUse|OtherNotAssigned|.*Separator)$') { return $false }
        if ([char]::IsHighSurrogate($Name[$i])) { $i++ }
        $count++
    }
    return ($count -ge 1 -and $count -le 100)
}

function Get-Digest([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream)) -replace '-', '') }
    finally { $sha.Dispose(); $stream.Dispose() }
}

function Set-InstalledFile([string]$Path, [byte[]]$Bytes) {
    $temp = Join-Path $Folder ('.install-' + [Guid]::NewGuid().ToString('N'))
    $stream = [IO.File]::Open($temp, 'CreateNew', 'Write', 'None')
    try {
        try { $stream.Write($Bytes, 0, $Bytes.Length) }
        finally { $stream.Dispose() }
        if (Test-Path -LiteralPath $Path) {
            $item = Get-Item -LiteralPath $Path -Force
            if ($item -isnot [IO.FileInfo] -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw (LocalText "普通のファイルではありません: $Path" "Not an ordinary file: $Path")
            }
            # Windows PowerShell turns $null into "" for a string argument; NullString means no backup file.
            [IO.File]::Replace($temp, $Path, [NullString]::Value)
        }
        else { [IO.File]::Move($temp, $Path) }
    }
    finally { if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) } }
}

$code = 0
try {
    if (-not (Test-VmName $VmName)) { throw (LocalText 'VM 名が無効です。' 'Invalid VM name.') }
    if ($Elevated) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        if (-not (New-Object Security.Principal.WindowsPrincipal $identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw (LocalText '管理者として動いていません。' 'This process is not running as administrator.')
        }
        if (Test-Path -LiteralPath $Folder) {
            $item = Get-Item -LiteralPath $Folder -Force
            if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw (LocalText 'SoraVmTools が普通のフォルダではありません。' 'SoraVmTools is not an ordinary directory.')
            }
        }
        else { [void](New-Item -ItemType Directory -Path $Folder) }
        $json = '{"vm_name":"' + $VmName.Replace('\', '\\').Replace('"', '\"') + '"}'
        Set-InstalledFile $Target ([IO.File]::ReadAllBytes($Source))
        Set-InstalledFile $ConfigPath ([Text.UTF8Encoding]::new($false, $true).GetBytes($json))
        exit 0
    }
    # Carry the exact VM name through the UAC launch without command-line metacharacters.
    $scriptPath = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($PSCommandPath))
    $argument = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($VmName))
    $command = "& ([Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('$scriptPath'))) -Elevated -VmName ([Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('$argument'))); exit `$LASTEXITCODE"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $process = Start-Process -FilePath $PowerShell -Verb RunAs -Wait -PassThru -ArgumentList "-NoProfile -ExecutionPolicy RemoteSigned -EncodedCommand $encoded"
    if ($process.ExitCode -ne 0) { throw (LocalText 'コピーできませんでした。' 'The files could not be copied.') }
    $digest = Get-Digest $Target
    if ((Get-Digest $Source) -ne $digest) { throw (LocalText 'コピーした中身が違います。' 'The copied contents differ.') }
    $safeName = ($VmName -replace '[\\/:*?"<>|]', '_').TrimEnd(' ', '.')
    if (-not $safeName) { $safeName = '_' }
    $LinkName = LocalText "$safeName へ送る.lnk" "Send to $safeName.lnk"
    $shell = New-Object -ComObject WScript.Shell
    foreach ($place in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('SendTo'))) {
        $link = $shell.CreateShortcut((Join-Path $place $LinkName))
        $link.TargetPath = $PowerShell
        $link.Arguments = "-NoProfile -ExecutionPolicy RemoteSigned -File `"$Target`""
        $link.IconLocation = "$env:SystemRoot\System32\vmconnect.exe,0"
        $link.Description = LocalText "ファイルを $VmName へ送る（ホストから VM だけ）" "Send files to $VmName (host to VM only)"
        $link.Save()
        Write-Host (LocalText "作りました: $(Join-Path $place $LinkName)" "Created: $(Join-Path $place $LinkName)")
    }
    Write-Host "SHA-256 $digest"
}
catch { Write-Host "[HOLD] $($_.Exception.Message)"; $code = 2 }
if (-not $Elevated) { [void](Read-Host (LocalText 'Enter で閉じます' 'Press Enter to close')) }
exit $code
