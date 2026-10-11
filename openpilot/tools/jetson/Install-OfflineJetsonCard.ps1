param([int]$DiskNumber = -1, [switch]$Apply, [switch]$CopyPayload,
      [string]$ExpectedId, [long]$ExpectedSize = 0)
$ErrorActionPreference = 'Stop'
$support = Join-Path $PSScriptRoot 'offline-card'
$report = Join-Path $PSScriptRoot 'Jetson-offline-card-result.txt'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  if ($CopyPayload) { throw 'Administrator child required' }
  $arguments = @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"{0}"' -f $PSCommandPath),'-DiskNumber',[string]$DiskNumber)
  if ($Apply) { $arguments += '-Apply' }
  $child = Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments -Wait -PassThru
  if ($child.ExitCode -ne 0) { throw "Card preparation failed. Read $report. Do not format or boot after an interrupted APP write." }
  Write-Host "Card preparation report: $report"
  return
}
$locks = [Collections.Generic.List[System.IDisposable]]::new()
function Report([string]$line) { Write-Host $line; Add-Content -LiteralPath $report -Value $line -Encoding UTF8 }
try {
  if (-not $CopyPayload) {
    "NEXO offline card preparation: $(Get-Date -Format o). No installation started." | Set-Content -LiteralPath $report -Encoding UTF8
  }
  $checks = Get-Content -LiteralPath (Join-Path $support 'checksums.json') -Raw | ConvertFrom-Json
  foreach ($name in @('offline_card_patch.py','protected_card_check.py','nexo-offline.json','nexo-offline-runtime.tar.gz','python.exe')) {
    $path = if ($name -eq 'python.exe') { $checks.python_path } else { Join-Path $support $name }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $checks.$name) { throw "Offline helper checksum mismatch: $name" }
  }
  $index = Get-Content -LiteralPath (Join-Path $support 'nexo-offline.json') -Raw | ConvertFrom-Json
  $source = @(Get-Partition -DriveLetter $PSScriptRoot.Substring(0,1))
  if ($source.Count -ne 1) { throw 'Cannot identify the PC source disk' }
  function Test-Target($disk) {
    return ($null -ne $disk -and [string]$disk.BusType -eq 'USB' -and -not $disk.IsBoot -and -not $disk.IsSystem -and
      -not $disk.IsOffline -and -not $disk.IsReadOnly -and $disk.Number -ne $source[0].DiskNumber -and
      $disk.Size -ge $index.compatibility.profiles[0].image_bytes -and -not [string]::IsNullOrWhiteSpace([string]$disk.UniqueId))
  }
  $choices = @(Get-Disk | Where-Object { Test-Target $_ })
  if (-not $choices.Count) { throw 'No eligible Jetson USB card. Do not format it.' }
  if ($DiskNumber -lt 0) {
    foreach ($disk in $choices) { Write-Host ("[{0}] {1} / {2:N1} GB" -f $disk.Number,$disk.FriendlyName,($disk.Size/1e9)) }
    $answer = Read-Host 'Enter the Jetson card number'
    if (-not [int]::TryParse($answer,[ref]$DiskNumber)) { throw 'Cancelled. Card unchanged.' }
  }
  $selected = @($choices | Where-Object Number -eq $DiskNumber)
  if ($selected.Count -ne 1) { throw 'Disk is not an eligible card' }
  $selected = $selected[0]
  function Assert-Disk {
    $current = Get-Disk -Number $DiskNumber
    if (-not (Test-Target $current) -or $current.UniqueId -ne $selected.UniqueId -or $current.Size -ne $selected.Size -or
        ([string]$current.SerialNumber).Trim() -ne ([string]$selected.SerialNumber).Trim()) { throw 'Card identity changed' }
    if ($CopyPayload -and ($current.UniqueId -ne $ExpectedId -or $current.Size -ne $ExpectedSize)) { throw 'Child card identity changed' }
  }
  function Assert-Target {
    Assert-Disk
    $partition = Get-Partition -DiskNumber $DiskNumber -PartitionNumber 16
    if ($partition.Offset -ne $index.compatibility.setup_offset -or $partition.Size -ne $index.compatibility.setup_bytes) { throw 'Unexpected SETUP layout' }
    $volume = $partition | Get-Volume
    if ($volume.FileSystemLabel -ne 'CARROTSETUP' -or $volume.FileSystem -ne 'FAT32') { throw 'Expected CARROTSETUP FAT32' }
    return $partition
  }
  $null = Assert-Target
  if (-not $Apply) {
    Report ("READY: disk {0}, {1:N1} GB, source {2}. Use -Apply to stage the runtime and patch its APP entry." -f $DiskNumber,($selected.Size/1e9),$index.source_commit)
    return
  }
  if ($CopyPayload) {
    $partition = Assert-Target
    $volume = $partition | Get-Volume
    # Require an existing assigned letter; don't alter mount points in this
    # optional installer. The read-only preflight already identified SETUP.
    if (-not $volume.DriveLetter) { throw 'SETUP has no existing drive letter; nothing copied' }
    $directory = '{0}:\' -f $volume.DriveLetter
    $destination = Join-Path $directory 'nexo-offline-runtime.tar.gz'
    $temporary = Join-Path $directory 'nexo-offline-runtime.new'
    if (Test-Path -LiteralPath $destination) {
      if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $index.bundle_sha256) {
        throw 'A different offline runtime is already staged; it was not overwritten'
      }
      return
    }
    if ($volume.SizeRemaining -lt $index.bundle_bytes + 131072) { throw 'SETUP has insufficient space; nothing copied' }
    if (Test-Path -LiteralPath $temporary) {
      if ((Get-Item -LiteralPath $temporary).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Unexpected staging link' }
    }
    $reader = [IO.File]::OpenRead((Join-Path $support 'nexo-offline-runtime.tar.gz'))
    try {
      $writer = [IO.File]::Open($temporary,[IO.FileMode]::Create,[IO.FileAccess]::Write,[IO.FileShare]::None)
      try { $reader.CopyTo($writer); $writer.Flush($true) } finally { $writer.Dispose() }
    } finally { $reader.Dispose() }
    $null = Assert-Target
    if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $index.bundle_sha256) { throw 'SETUP copy readback differs' }
    [IO.File]::Move($temporary,$destination)
    Report 'SETUP runtime copy verified. APP has not been written yet.'
    return
  }
  Report ("Preparing Jetson card disk {0} / {1:N1} GB. No format; APP hook 6144 bytes, verified SETUP payload {2} bytes." -f $DiskNumber,($selected.Size/1e9),$index.bundle_bytes)
  # Provider handles close with this child before raw volume locking. A partial
  # copy changes no APP entry and can be replaced on the next identical attempt.
  $quotedScript = $PSCommandPath.Replace("'","''")
  $quotedId = ([string]$selected.UniqueId).Replace("'","''")
  $copyCode = "& '$quotedScript' -CopyPayload -Apply -DiskNumber $DiskNumber -ExpectedId '$quotedId' -ExpectedSize $($selected.Size)"
  $encodedCopy = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($copyCode))
  $arguments = @('-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',$encodedCopy)
  $copy = Start-Process powershell.exe -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
  if ($copy.ExitCode -ne 0) { throw 'SETUP copy failed. APP unchanged. Check the report.' }
  if (-not ('NexoOfflineNative' -as [type])) { Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class NexoOfflineNative {
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern bool DeviceIoControl(SafeFileHandle file, uint control, IntPtr input, uint inputBytes, IntPtr output, uint outputBytes, out uint returned, IntPtr overlapped);
  [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int n);
  [DllImport("kernel32.dll")] static extern bool GetConsoleMode(IntPtr h, out uint mode);
  [DllImport("kernel32.dll")] static extern bool SetConsoleMode(IntPtr h, uint mode);
  public static void DisableSelection() { uint mode; var h=GetStdHandle(-10); if(GetConsoleMode(h,out mode)) SetConsoleMode(h,(mode|0x80)&~0x40u); }
  public static SafeFileHandle Open(string path) {
    var handle=CreateFile(path,0xC0000000,3,IntPtr.Zero,3,0,IntPtr.Zero);
    if(handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
    return handle;
  }
  public static void Control(SafeFileHandle handle,uint code) {
    uint count; if(!DeviceIoControl(handle,code,IntPtr.Zero,0,IntPtr.Zero,0,out count,IntPtr.Zero)) throw new Win32Exception(Marshal.GetLastWin32Error());
  }
}
'@ }
  [NexoOfflineNative]::DisableSelection()
  $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
  foreach ($partition in (Get-Partition -DiskNumber $DiskNumber)) {
    foreach ($path in $partition.AccessPaths) {
      if ($path -like '\\?\Volume{*' -and $seen.Add($path.TrimEnd('\'))) {
        $locked = $false
        for ($attempt=0; $attempt -lt 31; $attempt++) {
          Assert-Disk
          $handle = $null
          try {
            $handle = [NexoOfflineNative]::Open($path.TrimEnd('\'))
            [NexoOfflineNative]::Control($handle,0x00090018)
            [NexoOfflineNative]::Control($handle,0x00090020)
            $locks.Add($handle); $locked=$true; break
          } catch {
            if ($handle) { $handle.Dispose() }
            $failure=$_.Exception; while ($failure.InnerException) { $failure=$failure.InnerException }
            if ($failure -isnot [ComponentModel.Win32Exception] -or $failure.NativeErrorCode -notin @(5,32,33)) { throw }
            Start-Sleep -Seconds 1
          }
        }
        if (-not $locked) { throw 'Close Explorer windows using the card and rerun. Volume lock failed; APP unchanged.' }
      }
    }
  }
  if ($locks.Count -lt 1) { throw 'No Windows volume was locked; APP unchanged' }
  Assert-Disk
  $backup = Join-Path $support 'APP-first-boot-recovery.json'
  & $checks.python_path -I -u -X utf8 (Join-Path $support 'offline_card_patch.py') --target "\\.\PhysicalDrive$DiskNumber" `
    --manifest (Join-Path $support 'nexo-offline.json') --manifest-sha256 $checks.'nexo-offline.json' --backup $backup | ForEach-Object {
      $line = [string]$_
      if ($line -match '^APP_READ ([0-9]+)/([0-9]+)$') {
        $percent = [Math]::Round(100.0 * [double]$Matches[1] / [double]$Matches[2],1)
        Report ("APP check: $percent percent")
      } else { Report $line }
    }
  if ($LASTEXITCODE -ne 0) { throw 'APP preparation not verified. Keep the card connected and send this report; do not boot or format.' }
  # No Storage/provider access while volumes are dismounted; Python has just
  # reread the APP bytes, and locked handles prevent a live filesystem writer.
  Report 'PC_PREPARATION_COMPLETE. Jetson installation has NOT started. First boot needs up to 20 minutes; parked device only.'
} catch {
  if ($CopyPayload) { Add-Content -LiteralPath $report -Value $_.Exception.Message -Encoding UTF8; exit 1 }
  Report ('FAILED: ' + $_.Exception.Message)
  Read-Host 'Press Enter to close' | Out-Null
  exit 1
} finally {
  foreach ($handle in $locks) { $handle.Dispose() }
}
if (-not $CopyPayload) { Read-Host 'Preparation finished. Press Enter to close' | Out-Null }
