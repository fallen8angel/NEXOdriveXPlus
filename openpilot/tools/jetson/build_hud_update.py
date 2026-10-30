"""Build a committed, checksummed Windows -> comma -> protected Jetson HUD ZIP."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import zipfile

from openpilot.tools.jetson.build_protected_bundle import build

BUNDLE = 'nexo-hud-repair.tar.gz'
HELPERS = ('protected_install.py', 'protected_ncm.py', 'install_hud_update.py', 'Connect-Jetson.ps1')


def instructions(commit, checksum):
  return f'''# NEXO Jetson HUD repair

Source commit: `{commit}`
Bundle SHA256: `{checksum}`

차량을 주차하고 콤마가 offroad인 상태에서 진행하세요. 젯슨 전원을 유지하세요.
기존 보호형 NCM Display Only 설정에서만 설치됩니다. APP/SETUP 및 차량 설정을 변경하지 않습니다.
실제 젯슨, Xorg, USB 패널 및 냉간 부팅 검증은 완료되지 않았습니다.

## Windows PowerShell: 압축 해제한 폴더에서 전송

`$comma`에는 현재 콤마의 실제 Wi-Fi IP를 입력하세요. SSH 키는 기존 설치와 같습니다.
ProxyJump의 콤마 키는 기존 SSH 설정의 id_rsa를 사용합니다. 키가 다르면 SSH 설정을 맞추세요.
호스트 키 검증은 끄지 않습니다.

```powershell
$comma = '여기에_콤마_IP'
$key = Join-Path $env:USERPROFILE '.ssh/carrot_jetson'
$peer = & .\\Connect-Jetson.ps1 -CommaHost $comma -DiscoverOnly
if (-not $peer.Target) {{ throw 'Jetson peer not found' }}
$remote = '/opt/carrot-jetlink/updates/hud-{commit[:12]}'
ssh -i $key -J "comma@$comma" $peer.Target "test -f /run/carrot-storage.json && mountpoint -q /opt/carrot-jetlink && sudo install -d -o jetlink -g jetlink -m 0750 $remote"
if ($LASTEXITCODE -ne 0) {{ throw 'DATA preparation failed' }}
$destination = "jetlink@[$($peer.Address)%usb0]:$remote/"
scp -i $key -J "comma@$comma" .\\{BUNDLE} .\\nexo-hud-repair.json .\\protected_install.py .\\protected_ncm.py .\\install_hud_update.py .\\SHA256SUMS .\\Connect-Jetson.ps1 .\\INSTALL_KO.md $destination
if ($LASTEXITCODE -ne 0) {{ throw 'Upload failed' }}
ssh -t -i $key -J "comma@$comma" $peer.Target
```

## 젯슨 SSH 셸: 무결성 및 실제 실행 환경 사전 검사

```sh
cd /opt/carrot-jetlink/updates/hud-{commit[:12]}
sha256sum -c SHA256SUMS
sudo python3 install_hud_update.py --bundle {BUNDLE} --sha256 {checksum} --check-only
```

모든 체크섬과 `NEXO_PREFLIGHT_OK`를 확인한 뒤에만 아래 명령을 실행하세요.
사전 검사가 실패하면 기존 서비스는 중단하지 않습니다. 오류를 해결하지 않고 검사를 우회하지 마세요.
원격 연결이 끊겨도 설치가 계속되도록 별도 systemd 작업으로 실행합니다.

```sh
sudo systemd-run --unit=nexo-hud-repair --collect --no-block --property=Type=oneshot --property=TimeoutStartSec=1200 --property=KillMode=control-group /usr/bin/python3 /opt/carrot-jetlink/updates/hud-{commit[:12]}/install_hud_update.py --bundle /opt/carrot-jetlink/updates/hud-{commit[:12]}/{BUNDLE} --sha256 {checksum} --confirmed-offroad
```

연결이 끊기면 위 PowerShell의 Connect-Jetson으로 다시 접속합니다. 상태를 확인하기 전에는 재실행하지 마세요.

```sh
systemctl status nexo-hud-repair.service --no-pager
cat /opt/carrot-jetlink/updates/nexo-install-status.json
cat /opt/carrot-jetlink/current/SOURCE_COMMIT
systemctl show carrot-jetlink.service carrot-jetlink-hud.service carrot-jetlink-xorg.service carrot-jetlink-boot-display.service -p ActiveState -p SubState -p Result -p ConditionResult -p MainPID
journalctl -u nexo-hud-repair -u carrot-jetlink-hud -u carrot-jetlink-xorg -b -n 150 --no-pager
```

정상 설치 상태는 `display_only_installed`, SOURCE_COMMIT은 `{commit}`입니다.
offroad에서는 HUD 렌더러가 입력을 기다리고 부팅/연결 대기 화면이 보일 수 있습니다.
TCP 정상, 프로세스 실행, 패널 쓰기 성공은 서로 다른 증거이며 실제 픽셀 표시를 보증하지 않습니다.
새 오류 분류를 콤마 UI에서도 보려면 콤마 역시 NEXO의 이 커밋 이상으로 정상 업데이트하세요.
전송 프로토콜과 모델 바이트는 변경하지 않았습니다.

## 롤백 / 중단된 설치 복구

완료된 설치를 직전 버전으로 되돌릴 때만:

```sh
sudo python3 /opt/carrot-jetlink/updater/nexo_protected.py rollback --confirmed-offroad
```

`rollback_failed` 또는 `restored_pending_services`이면 성공으로 간주하지 마세요.
복구 트랜잭션을 지우지 말고 위 journal을 보존하세요. 전원 재부팅 시 기존 boot updater가
이전 버전을 복원하고 별도 `nexo-rollback-verify` 작업이 서비스 복구를 확인합니다.
반복 실패 시 로그와 시작 조건을 확인해야 합니다. 기존 APP/SETUP 재설치나 USB 리셋은 하지 않습니다.
'''


def write_checksums(directory):
  files = sorted(path for path in directory.iterdir() if path.name != 'SHA256SUMS')
  sums = ''.join(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n' for path in files)
  # GNU sha256sum treats a Windows CR as part of the filename.
  (directory / 'SHA256SUMS').write_bytes(sums.encode('ascii'))


def package(root, vendor, output):
  output.parent.mkdir(parents=True, exist_ok=True)
  with tempfile.TemporaryDirectory(dir=output.parent, prefix='.hud-package-') as temporary:
    directory = Path(temporary)
    bundle = directory / BUNDLE
    build(root, vendor, bundle, compact=True)
    report = json.loads((directory / 'nexo-hud-repair.json').read_text(encoding='utf-8'))
    with tarfile.open(bundle, 'r:gz') as archive:
      for name in HELPERS:
        (directory / name).write_bytes(archive.extractfile('openpilot/tools/jetson/' + name).read())
    (directory / 'INSTALL_KO.md').write_text(instructions(report['source_commit'], report['bundle_sha256']), encoding='utf-8')
    write_checksums(directory)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as target:
      for path in sorted(directory.iterdir()):
        target.write(path, path.name)
  identity = hashlib.sha256(output.read_bytes()).hexdigest()
  output.with_suffix('.zip.sha256').write_bytes((identity + '  ' + output.name + '\n').encode('ascii'))
  print(json.dumps({'source_commit': report['source_commit'], 'zip': str(output), 'zip_sha256': identity,
                    'bundle_sha256': report['bundle_sha256'], 'physical_hud_verified': False}))


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--carrot', type=Path, required=True)
  parser.add_argument('output', type=Path)
  args = parser.parse_args()
  package(Path(__file__).resolve().parents[3], args.carrot.resolve(), args.output.resolve())


if __name__ == '__main__':
  main()
