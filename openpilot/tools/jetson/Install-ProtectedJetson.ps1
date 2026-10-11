param(
  [string]$CommaHost = '192.168.0.43',
  [switch]$Status
)
$ErrorActionPreference = 'Stop'
$commaKey = Join-Path $env:USERPROFILE '.ssh/id_rsa'
$jetsonKey = Join-Path $env:USERPROFILE '.ssh/carrot_jetson'
$peer = & (Join-Path $PSScriptRoot 'Connect-Jetson.ps1') -CommaHost $CommaHost -DiscoverOnly
if (-not $peer.Target) { throw 'No verified USB SSH peer was discovered.' }
$sshArgs = @('-T','-o','IdentitiesOnly=yes','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3','-o','ConnectTimeout=5','-i',$jetsonKey,'-J',"comma@$CommaHost",$peer.Target,'python3','-')
$report = Join-Path $PSScriptRoot 'Jetson-install-result.txt'
$monitor = @'
import json, subprocess, time
from pathlib import Path
def status():
    result = subprocess.run(['sudo','-n','cat','/opt/carrot-jetlink/updates/nexo-install-status.json'], capture_output=True, text=True, timeout=10)
    if result.returncode:
        return {'state': 'no_install_record'}
    return json.loads(result.stdout)
def unit():
    result = subprocess.run(['systemctl','show','nexo-jetlink-install.service','-p','ActiveState','-p','ExecMainStatus'], capture_output=True, text=True, timeout=10)
    return dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
'@
if ($Status) {
  $program = $monitor + @'

print(json.dumps({'installation':status(),'worker':unit()},indent=2),flush=True)
'@
  $program | & ssh @sshArgs | Tee-Object -FilePath $report
  if ($LASTEXITCODE -ne 0) { throw 'Status could not be read. No installation was started.' }
  exit 0
}
# Verify the comma's own state before stopping any optional Jetson service.
$current = Invoke-RestMethod -Uri "http://${CommaHost}:7000/api/jetson/status" -TimeoutSec 10
$link = $current.local_jetlink
if ($link.offroad -ne $true -or $link.onroad -ne $false -or $link.enabled -ne $true -or $link.display_usb -ne $false -or $link.waiting_modeld -ne $true) {
  throw 'Keep comma offroad, enable Jetlink inference, and turn NexoJetsonUsb OFF before installation.'
}
$bundle = Join-Path $PSScriptRoot 'nexo-protected-runtime.tar.gz'
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'nexo-protected-runtime.json') -Raw | ConvertFrom-Json
$installer = Join-Path $PSScriptRoot 'protected_install.py'
if ($manifest.bundle_sha256 -notmatch '^[0-9a-f]{64}$' -or $manifest.source_commit -notmatch '^[0-9a-f]{40}$') { throw 'Invalid local bundle manifest.' }
$sha = (Get-FileHash -LiteralPath $bundle -Algorithm SHA256).Hash.ToLowerInvariant()
$installerSha = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
if ($sha -ne $manifest.bundle_sha256 -or (Get-Item -LiteralPath $bundle).Length -ne $manifest.bundle_size) { throw 'Local bundle checksum differs.' }
Write-Host 'Preparing NEXO installation. Keep comma offroad and keep Jetson power connected until this finishes.'
$remoteDir = (& ssh -T -o IdentitiesOnly=yes -o ConnectTimeout=5 -i $jetsonKey -J "comma@$CommaHost" $peer.Target 'mktemp -d /tmp/nexo-install.XXXXXXXX') -join ''
if ($LASTEXITCODE -ne 0 -or $remoteDir -notmatch '^/tmp/nexo-install\.[A-Za-z0-9]{8}$') { throw 'Jetson upload directory was not created. Installation has not started.' }
$scpTarget = "jetlink@[$($peer.Address)%usb0]"
Write-Host 'Transferring the checked NEXO runtime and installer...'
& scp -o IdentitiesOnly=yes -o ConnectTimeout=5 -i $jetsonKey -J "comma@$CommaHost" $bundle $installer "${scpTarget}:$remoteDir/"
if ($LASTEXITCODE -ne 0) { throw 'Upload failed. Installation has not started.' }
# Check offroad again immediately before the detached, recoverable worker starts.
$current = Invoke-RestMethod -Uri "http://${CommaHost}:7000/api/jetson/status" -TimeoutSec 10
if ($current.local_jetlink.offroad -ne $true -or $current.local_jetlink.onroad -ne $false -or $current.local_jetlink.waiting_modeld -ne $true -or $current.local_jetlink.enabled -ne $true -or $current.local_jetlink.display_usb -ne $false) { throw 'comma is no longer waiting offroad with Jetlink inference enabled. Installation has not started.' }
$program = @'
import hashlib, subprocess
from pathlib import Path
root_code = r'''
import hashlib, subprocess
from pathlib import Path
upload = Path('%UPLOAD%')
raw = (upload / 'protected_install.py').read_bytes()
if hashlib.sha256(raw).hexdigest() != '%INSTALLER_SHA%':
    raise ValueError('Uploaded installer checksum differs')
module = {'__file__': str(upload / 'protected_install.py'), '__name__': 'nexo_install_bootstrap'}
exec(compile(raw, module['__file__'], 'exec'), module)
module['storage_guard']()
state = subprocess.run(['systemctl','is-active','nexo-jetlink-install.service'], capture_output=True,text=True).stdout.strip()
if state in ('active','activating','deactivating'):
    raise RuntimeError('Installation is already running. Use -Status; do not start another worker.')
bundle = upload / 'nexo-protected-runtime.tar.gz'
if bundle.stat().st_size > 256 << 20 or module['digest'](bundle) != '%BUNDLE_SHA%':
    raise ValueError('Uploaded bundle checksum differs')
updates = module['ROOT'] / 'updates'
if updates.is_symlink():
    raise ValueError('Updates directory cannot be a symlink')
updates.mkdir(exist_ok=True)
if (updates / 'nexo-install-transaction.json').exists():
    raise RuntimeError('Interrupted installation needs reboot recovery before retrying')
worker, package = updates / 'nexo-install-worker.py', updates / 'nexo-upload.tar.gz'
module['durable'](worker, raw)
module['durable'](package, bundle.read_bytes(), mode=0o600)
module['store'](updates / 'nexo-install-status.json', {'state':'queued','source_commit':'%SOURCE%'})
subprocess.run(['systemd-run','--collect','--unit=nexo-jetlink-install',
                '--property=Type=oneshot','--property=TimeoutStartSec=1100',
                '--property=KillMode=control-group','--property=Nice=10',
                '/usr/bin/python3',str(worker),'install','--bundle',str(package),'--sha256','%BUNDLE_SHA%'],check=True)
'''
subprocess.run(['sudo','-n','python3','-c',root_code],check=True)
print('Installation started in a separate Jetson service. Model preparation can take several minutes.',flush=True)
'@
$program = $program.Replace('%UPLOAD%', $remoteDir).Replace('%INSTALLER_SHA%', $installerSha).Replace('%BUNDLE_SHA%', $sha).Replace('%SOURCE%', [string]$manifest.source_commit)
$program += "`n" + $monitor + @'

last = None
for attempt in range(240):
    value, worker = status(), unit()
    if value != last:
        print(json.dumps(value,indent=2),flush=True)
        last = value
    if value.get('state') in ('installed','failed','rolled_back') and worker.get('ActiveState') not in ('active','activating','deactivating'):
        if value.get('state') == 'installed':
            print('NEXO installation completed. Real vehicle inference and the physical external HUD still need checking.',flush=True)
            raise SystemExit(0)
        break
    if worker.get('ActiveState') not in ('active','activating'):
        break
    if attempt and attempt % 12 == 0:
        print('Still preparing/testing the NEXO model. Keep power connected.',flush=True)
    time.sleep(5)
print(json.dumps({'installation':status(),'worker':unit()},indent=2),flush=True)
result = subprocess.run(['sudo','-n','journalctl','-u','nexo-jetlink-install.service','-n','25','--no-pager','-o','cat'],capture_output=True,text=True,timeout=10)
print(result.stdout[-6000:],flush=True)
raise SystemExit(1)
'@
$program | & ssh @sshArgs | Tee-Object -FilePath $report
if ($LASTEXITCODE -ne 0) {
  throw 'Connection or installation check ended. The detached worker may still be running. Use this script with -Status before retrying installation.'
}
Write-Host "Report saved: $report"
