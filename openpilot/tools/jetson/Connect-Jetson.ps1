param(
  [string]$CommaHost = '192.168.0.43',
  [switch]$Inspect,
  [switch]$RuntimeDetails,
  [switch]$DiscoverOnly
)

$ErrorActionPreference = 'Stop'
$commaKey = Join-Path $env:USERPROFILE '.ssh/id_rsa'
$jetsonKey = Join-Path $env:USERPROFILE '.ssh/carrot_jetson'
foreach ($key in @($commaKey, $jetsonKey)) {
  if (-not (Test-Path -LiteralPath $key -PathType Leaf)) {
    throw "SSH key file missing: $key"
  }
}
if ($CommaHost -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]*$') {
  throw 'Invalid comma host.'
}

Write-Host 'Discovering the current Jetson USB address through comma...'
$discovery = @'
import ipaddress
import json
from pathlib import Path
import socket
import subprocess
import time

def run(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(args, 124, '', str(error))

def record(command):
    result = run(command)
    return {'returncode': result.returncode, 'stdout': result.stdout.strip(), 'stderr': result.stderr.strip()}

def read(name):
    try:
        return Path('/sys/class/net/usb0/' + name).read_text().strip()
    except OSError as error:
        return 'read error: ' + str(error)

def discover():
    report = {'peers': [], 'attempts': [], 'errors': []}
    link = record(['ip', '-o', 'link', 'show', 'dev', 'usb0'])
    report['initial_link'] = link
    if link['returncode']:
        report['errors'].append('usb0 could not be read.')
        return report
    raw = link['stdout']
    flags = raw.split('<', 1)[1].split('>', 1)[0].split(',') if '<' in raw else []
    if 'UP' not in flags:
        report['bring_up'] = record(['sudo', '-n', 'ip', 'link', 'set', 'usb0', 'up'])
        if report['bring_up']['returncode']:
            report['errors'].append('Could not bring usb0 UP.')
            return report
        time.sleep(2)  # Allow IPv6 address initialization after administrative UP.
    deadline = time.monotonic() + 25
    try:
        scope = socket.if_nametoindex('usb0')
    except OSError as error:
        report['errors'].append(str(error))
        return report
    for attempt in range(3):
        status = {'number': attempt + 1,
                  'link': record(['ip', '-br', 'link', 'show', 'usb0']),
                  'carrier': read('carrier'), 'operstate': read('operstate'),
                  'ipv6': record(['ip', '-o', '-6', 'addr', 'show', 'dev', 'usb0'])}
        own = set()
        for line in status['ipv6']['stdout'].splitlines():
            fields = line.split()
            if 'inet6' in fields:
                try:
                    own.add(str(ipaddress.IPv6Address(fields[fields.index('inet6') + 1].split('/')[0])))
                except (ValueError, IndexError):
                    pass
        status['ping'] = record(['ping', '-6', '-c', '2', '-I', 'usb0', 'ff02::1'])
        status['neighbors'] = record(['ip', '-6', 'neigh', 'show', 'dev', 'usb0'])
        candidates = []
        for line in status['neighbors']['stdout'].splitlines():
            fields = line.split()
            if fields and 'lladdr' in fields and fields[-1] not in ('FAILED', 'INCOMPLETE'):
                candidates.append(fields[0])
        for line in status['ping']['stdout'].splitlines():
            if 'bytes from ' in line:
                token = line.split('bytes from ', 1)[1].split()[0]
                candidates.append(token.split('%', 1)[0] if '%' in token else token.removesuffix(':'))
        peers = []
        for candidate in candidates:
            try:
                address = ipaddress.IPv6Address(candidate)
            except ValueError:
                continue
            address_text = str(address)
            if own and address.is_link_local and address_text not in own and address_text not in peers:
                peers.append(address_text)
        if not own:
            status['selection_note'] = 'comma has no readable IPv6 address; peer selection was skipped.'
        results = []
        if len(peers) > 8:
            report['errors'].append('Too many USB neighbors; selection was refused.')
        else:
            for address in peers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as connection:
                        connection.settimeout(min(2, remaining))
                        connection.connect((address, 22, 0, scope))
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('Discovery deadline expired.')
                        connection.settimeout(min(2, remaining))
                        banner = connection.recv(512).decode('ascii', errors='replace').strip()
                    results.append({'address': address, 'ssh': banner.startswith('SSH-'), 'detail': banner})
                except OSError as error:
                    results.append({'address': address, 'ssh': False, 'detail': str(error)})
        status['probes'] = results
        report['peers'] = results
        report['attempts'].append(status)
        if any(peer['ssh'] for peer in results) or report['errors'] or time.monotonic() >= deadline:
            break
        if attempt < 2:
            time.sleep(2)
    return report

print(json.dumps(discover()))
'@
$raw = $discovery | & ssh -o IdentitiesOnly=yes -o ConnectTimeout=5 -i $commaKey "comma@$CommaHost" python3 -
if ($LASTEXITCODE -ne 0) {
  throw 'comma discovery failed. Installation has not started.'
}
$result = ($raw -join "`n") | ConvertFrom-Json
$ready = @($result.peers | Where-Object { $_.ssh })
if ($ready.Count -ne 1) {
  Write-Host 'USB discovery details:'
  $result | ConvertTo-Json -Depth 10 | Out-Host
  throw 'USB discovery did not find exactly one SSH peer. Send the USB discovery details above. No installation was attempted.'
}
$address = [string]$ready[0].address
$parsed = $null
if (-not [System.Net.IPAddress]::TryParse($address, [ref]$parsed) -or -not $parsed.IsIPv6LinkLocal) {
  throw 'Unexpected peer address.'
}
$target = "jetlink@${address}%usb0"
if ($DiscoverOnly) {
  [pscustomobject]@{ Target = $target; Address = $address; CommaHost = $CommaHost }
  return
}
if ($Inspect) {
  Write-Host "Reading Jetson installation and storage status from $target..."
  $inspection = @'
import json
import os
from pathlib import Path
import socket
import subprocess

if not Path('/etc/nv_tegra_release').is_file():
    raise SystemExit('Target is not a Jetson. No installation was attempted.')

def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError as error:
        return str(error)

def run(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return {'returncode': result.returncode, 'stdout': result.stdout.strip(), 'stderr': result.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'error': str(error)}

report = {
    'hostname': socket.gethostname(),
    'l4t': read('/etc/nv_tegra_release'),
    'protected_config': read('/etc/carrot-jetlink-protected.json'),
    'storage': read('/run/carrot-storage.json'),
    'runtime_source': os.path.realpath('/opt/carrot-jetlink/current'),
    'runtime_mount': run(['findmnt', '-T', '/opt/carrot-jetlink', '-no', 'TARGET,SOURCE,FSTYPE,OPTIONS']),
    'etc_mount': run(['findmnt', '-T', '/etc/systemd/system', '-no', 'TARGET,SOURCE,FSTYPE,OPTIONS']),
    'data_mount': run(['findmnt', '-T', '/run/carrot-data', '-no', 'TARGET,SOURCE,FSTYPE,OPTIONS']),
    'data_paths': run(['sudo', '-n', 'ls', '-ld', '/run/carrot-data', '/run/carrot-data/runtime', '/opt/carrot-jetlink/current']),
    'space': run(['df', '-h', '/opt/carrot-jetlink']),
    'services': run(['systemctl', 'is-active', 'carrot-jetlink.service', 'carrot-jetlink-hud.service', 'carrot-jetlink-xorg.service']),
    'dependencies': run(['/opt/carrot-jetlink/venv/bin/python', '-c', 'import importlib.util,json; print(json.dumps({name:bool(importlib.util.find_spec(name)) for name in ["numpy","capnp","usb1","usb","pyray","av","tensorrt","scons"]}))']),
}
print(json.dumps(report, indent=2))
'@
  if ($RuntimeDetails) {
    $inspection += @'

report = {
    'runtime_units': run(['systemctl', 'show', 'carrot-jetlink.service', 'carrot-jetlink-hud.service', 'carrot-jetlink-update-apply.service', 'carrot-jetlink-update-stage.timer', '--no-pager', '-p', 'User', '-p', 'WorkingDirectory', '-p', 'ExecStart', '-p', 'ActiveState', '-p', 'FragmentPath', '-p', 'DropInPaths']),
    'sudo': run(['sudo', '-n', 'true']),
    'cache_metadata': run(['sudo', '-n', 'cat', '/opt/carrot-jetlink/cache/last-loaded.json']),
    'update_records': run(['sudo', '-n', 'ls', '-l', '/opt/carrot-jetlink/updates/pending.json', '/opt/carrot-jetlink/updates/transaction.json']),
    'runtime_versions': run(['/opt/carrot-jetlink/venv/bin/python', '-c', 'import sys,json,importlib.util,tensorrt; print(json.dumps({"python":sys.version,"trt":tensorrt.__version__,"modules":{n:bool(importlib.util.find_spec(n)) for n in ["aiohttp","onnx","onnxruntime","PIL","Crypto","zstandard","psutil","msgq","capnp","cv2"]}}))']),
    'ffmpeg_libx264': 'libx264' in run(['ffmpeg', '-hide_banner', '-encoders']).get('stdout', ''),
    'persistent_updater': run(['sudo', '-n', 'ls', '-ld', '/opt/carrot-jetlink/updater', '/opt/carrot-jetlink/releases', '/opt/carrot-jetlink/cache/models']),
    'runtime_source_identity': read('/opt/carrot-jetlink/current/SOURCE_COMMIT'),
    'uptime': read('/proc/uptime'),
    'interfaces': run(['ip', '-br', 'addr']),
    'suspend_stats': run(['sudo', '-n', 'cat', '/sys/kernel/debug/suspend_stats']),
    'power_mode': {name: read('/sys/power/' + name) for name in ['state', 'mem_sleep']},
}
import re
for name, args in {
    'server_events': ['sudo', '-n', 'journalctl', '-b', '-u', 'carrot-jetlink.service', '--no-pager', '-n', '80'],
    'kernel_events': ['sudo', '-n', 'journalctl', '-b', '-k', '--no-pager', '-n', '120'],
}.items():
    entry = run(args)
    lines = [line for line in entry.get('stdout', '').splitlines()
             if re.search(r'suspend|sleep|resume|usb|gadget|disconnect|error|failed|traceback|exception', line, re.I)]
    entry['stdout'] = '\n'.join('[sensitive entry omitted]' if re.search(r'password|passwd|secret|token|ssid|psk|authorization', line, re.I) else line for line in lines[-25:])
    report[name] = entry
print(json.dumps({'runtime_details': report}, indent=2))
'@
  }
  $inspection | & ssh -T -o IdentitiesOnly=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ConnectTimeout=5 -i $jetsonKey -J "comma@$CommaHost" $target python3 -
  if ($LASTEXITCODE -ne 0) {
    throw 'Jetson status check did not complete. Re-run the same Windows command to discover the current address again. Installation has not started.'
  }
  Write-Host 'Jetson status check completed. Installation has not started.'
  return
}
Write-Host "Connecting to $target. This opens a terminal; it does not install anything."
& ssh -t -o IdentitiesOnly=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ConnectTimeout=5 -i $jetsonKey -J "comma@$CommaHost" $target
if ($LASTEXITCODE -ne 0) {
  throw 'Jetson SSH session ended with an error or disconnected. Re-run this script to reconnect. This script does not install anything.'
}
