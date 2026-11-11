# 젯슨 AI·HUD 성능 통합진단 (2026-11-11)

엑스플러스 7000 서버의 기존 **젯슨 통합진단 ZIP 다운로드** 버튼을 누르면
콤마4·젯슨의 상태 및 영상/HUD 추적과 함께 아래 성능 자료를 읽기 전용으로 저장합니다.
원격 접근이 안 되는 경우에도 콤마4 측 자료와 실패 사유가 ZIP에 남습니다.

## 이번에 추가한 관측 자료

- `04_comma_ai_performance.json/txt` — 콤마4 modeld의 **실제 활성 상태**와
  마지막 완료된 외부 추론 왕복시간(ms), 젯슨 응답의 실행/준비/합계(us),
  모드 상태 및 응답 신선도. 엔진 준비만으로 AI 추론 성공이라고 판단하지 않습니다.
- `remote/10_ai_hud_performance.json` — 젯슨에서 SSH 표준입력으로 실행하는 **일회성**
  읽기 전용 표본. CPU 전체/코어 사용률, GPU 현재/최소/최대 클럭과 드라이버가
  제공하는 부하, EMC 클럭, `nvpmodel -q` 전력 모드,
  MemAvailable/Swap, 실제 서비스의 RSS/PSS/Private Dirty, CPU 스레드 실행시간,
  실행 가능 상태에서 기다린 runqueue 대기시간,
  HUD 프레임 입력/인코더 만료/패널 송신 간격.
- `05_ai_hud_performance_summary.json/txt` — 최신 데이터만 바탕으로 분석 대기·엔진 부재,
  GPU 수치 미지원·스케줄러 통계 미지원·HUD GC 미계측을 구분합니다.
  실제 물리 패널을 확인하거나 GPU·메모리 병목을 확정하는 판정은 제공하지 않습니다.
- `remote/10_ai_hud_performance.json`의 `hud_python_gc` — 업데이트된 젯슨 HUD
  렌더러에 별도 **5초 간격**의 Python GC 관측 스레드가 있는 경우에만
  세대별 수집 횟수와 실제 GC 일시정지 시간(ms)을 저장합니다.

**중요:** GPU 드라이버 sysfs가 없으면 값은 없다고 명시합니다.
기록이 없는 경우 CPU/GPU/GC 값을 0으로 채우거나 '정상'이라고 판단하지 않습니다.
Python GC 관측은 **젯슨 HUD Python 렌더러 프로세스**만 대상으로 합니다.
다른 Python 서비스·TensorRT·CUDA의 실행 중단을 측정하지는 않습니다.
스레드 대기시간 역시 약 0.35초의 표본이라 과거 1초 미만 HUD 깜박임의 원인을
확정할 수 없습니다.

## 설치와 사용

1. 콤마4에서 `NEXO` 브랜치 업데이트 후 7000 서버의 **젯슨 통합진단 ZIP 다운로드** 사용.
2. 기존에 설정된 젯슨 SSH 대상 및 비밀번호 없는 SSH가 필요합니다.
   기능은 명령어를 SSH stdin으로 보내므로 젯슨에 새 진단 명령어를 설치하지 않아도
   **일회성 CPU/GPU/메모리/스케줄러 상태**는 확인 가능합니다.
3. HUD Python GC 자료까지 수집하려면 **새 보호형 HUD 패키지를 젯슨에 업데이트**해야 합니다.
   패키지는 메인 브랜치 릴리스에서 검증된 경우에만 설치하십시오.
4. 수집된 ZIP의 `05_ai_hud_performance_summary.txt`를 먼저 보고
   세부 비교가 필요하면 `04_comma_ai_performance.json`,
   `remote/10_ai_hud_performance.json`, `remote/09_hud_trace.json` 및
   `remote/06_model.json`을 함께 확인하십시오.

읽기 전용 수집은 AI 활성화, 엔진 생성, 서비스 재시작, GPU 최대 성능 모드 전환,
SSH 키 저장, USB 네트워크 변경, GC 강제 실행, 차량 제어를 수행하지 않습니다.
실차 배포 이후 화면/업데이트 작동 여부는 별도 확인이 필요합니다.
