# DEV_ONLY. Owner tool: copies dropped files from this PC into the test VM with Copy-VMFile.
# Host to VM only, over the Hyper-V bus: no network, no password, no shared folder.
# Run it only from C:\Program Files\SoraVmTools (see Install-SendToVm.ps1). AI tools must not run it.
# Dropped paths arrive without names; only -PlanPath and -PlanHash by name may fill those two.
[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$Elevated,
    [string]$PlanPath,
    [string]$PlanHash,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Paths
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$GuestRoot = 'C:\SoraFromHost'
$GuestServiceId = '6C09BB55-D683-4DA0-8931-C9BF705F6480'
$MaxDropped = 50
$MaxFiles = 500
$MaxBytes = 20GB
$MaxPlanBytes = 4MB
$JapaneseLanguage = [Globalization.CultureInfo]::CurrentUICulture.Name -like 'ja*'
$BundleFiles = @(
    'vm_agent.py', 'vm_protocol.py', 'vm_win.py', 'vm_files.py', 'vm_uia.py',
    'vm_guest.py', 'check.cmd', 'firewall-admin.cmd', 'firewall-remove-admin.cmd', 'install.cmd',
    'start-view.cmd', 'start-full.cmd', 'update.cmd', 'netcheck.cmd', 'remove.cmd', 'vm-guest.json',
    'vm-guest.manifest.json'
)

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

function Get-ConfiguredVmName {
    $folder = Get-Item -LiteralPath $PSScriptRoot -Force
    if (-not $folder.PSIsContainer -or ($folder.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw (LocalText 'SoraVmTools が普通のフォルダではありません。' 'SoraVmTools is not an ordinary directory.')
    }
    $path = Join-Path $folder.FullName 'send-to-vm.json'
    $bad = LocalText 'send-to-vm.json が無効です。' 'send-to-vm.json is invalid.'
    $item = Get-Item -LiteralPath $path -Force
    if ($item -isnot [IO.FileInfo] -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -gt 4096) {
        throw $bad
    }
    $stream = [IO.File]::Open($path, 'Open', 'Read', 'Read')
    try {
        if ($stream.Length -gt 4096) { throw $bad }
        $reader = [IO.BinaryReader]::new($stream)
        $json = [Text.UTF8Encoding]::new($false, $true).GetString($reader.ReadBytes([int]$stream.Length))
    }
    finally { $stream.Dispose() }
    # The installer writes one member, with printable text and escaped quotes or backslashes.
    if ($json -cnotmatch '\A[ \r\n\t]*\{[ \r\n\t]*"vm_name"[ \r\n\t]*:[ \r\n\t]*"((?:[^"\\\x00-\x1f]|\\["\\])*)"[ \r\n\t]*\}[ \r\n\t]*\z') {
        throw $bad
    }
    $name = [regex]::Replace($Matches[1], '\\(["\\])', '$1')
    if (-not (Test-VmName $name)) { throw $bad }
    return $name
}

function Get-BundleFiles($Top) {
    for ($folder = $Top; $null -ne $folder; $folder = $folder.Parent) {
        if ($folder.Attributes -band [IO.FileAttributes]::ReparsePoint) { return }
    }
    $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $children = @(Get-ChildItem -LiteralPath $Top.FullName -Force)
    foreach ($child in $children) {
        if ($child -isnot [IO.FileInfo] -or ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $child.LinkType) { return }
        [void]$names.Add($child.Name)
    }
    if ($names.SetEquals([string[]]$BundleFiles) -or $names.SetEquals([string[]]($BundleFiles + 'vm-guest.key'))) {
        return ,$children
    }
}

function Test-Private([string]$Name) {
    # A safety net against a wrong drop, not a secret scanner.
    return ($Name -match '(?i)\.(key|pem|pfx|p12|kdbx|ppk)$' -or $Name -match '(?i)^\.env(\..*)?$' -or
            $Name -match '(?i)^id_(rsa|dsa|ecdsa|ed25519)' -or $Name -match '(?i)^\.(ssh|gnupg|git|config|codex|claude)$')
}

function Initialize-FileInspection {
    if ('SoraVmTools.FileInspection' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace SoraVmTools {
    public static class FileInspection {
        [StructLayout(LayoutKind.Sequential)]
        public struct FileInformation {
            public uint Attributes, CreationLow, CreationHigh, AccessLow, AccessHigh,
                WriteLow, WriteHigh, Volume, SizeHigh, SizeLow, Links, IdHigh, IdLow;
            public long ChangeTime;
            public ulong FileVolume, FileIdLow, FileIdHigh;
            public long Length { get { return ((long)SizeHigh << 32) | SizeLow; } }
            public string Identity {
                get { return String.Format("{0:X16}:{1:X16}{2:X16}:{3:X8}{4:X8}:{5:X8}{6:X8}:{7:X16}",
                    FileVolume, FileIdHigh, FileIdLow, CreationHigh, CreationLow, WriteHigh, WriteLow, ChangeTime); }
            }
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct FileBasicInformation {
            public long CreationTime, AccessTime, WriteTime, ChangeTime;
            public uint Attributes;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct FileIdInformation {
            public ulong Volume, IdLow, IdHigh;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        public static extern uint GetDriveType(string root);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFile(string path, uint access, uint share,
            IntPtr security, uint disposition, uint flags, IntPtr template);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(SafeFileHandle file, out FileInformation information);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandleEx(SafeFileHandle file, int infoClass,
            out FileBasicInformation information, uint size);

        [DllImport("kernel32.dll", EntryPoint = "GetFileInformationByHandleEx", SetLastError = true)]
        private static extern bool GetFileId(SafeFileHandle file, int infoClass,
            out FileIdInformation information, uint size);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandle(SafeFileHandle file, StringBuilder path,
            uint capacity, uint flags);

        public static SafeFileHandle Open(string path, bool ancestor, uint share) {
            // Open the directory entry itself and permit directory handles.
            // FILE_LIST_DIRECTORY on ancestors makes their share restrictions effective.
            var file = CreateFile(path, ancestor ? 0x81u : 0x80000000u, share,
                IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
            if (file.IsInvalid) {
                int error = Marshal.GetLastWin32Error();
                file.Dispose();
                throw new Win32Exception(error);
            }
            return file;
        }

        public static FileInformation Inspect(SafeFileHandle file) {
            FileInformation information;
            if (!GetFileInformationByHandle(file, out information))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            if ((information.Attributes & 0x10u) == 0) {
                FileBasicInformation basic;
                if (!GetFileInformationByHandleEx(file, 0, out basic,
                    (uint)Marshal.SizeOf(typeof(FileBasicInformation))))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                FileIdInformation id;
                if (!GetFileId(file, 18, out id, (uint)Marshal.SizeOf(typeof(FileIdInformation))))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                information.ChangeTime = basic.ChangeTime;
                information.FileVolume = id.Volume;
                information.FileIdLow = id.IdLow;
                information.FileIdHigh = id.IdHigh;
            }
            return information;
        }

        public static string FinalPath(SafeFileHandle file) {
            var path = new StringBuilder(32768);
            uint length = GetFinalPathNameByHandle(file, path, (uint)path.Capacity, 0);
            if (length == 0) throw new Win32Exception(Marshal.GetLastWin32Error());
            if (length >= path.Capacity) throw new InvalidOperationException("Source path too long.");
            return path.ToString();
        }
    }
}
'@
}

function Test-LocalDrive([string]$Root) {
    return [SoraVmTools.FileInspection]::GetDriveType($Root) -in 2, 3, 6
}

function Close-SendPath($Handles) {
    for ($i = $Handles.Count - 1; $i -ge 0; $i--) { $Handles[$i].Dispose() }
}

function Open-SendPath([string]$Path, [uint32]$LeafShare = 1) {
    Initialize-FileInspection
    $Path = $Path.TrimEnd('\')
    if ($Path -notmatch '^[A-Za-z]:\\[^\\]' -or $Path -match '[\x00-\x1f"<>|?*/]') {
        throw "Only absolute local paths can be sent: $Path"
    }
    $root = $Path.Substring(0, 3)
    if (-not (Test-LocalDrive $root)) { throw "Only local drives can be used: $root" }
    $parts = $Path.Substring(3).Split('\')
    $handles = [Collections.Generic.List[Microsoft.Win32.SafeHandles.SafeFileHandle]]::new()
    try {
        $current = $root
        for ($i = 0; $i -le $parts.Count; $i++) {
            if ($i -gt 0) {
                $part = $parts[$i - 1]
                if (-not $part -or $part -match '[. ]$|:') { throw "Invalid path component: $Path" }
                $current = [IO.Path]::Combine($current, $part)
            }
            $ancestor = $i -lt $parts.Count
            $share = if ($ancestor) { 1 } else { $LeafShare }
            $handle = [SoraVmTools.FileInspection]::Open($current, $ancestor, $share)
            $handles.Add($handle)
            $info = [SoraVmTools.FileInspection]::Inspect($handle)
            $directory = ($info.Attributes -band [IO.FileAttributes]::Directory) -ne 0
            if (($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
                ($ancestor -and -not $directory) -or (-not $directory -and $info.Links -ne 1) -or
                -not [string]::Equals([SoraVmTools.FileInspection]::FinalPath($handle),
                    ('\\?\' + $current), [StringComparison]::OrdinalIgnoreCase)) {
                throw "Links and non-canonical paths cannot be used: $current"
            }
        }
        return [pscustomobject]@{ Handles = $handles; Handle = $handle; Directory = $directory
            Identity = $info.Identity; Length = $info.Length }
    }
    catch {
        Close-SendPath $handles
        throw
    }
}

function Get-SendList([string[]]$Dropped) {
    # Snapshot each source by handle, including its volume, file ID, timestamps and byte count.
    if ($null -eq $Dropped -or $Dropped.Count -lt 1) { throw (LocalText 'ファイルをアイコンに重ねてください。' 'Drop files onto the icon.') }
    if ($Dropped.Count -gt $MaxDropped) { throw (LocalText "一度に重ねられるのは $MaxDropped 個までです。" "Drop at most $MaxDropped items at a time.") }
    $items = New-Object System.Collections.ArrayList
    foreach ($path in $Dropped) {
        if ($path -notmatch '^[A-Za-z]:\\[^\\]' -or $path -match '[\x00-\x1f"<>|?*]') { throw (LocalText 'このパソコンのドライブにあるファイルだけ送れます。' 'Only files on this PC can be sent.') }
        foreach ($part in $path.Split('\')) { if (Test-Private $part) { throw (LocalText "秘密のファイルに見えるので止めました: $part" "Stopped because this looks like a private file: $part") } }
        $bundleKey = $null
        # Windows PowerShell follows directory links in -Recurse, so walk by hand.
        $pending = New-Object System.Collections.Stack
        $checked = Open-SendPath $path
        try {
            $top = Get-Item -LiteralPath $path -Force
            $base = Split-Path -Parent $top.FullName
            if ($checked.Directory -and $top.Name -ceq 'vm-guest') {
                $children = Get-BundleFiles $top
                if ($Dropped.Count -ne 1 -or $null -eq $children) {
                    throw (LocalText 'vm-guest フォルダは、決まったファイルだけを入れて単独で送ってください。' 'Send vm-guest by itself, with the exact bundle file set.')
                }
                $bundleKey = Join-Path $top.FullName 'vm-guest.key'
                foreach ($child in ($children | Sort-Object Name)) { $pending.Push($child) }
            }
            else { $pending.Push($top) }
        }
        finally { Close-SendPath $checked.Handles }
        while ($pending.Count -gt 0) {
            $item = $pending.Pop()
            if ((Test-Private $item.Name) -and $item.FullName -cne $bundleKey) {
                throw (LocalText "秘密のファイルに見えるので止めました: $($item.Name)" "Stopped because this looks like a private file: $($item.Name)")
            }
            $checked = Open-SendPath $item.FullName
            try {
                if ($checked.Directory) {
                    if ($null -ne $bundleKey) { throw 'The guest bundle changed during inspection.' }
                    foreach ($child in (Get-ChildItem -LiteralPath $item.FullName -Force | Sort-Object Name)) { $pending.Push($child) }
                    continue
                }
                [void]$items.Add([ordered]@{
                    Source = $item.FullName; Length = [int64]$checked.Length; Identity = $checked.Identity
                    Relative = $item.FullName.Substring($base.Length).TrimStart('\')
                })
            }
            finally { Close-SendPath $checked.Handles }
            if ($items.Count -gt $MaxFiles) { throw (LocalText "一度に送れるのは $MaxFiles 個までです。" "Send at most $MaxFiles files at a time.") }
        }
    }
    if ($items.Count -lt 1) { throw (LocalText '送るファイルがありません。' 'There are no files to send.') }
    $total = [int64]0
    $seen = @{}
    foreach ($item in $items) {
        $total += $item.Length
        if ($seen.ContainsKey($item.Relative.ToLowerInvariant())) { throw (LocalText "同じ名前が 2 つあります: $($item.Relative)" "Two files have the same name: $($item.Relative)") }
        $seen[$item.Relative.ToLowerInvariant()] = $true
    }
    if ($total -gt $MaxBytes) { throw (LocalText "合計が大きすぎます（上限 $([int]($MaxBytes / 1GB)) GB）。" "The combined size exceeds $([int]($MaxBytes / 1GB)) GB.") }
    return , $items.ToArray()
}

function New-SendPlan([string[]]$Dropped) {
    $name = Get-ConfiguredVmName
    $items = Get-SendList $Dropped
    $bundle = $Dropped.Count -eq 1 -and $items[0].Relative.StartsWith('vm-guest\',
        [StringComparison]::Ordinal) -and [IO.Path]::GetFileName($Dropped[0]) -ceq 'vm-guest'
    if ($bundle) {
        $pins = Open-VerifiedBundle $items $name
        foreach ($pin in $pins.Values) { Close-SendPath $pin.Handles }
        # Deliver the key only after the rest of the bundle was successfully copied.
        $items = @($items | Where-Object { $_.Relative -cne 'vm-guest\vm-guest.key' }) +
                 @($items | Where-Object { $_.Relative -ceq 'vm-guest\vm-guest.key' })
    }
    return [ordered]@{ VmName = $name; Dropped = $Dropped; Files = $items; Bundle = $bundle }
}

function Open-VerifiedBundle($Items, [string]$VmName) {
    $pins = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
    $manifestName = 'vm-guest.manifest.json'
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $folder = Split-Path -Parent $Items[0].Source
        foreach ($item in $Items) {
            $name = [IO.Path]::GetFileName($item.Source)
            $pin = Open-SendPath $item.Source
            $pins.Add($name, $pin)
            if ($pin.Directory -or $pin.Identity -cne $item.Identity -or $pin.Length -ne $item.Length) {
                throw "Source changed after inspection: $($item.Relative)"
            }
        }
        $children = Get-BundleFiles (Get-Item -LiteralPath $folder -Force)
        if ($null -eq $children -or $children.Count -ne $pins.Count) { throw 'The guest bundle changed.' }
        foreach ($child in $children) {
            if (-not $pins.ContainsKey($child.Name)) { throw 'The guest bundle changed.' }
        }
        if (-not $pins.ContainsKey($manifestName) -or $pins[$manifestName].Length -gt 8192) {
            throw 'Invalid guest bundle manifest.'
        }
        $digests = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
        $json = $null
        foreach ($name in $pins.Keys) {
            $pin = $pins[$name]
            $handle = [Microsoft.Win32.SafeHandles.SafeFileHandle]::new($pin.Handle.DangerousGetHandle(), $false)
            $stream = [IO.FileStream]::new($handle, [IO.FileAccess]::Read)
            try {
                if ($name -ceq $manifestName) {
                    $bytes = [IO.BinaryReader]::new($stream).ReadBytes([int]$pin.Length + 1)
                    $json = [Text.UTF8Encoding]::new($false, $true).GetString($bytes)
                }
                elseif ($name -ceq 'vm-guest.key') {
                    if ($pin.Length -lt 64 -or $pin.Length -gt 66 -or
                        [Text.Encoding]::ASCII.GetString([IO.BinaryReader]::new($stream).ReadBytes(67)) -cnotmatch '\A[0-9a-f]{64}(?:\r?\n)?\z') {
                        throw 'Invalid guest bundle key format.'
                    }
                }
                else {
                    if ($pin.Length -gt 4MB) { throw "Guest bundle file is too large: $name" }
                    $digests.Add($name, [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant())
                }
            }
            finally { $stream.Dispose() }
        }
        # The generator writes these three members in order. Require exact names and reject repeated hashes.
        $header = '\A\s*\{\s*"format"\s*:\s*1\s*,\s*"vm_name"\s*:\s*"(?:[^"\\\x00-\x1f]|\\["\\])*"\s*,\s*"sha256"\s*:\s*\{'
        $entry = '"([a-z0-9._-]+)"\s*:\s*"([a-f0-9]{64})"'
        $shape = $header + '\s*' + $entry + '(?:\s*,\s*' + $entry + ')*\s*\}\s*\}\s*\z'
        if ($json -cnotmatch $shape) { throw 'Invalid guest bundle manifest.' }
        $manifest = ConvertFrom-Json -InputObject $json
        if ($manifest.vm_name -cne $VmName) { throw 'The guest bundle is for a different VM.' }
        foreach ($match in [regex]::Matches($json.Substring([regex]::Match($json, $header).Length), $entry)) {
            $name = $match.Groups[1].Value
            if (-not $digests.ContainsKey($name) -or $digests[$name] -cne $match.Groups[2].Value) {
                throw "Guest bundle hash mismatch: $name"
            }
            [void]$digests.Remove($name)
        }
        if ($digests.Count -ne 0) { throw 'The guest bundle manifest is missing files.' }
        return ,$pins
    }
    catch {
        foreach ($pin in $pins.Values) { Close-SendPath $pin.Handles }
        throw
    }
    finally { $sha.Dispose() }
}

function Get-PlanBytes($Plan) {
    return , [Text.UTF8Encoding]::new($false, $true).GetBytes((ConvertTo-Json -InputObject $Plan -Depth 4 -Compress))
}

function Get-PlanHash([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($Bytes)).Replace('-', '') }
    finally { $sha.Dispose() }
}

function Read-SendPlan([string]$Path, [string]$Hash) {
    if ($Hash -cnotmatch '\A[0-9A-F]{64}\z') { throw 'Invalid send plan hash.' }
    $checked = Open-SendPath $Path 7
    try {
        if ($checked.Directory -or $checked.Length -gt $MaxPlanBytes) { throw 'Invalid send plan.' }
        $stream = [IO.FileStream]::new($checked.Handle, [IO.FileAccess]::Read)
        try { $bytes = [IO.BinaryReader]::new($stream).ReadBytes([int]$checked.Length + 1) }
        finally { $stream.Dispose() }
        if ((Get-PlanHash $bytes) -cne $Hash) { throw 'The send plan changed before elevation.' }
        return (ConvertFrom-Json -InputObject ([Text.UTF8Encoding]::new($false, $true).GetString($bytes)))
    }
    finally { Close-SendPath $checked.Handles }
}

function Get-ReadyVm([string]$VmName) {
    $vm = @(Get-VM -Name $VmName)
    if ($vm.Count -ne 1 -or $vm[0].Name -ne $VmName) { throw (LocalText "$VmName が見つかりません。" "Cannot find $VmName.") }
    if ("$($vm[0].State)" -ne 'Running') { throw (LocalText "$VmName を起動してから、もう一度重ねてください。" "Start $VmName and drop the files again.") }
    $service = @(Get-VMIntegrationService -VM $vm[0] | Where-Object { "$($_.Id)" -match "(?i)$GuestServiceId`$" })
    if ($service.Count -ne 1 -or -not $service[0].Enabled) {
        throw (LocalText 'Hyper-V マネージャーで、VM の設定 → 統合サービス → 「ゲスト サービス」にチェックを入れてください。' 'In Hyper-V Manager, enable Guest Services under the VM settings, Integration Services.')
    }
    return $vm[0]
}

function Send-Files($Vm, $Items, [string]$Folder) {
    # A new folder per run, never -Force: nothing in the VM is overwritten. Stops at the first failure.
    $done = 0
    foreach ($item in $Items) {
        $checked = Open-SendPath $item.Source
        try {
            if ($checked.Directory -or $checked.Identity -cne $item.Identity -or $checked.Length -ne $item.Length) {
                throw "Source changed after inspection: $($item.Relative)"
            }
            # Keep the source read-only and all ancestors open without delete sharing until Hyper-V returns.
            Copy-VMFile -VM $Vm -SourcePath $item.Source -DestinationPath "$Folder\$($item.Relative)" -FileSource Host -CreateFullPath
        }
        finally { Close-SendPath $checked.Handles }
        $done++
        Write-Host "OK $done/$($Items.Count) $($item.Relative)"
    }
}

function New-GuestFolder {
    return "$GuestRoot\$((Get-Date).ToString('yyyyMMdd-HHmmss', [Globalization.CultureInfo]::InvariantCulture))-$([Guid]::NewGuid().ToString('N'))"
}

function Invoke-Send([string[]]$Dropped, [string]$ExpectedHash) {
    $plan = New-SendPlan $Dropped
    if ((Get-PlanHash (Get-PlanBytes $plan)) -cne $ExpectedHash) { throw 'The VM or the source list changed before elevation.' }
    $VmName = $plan.VmName
    $items = $plan.Files
    $vm = Get-ReadyVm $VmName
    $total = [int64]0
    foreach ($item in $items) { $total += $item.Length }
    Write-Host (LocalText "$VmName へ送ります: $($items.Count) 個、$([math]::Round($total / 1MB, 1)) MB" "Send $($items.Count) items to $($VmName): $([math]::Round($total / 1MB, 1)) MB")
    for ($i = 0; $i -lt [Math]::Min($items.Count, 20); $i++) { Write-Host "  $($items[$i].Relative)" }
    if ($items.Count -gt 20) { Write-Host (LocalText "  ほか $($items.Count - 20) 個" "  and $($items.Count - 20) more") }
    Write-Host (LocalText 'VM の画面に出た中身は ChatGPT にも見えることがあります。' 'ChatGPT may be able to see content displayed on the VM screen.')
    if ((Read-Host (LocalText '送るなら y を押して Enter' 'Press y and Enter to send')) -ne 'y') {
        Write-Host (LocalText 'やめました。何も送っていません。' 'Cancelled. Nothing was sent.')
        return
    }
    $folder = New-GuestFolder
    $pins = $null
    try {
        if ($plan.Bundle) { $pins = Open-VerifiedBundle $items $VmName }
        Send-Files $vm $items $folder
    }
    catch {
        Write-Host (LocalText "途中で止まりました: $($_.Exception.Message)" "Transfer stopped: $($_.Exception.Message)")
        Write-Host (LocalText "VM の $folder に途中までの結果がある場合があります。" "Partial results may be in $folder on the VM.")
        throw
    }
    finally {
        if ($null -ne $pins) {
            foreach ($pin in $pins.Values) { Close-SendPath $pin.Handles }
        }
    }
    Write-Host (LocalText "終わりました。VM の $folder にあります。" "Done. The files are in $folder on the VM.")
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-ElevatedArguments([string]$Script, [string]$Path, [string]$Hash) {
    return "-NoProfile -ExecutionPolicy RemoteSigned -File `"$Script`" -Elevated -PlanPath `"$Path`" -PlanHash $Hash"
}

if ($MyInvocation.InvocationName -ne '.') {
    $code = 0
    try {
        if (-not $Elevated) {
            # Check first, so a wrong drop never reaches the administrator prompt.
            $bytes = Get-PlanBytes (New-SendPlan $Paths)
            if ($bytes.Length -gt $MaxPlanBytes) { throw 'Send plan too large.' }
            $hash = Get-PlanHash $bytes
            $path = Join-Path ([IO.Path]::GetTempPath()) ('scn-send-' + [Guid]::NewGuid().ToString('N') + '.json')
            # Keep the plan open and let Windows remove it on close. No key or file contents are saved here.
            $planFile = [IO.FileStream]::new($path, 'CreateNew', 'Write', 'Read', 4096, 'DeleteOnClose')
            try {
                $planFile.Write($bytes, 0, $bytes.Length)
                $planFile.Flush()
                $arguments = Get-ElevatedArguments $PSCommandPath $path $hash
                $child = Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Verb RunAs -Wait -PassThru -ArgumentList $arguments
                exit $child.ExitCode
            }
            finally {
                $planFile.Dispose()
                # DeleteOnClose is not enough: another handle opened for checking can keep the file. Remove it by its exact path.
                if ([IO.File]::Exists($path)) { [IO.File]::Delete($path) }
            }
        }
        if (-not (Test-Admin)) { throw (LocalText '管理者として動いていません。' 'This process is not running as administrator.') }
        $plan = Read-SendPlan $PlanPath $PlanHash
        Invoke-Send ([string[]]$plan.Dropped) $PlanHash
    }
    catch { Write-Host "[HOLD] $($_.Exception.Message)"; $code = 2 }
    [void](Read-Host (LocalText 'Enter で閉じます' 'Press Enter to close'))
    exit $code
}
