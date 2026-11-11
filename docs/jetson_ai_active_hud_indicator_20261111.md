# 외부 HUD 젯슨 AI 추론 작동 표시 (2026-11-11)

이 변경은 젯슨에 연결되었다는 이유만으로 AI가 작동한다고 표시하지 않습니다.
콤마4의 `JoiningModel.run()`에서 실제 외부 `client.infer()` 결과를 얻은 후 기록한
`/dev/shm/nexo-jetlink-model.json`의 **active=true + ready=true**가 모두 확인되며
기록이 **2.25초 이내**일 때에만 외부 HUD의 우측 상단에 녹색 `JETSON AI ON` 배지를 표시합니다.

콤마4의 `SnapshotBuilder`가 읽기 전용 모델 상태를 확인하여 기존 HUD JSON에
`jetlink_inference_active`이라는 선택적 boolean 필드로 보냅니다.
젯슨 HUD는 이미 적용 중인 **0.5초 snapshot 신선도** 정책을 통과한 경우에만 표시합니다.
연결 준비, 엔진 준비, USB 연결, AI 기능 활성화 설정만으로는 배지가 나타나지 않습니다.
모델 추론이 중지되거나 상태 파일이 오래되면 배지는 사라집니다.

이 표시는 녹색 **JETSON AI ON**이라는 작은 배지입니다.
기존 속도·방향지시등·기어·주차 카메라·네비게이션·실험 모드 아이콘을 수정하지 않습니다.
활성 상태가 아닌 경우에는 배지를 아예 표시하지 않습니다.

구현 경로:

- `openpilot/tools/jetson/jetlink_ai_indicator.py`: 콤마4의 읽기 전용 증거, 젯슨 렌더러에 배지 설치
- `openpilot/tools/jetson/snapshot.py`: 활성 상태만 안전한 선택 필드로 스냅샷 전달
- `openpilot/tools/jetson/hud.py`: 기존 젯슨 HUD 렌더러에 배지 설치
- `openpilot/tools/jetson/tests/test_jetlink_ai_indicator.py`: 상태·만료·NCM 스냅샷 및 렌더러 회귀 시험

실제 외부 HUD에 표시하려면 콤마4의 엑스플러스와 젯슨의
**검증된 보호형 HUD 번들 모두 업데이트**되어야 합니다.
GitHub 코드의 메인 브랜치 반영만으로 실차에 자동 반영되지 않습니다.

현재 실제 젯슨 TensorRT/모델 추론 성공은 확인되지 않았습니다.
따라서 당장 배지가 나타나지 않더라도 코드 오류라고 단정하면 안 됩니다.
먼저 모델 활성 상태를 읽기 전용으로 확인해야 합니다.

콤마4에서 (SSH):

~~~sh
cat /dev/shm/nexo-jetlink-model.json
bash /data/openpilot/openpilot/selfdrive/modeld/jetlink/nexo_jetlink_ctl.sh status
~~~

위 보고서에서 `active=true`와 `ready=true`가 실제로 갱신되는지,
젯슨 HUD의 입력이 0.5초 이내로 수신되는지, 패널에 배지가 나타나는지를
**정차 상태에서 실기 검증**해야 합니다.
