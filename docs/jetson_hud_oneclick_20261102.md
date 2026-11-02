# 7000 Jetson HUD 보호형 자동 업데이트

2026-11-02. 기존 Windows → 콤마 → Jetson SSH/SCP 수동 배포를 콤마의 7000 관리 메뉴에서 실행하는 선택적 경로입니다. 실제 차량·Jetson 설치 또는 패널 픽셀 검증은 아직 수행하지 않았습니다.

## 사용자 동작

1. 콤마의 NEXO 버전을 최신 메인으로 업데이트하고, 차량을 주차한 뒤 **Offroad**로 전환합니다. Jetson 전원을 유지합니다.
2. `http://<콤마-IP>:7000/jetson`에서 SSH 대상을 등록합니다. 키 기반 SSH, known_hosts 검증과 기존 Jetson 보호형 설치 권한이 필수이며 비밀번호나 호스트키 검증을 우회하지 않습니다.
3. **최신 HUD 버전 확인**을 누릅니다. 확인 대상은 일반 Carrot 릴리스가 아니라 정확한 NEXO 메인 HEAD용 `jetson-hud-<40자리 커밋>` GitHub Release입니다.
4. 현재 Jetson 버전이 다를 때 **젯슨 HUD 업데이트 설치**를 누르고 정차/Offroad 확인창에 동의합니다.
5. 다운로드 → SHA256 검사 → SSH 전송 → Jetson 보호형 사전 검사 → 설치 → 결과 확인 단계가 표시됩니다.

GitHub Actions의 HUD CI가 성공한 메인 커밋만 공개 릴리스로 발행됩니다. CI가 실행 중이거나 발행 실패일 때는 설치할 수 없으며 임의의 이전 ZIP을 선택하지 않습니다. 콤마에 GitHub 인터넷 연결과 내려받을 저장 공간이 필요합니다. Jetson 자체 인터넷 접속은 필요하지 않습니다.

## 경계와 복구

- 신규 API `/api/jetson/hud-update/check` 및 `/start`는 POST + 동일 Origin + 기존 7000 CSRF 토큰을 요구합니다. `/status`는 읽기 전용입니다.
- 서버는 정차/Offroad 및 모드 상태를 확인하고 독립 설치 작업도 실제 Params를 다시 검사합니다. 진단/다운로드만으로 HUD 서비스를 중단하지 않습니다.
- 공식 NEXO SHA에 고정된 릴리스 ZIP과 `.sha256`, ZIP 내부 `SHA256SUMS`, 보호형 번들 소스 커밋·SHA256을 교차 확인한 후에만 고정 경로로 업로드합니다.
- 젯슨은 기존 `install_hud_update.py --check-only`에서 `NEXO_PREFLIGHT_OK`를 반환해야 설치를 시작할 수 있습니다. 기존 보호형 설치 서비스의 rollback/transaction 로직을 그대로 사용합니다.
- 기존 관리 버튼 `Jetson 업데이트 확인`은 여전히 Carrot 서명 매니페스트를 조회하는 읽기 전용 기능입니다. 새 버튼과 혼동하지 마십시오.
- 자동 설치 중 서비스가 끊기면 7000의 진행 기록과 `/opt/carrot-jetlink/updates/nexo-install-status.json`을 확인해야 합니다. **상태 미확인 시 설치 버튼을 반복해서 누르지 않습니다.**
- 별도의 차량 제어, DM, Panda, MED, 레이더, APP/SETUP 수정은 없습니다. 실제 HUD 출력 안정성·USB 복구는 정차 및 현장 시험이 필요합니다.

## 원격 사전 준비

기존 젯슨 SSH 대상이 7000 서버에서 **비밀번호 없이** 접속 가능해야 합니다. 호스트 키는 기존 SSH known_hosts에 있어야 하며 `sudo -n`으로 보호형 DATA 설치 준비와 사전 검사를 실행할 권한이 있어야 합니다. 해당 권한이 없다면 업데이트는 실패하도록 설계되었으며, 임의로 전체 root 권한을 부여하거나 SSH 보안을 끄지 마십시오.

버전 확인의 실패와 설치 실패를 구분합니다. `stage=completed`는 소프트웨어 설치 상태 증거일 뿐 실제 HUD 화면 표시의 증명은 아닙니다.