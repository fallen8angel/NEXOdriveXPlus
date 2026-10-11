param([int]$DiskNumber = -1)
$ErrorActionPreference = 'Stop'
$report = Join-Path $PSScriptRoot 'Jetson-card-check-result.txt'
$support = Join-Path $PSScriptRoot 'card-check'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host 'Opening an administrator window for READ-ONLY card inspection. No installation or formatting.'
  $arguments = @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"{0}"' -f $PSCommandPath),'-DiskNumber',[string]$DiskNumber)
  $child = Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments -Wait -PassThru
  if ($child.ExitCode -ne 0) { throw "Card inspection did not complete. Report (if created): $report" }
  Write-Host "Card report saved: $report"
  return
}
try {
  "Read-only card check attempt: $(Get-Date -Format o). No card selected yet." | Set-Content -LiteralPath $report -Encoding UTF8
  $checks = Get-Content -LiteralPath (Join-Path $support 'checksums.json') -Raw | ConvertFrom-Json
  foreach ($name in @('protected_card_check.py','boot-patch.json','python.exe')) {
    $path = if ($name -eq 'python.exe') { $checks.python_path } else { Join-Path $support $name }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $checks.$name) { throw "Inspection helper checksum mismatch: $name" }
  }
  $source = @(Get-Partition -DriveLetter $PSScriptRoot.Substring(0,1))
  if ($source.Count -ne 1) { throw 'Could not identify the PC source disk.' }
  function Test-Card($disk) {
    return ($null -ne $disk -and [string]$disk.BusType -eq 'USB' -and -not $disk.IsBoot -and
      -not $disk.IsSystem -and -not $disk.IsOffline -and $disk.Number -ne $source[0].DiskNumber -and
      $disk.Size -ge 42949672960 -and -not [string]::IsNullOrWhiteSpace([string]$disk.UniqueId))
  }
  $choices = @(Get-Disk | Where-Object { Test-Card $_ })
  if (-not $choices.Count) { throw 'Connect the Jetson SD card using a USB reader. No eligible card found. Do not format it.' }
  Write-Host 'READ-ONLY CHECK. Select the Jetson card by name and capacity. No erase or install will run.'
  foreach ($disk in $choices) { Write-Host ("[{0}] {1} / {2:N1} GB" -f $disk.Number,$disk.FriendlyName,($disk.Size/1e9)) }
  if ($DiskNumber -lt 0) {
    $answer = Read-Host 'Enter the Jetson card number shown above'
    if (-not [int]::TryParse($answer, [ref]$DiskNumber)) { throw 'Cancelled. Card unchanged.' }
  }
  $selected = @($choices | Where-Object Number -eq $DiskNumber)
  if ($selected.Count -ne 1) { throw 'Disk is not in the eligible list. Card unchanged.' }
  $selected = $selected[0]
  function Assert-Card {
    $current = Get-Disk -Number $DiskNumber
    if (-not (Test-Card $current) -or $current.UniqueId -ne $selected.UniqueId -or
      $current.Size -ne $selected.Size -or ([string]$current.SerialNumber).Trim() -ne ([string]$selected.SerialNumber).Trim()) {
      throw 'Card identity changed. Inspection stopped.'
    }
  }
  Assert-Card
  "Read-only card check started: $(Get-Date -Format o). Disk $DiskNumber; bytes $($selected.Size)." | Set-Content -LiteralPath $report -Encoding UTF8
  Write-Host 'Reading the Linux system partition. This can take several minutes. Keep the card connected.'
  & $checks.python_path -I -u -X utf8 (Join-Path $support 'protected_card_check.py') --target "\\.\PhysicalDrive$DiskNumber" `
    --manifest (Join-Path $support 'boot-patch.json') --manifest-sha256 $checks.'boot-patch.json' | Tee-Object -FilePath $report -Append
  $checkExit = $LASTEXITCODE
  Assert-Card
  if ($checkExit -notin @(0,2)) { throw 'Card check could not complete; no card writes were attempted.' }
  Write-Host "Report saved: $report"
  Write-Host 'Only APP compatibility was checked. NEXO is not installed. Send the report before any patch.'
} catch {
  $_.Exception.Message | Tee-Object -FilePath $report -Append | Out-Host
  Read-Host 'Check failed. Card unchanged. Press Enter to close' | Out-Null
  exit 1
}
Read-Host 'Read-only check finished. Press Enter to close' | Out-Null
