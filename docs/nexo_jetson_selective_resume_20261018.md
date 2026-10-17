기존 작업을 재사용한 NEXO Jetson 선별 반영 결과입니다. PC 코드·mock 검증이며 실물·실차 검증 완료를 의미하지 않습니다.

1. 사용한 작업 브랜치

feature/nexo-jetlink-v2-v3-observer에서 이어서 작업했습니다. 이전 폴더의 저장소를 현재 작업 공간에 로컬 복제했으며 이전 폴더는 수정하지 않았습니다.

2. 커밋과 비교 기준

재사용 커밋: 0793f7ef00d658e057465e2299fc204dfb153f1a
최신 NEXO: a6b5a756043d02f841a6dffff4e996aad2085d21
이전 carrot-wip 기준: f104b56c41a7f5353f75b0cf6ebd45824c4ea93e
이번 carrot-wip HEAD: c44d20f96d773bcd5cc83cc625c342ab2ef5f541
추가 작업 커밋은 이 보고서를 포함한 최종 Git 커밋이며 전체 SHA는 최종 응답에 기재합니다.

최신 NEXO가 기존 feature HEAD의 조상임을 확인했습니다. rebase·merge·cherry-pick은 필요하지 않았습니다. 초기 carrot fetch의 unresolved delta는 제한 시간 내 --refetch로 복구했습니다. 상속된 partial clone의 누락 tree에 따른 자동 GC 경고도 NEXO --refetch 후 해당 tree 조회 성공으로 확인했습니다. 비교 기준은 위 SHA로 고정했습니다.

3. 기존 작업 재사용 및 최신 7개 커밋 검토

기존 feature의 git status는 깨끗했습니다. v2/v3 호환·재연결·NV12 변환·관찰 전용 LeadBrakeLights·7000 진단을 재사용했습니다. 기존 대규모 테스트는 반복하지 않았습니다.

- e944a8c5: 신호/lead 제어 변경 — 제외했습니다.
- 5405f5a4: 신호 알림 변경 — 제외했습니다.
- 6da330c55fba4065a0b63a76d2ce19f29dce4f76: HUD 메모리 회수 — 수명 관리 아이디어를 비교하고 7번의 XPlus 전용 최소 보완만 적용했습니다.
- bd53684a: HUD 재시도 메모리 관측 문서 — 검토만 했습니다.
- e62fbd24: HUD 메모리 후속 관측 문서 — 검토만 했으며 당근의 실차 수치를 XPlus 결과로 사용하지 않았습니다.
- 0389e7a5: 중앙 신호등 선택 실험 — 제외했습니다.
- c44d20f9: 철회된 locationd CPU 실험 복구 기록 — 검토만 했습니다.

4. 수정 파일 목록

이번 재개 작업의 수정·추가 파일:

- docs/nexo_jetson_selective_port_20261016.md
- docs/nexo_jetson_selective_resume_20261018.md
- openpilot/selfdrive/carrot/server/features/jetson_pages.py
- openpilot/selfdrive/carrot/server/services/jetson.py
- openpilot/selfdrive/carrot/server/tests/test_jetlink_inference_status.py
- openpilot/selfdrive/modeld/jetlink/client.py
- openpilot/selfdrive/modeld/jetlink/compat.py
- openpilot/selfdrive/modeld/jetlink/transport.py
- openpilot/selfdrive/modeld/tests/test_jetlink_compat.py
- openpilot/selfdrive/modeld/tests/test_nexo_jetlink_recovery.py
- openpilot/tools/jetson/build_protected_bundle.py
- openpilot/tools/jetson/tests/test_hud_bundle.py

최신 NEXO 대비 기존 재사용분을 포함한 전체 변경 파일:

- docs/nexo_jetson_selective_port_20261016.md
- docs/nexo_jetson_selective_resume_20261018.md
- openpilot/common/params_keys.h
- openpilot/selfdrive/carrot/cluster/cluster_renderer.py
- openpilot/selfdrive/carrot/lead_brake_lights.py
- openpilot/selfdrive/carrot/server/features/jetson_pages.py
- openpilot/selfdrive/carrot/server/services/jetson.py
- openpilot/selfdrive/carrot/server/tests/test_jetlink_inference_status.py
- openpilot/selfdrive/carrot/tests/test_lead_brake_lights.py
- openpilot/selfdrive/carrot_settings.json
- openpilot/selfdrive/modeld/jetlink/client.py
- openpilot/selfdrive/modeld/jetlink/compat.py
- openpilot/selfdrive/modeld/jetlink/daemon.py
- openpilot/selfdrive/modeld/jetlink/protocol_v3.py
- openpilot/selfdrive/modeld/jetlink/transport.py
- openpilot/selfdrive/modeld/signal_tracking_shadow.py
- openpilot/selfdrive/modeld/tests/test_jetlink_compat.py
- openpilot/selfdrive/modeld/tests/test_nexo_jetlink_recovery.py
- openpilot/selfdrive/modeld/tests/test_signal_tracking_shadow.py
- openpilot/system/manager/process_config.py
- openpilot/tools/jetson/build_protected_bundle.py
- openpilot/tools/jetson/hud.py
- openpilot/tools/jetson/snapshot.py
- openpilot/tools/jetson/tests/test_hud_bundle.py
- openpilot/tools/jetson/tests/test_native_hud.py
- tools/signal_analysis/lead_brake_detector.py

5. v2/v3 호환

새 daemon은 v2를 먼저 시도합니다. HELLO 실패 시 기존 transport를 닫고 다음 새 transport에서 v3를 시도합니다. 같은 연결에서 protocol을 전환하지 않습니다. 성공한 버전은 유지하며 SHA·엔진 준비·추론 실패를 이유로 강제 변경하지 않습니다.

v2 protocol.py와 기본 framing을 보존했습니다. v3는 WANT_HIDDEN과 NEXO contract에서 계산한 scalar 크기를 사용합니다. NEXO driving_supercombo.onnx 및 SHA·shape·slice·frame_skip 검증을 유지했습니다. 모델 upload나 Cinque/Mountain Dew 교체는 없습니다. 실제 v3 TensorRT 수치 일치는 실물 검증이 필요합니다.

6. USB/Jetson 재연결

기존 HELLO 제한 시간·2/4/8/16/30초 backoff·local model 우선 시작과 fallback을 유지했습니다. 늦은 부팅/서버 지연에서는 v2→v3 새 연결 순서를, setup 실패에서는 protocol 유지와 재시도를 mock으로 확인했습니다. 새 연결 생성 전에 이전 연결 close가 완료되는지도 검사했습니다.

TCP 선택 시 USB setup을 실행하지 않습니다. ENGINE_REQ 전송에 최대 10초, STATE_REQ 전송에 호출자의 제한 시간을 추가해 읽지 않는 TCP peer로 인한 무한 전송 대기를 보완했습니다. HELLO 송수신은 각각 3초이며 엔진 응답/준비에는 기존 별도 제한 시간이 있습니다. 이는 각 단계의 제한이지 전체 왕복 시간은 아닙니다.

NexoJetsonUsb 충돌 방지와 stale jetlink/nexo-display만 정리하는 기존 setup을 유지했습니다. setup_gadget.sh와 network.py는 최신 NEXO와 동일합니다. AGNOS·adb·다른 network gadget을 강제로 해제하지 않습니다. NCM은 기존 offroad usb0 helper만 유지했고 당근 휴대전화 NCM/DHCP/방화벽 코드는 가져오지 않았습니다. NCM 실패 mock은 실물 NCM 검증을 의미하지 않습니다.

7. HUD memory 검토 및 적용

당근 6da330c5는 같은 프로세스에서 renderer를 재생성할 때 traceback/GC·native heap이 다음 초기화와 겹치는 문제를 다룹니다. XPlus tools/jetson/hud.py는 패널과 snapshot을 기다린 뒤 cluster.main()을 한 번 호출하며 실패 시 서비스가 프로세스를 재시작합니다. S3 discovery·renderer autorun·malloc_trim은 이식하지 않았습니다.

별도로 XPlus JetlinkFfsTransport에서 transport → watchdog → bound abort callback → transport 순환 참조를 확인했습니다. 실제 Python buffer/watchdog를 사용하되 장치 I/O 없이 검사한 결과, close 후에도 cyclic GC 전까지 weak reference가 살아 있는 것을 수정 전 실패로 재현했습니다.

해당 Jetlink subclass에만 최소 수정했습니다. reader/watchdog 종료와 자신의 UDC 해제를 모두 확인한 뒤 종료된 watchdog callback 참조와 수신 대기/재사용 buffer를 정리합니다. 종료가 불확실하면 UnsafeCleanupError를 유지하고 callback/buffer를 건드리지 않습니다. 기존 외부 HUD의 공용 FunctionFS 코드는 수정하지 않았습니다.

GC를 끈 상태에서 8회 생성/종료하여 이전 세션이 즉시 해제되고 반복 close도 안전한지 확인했습니다. 실제 RSS/PSS·native GPU heap 절감량이나 장시간 누수 해소를 입증하지는 않았습니다. 해당 수치와 thread/fd 수는 실물 관측이 필요합니다.

8. LeadBrakeLights

기본 OFF이며 OFF에서 camera/subscriber/detector 생성·영상 변환·결과 파일 polling이 없음을 확인했습니다. ON에서도 candidate/unknown과 control_permission=False만 기록하며 후보만 BRAKE?로 표시합니다. 미검출을 OFF로 확정하지 않으며 MED/SCC/longitudinal/braking/control 입력으로 연결하지 않았습니다.

기존 feature의 Jetson 배포 목록에 lead_brake_lights.py가 없어 renderer import가 실패할 수 있는 누락을 수정했습니다. 표시 reader 파일을 패키지에 포함하고 차량 확장 모듈 없이 OFF·candidate·만료 동작을 수행하는 회귀 검사를 추가했습니다. 전체 SD 패키지 생성이나 장비 설치는 하지 않았습니다.

9. 7000 진단

기존 feature의 연결/USB·TCP/protocol/SHA·contract/ready·active/추론 지연/온도/HUD TX/navigation TX/reconnect/fallback 진단을 유지했습니다. 이번에는 native TCP가 SSH로 표시되거나 HUD TX·Jetlink 단계가 누락되던 부분을 수정했습니다. TCP 동작을 USB 연결로 표시하지 않습니다.

과거 마지막 연결 실패와 현재 오류를 별도 JSON 필드/화면 항목으로 구분했습니다. TX 완료와 상대 HUD 표시 증거를 구분하고 만료된 값을 정상으로 확정하지 않습니다. JSON과 v2 USB/v3 TCP mock DOM을 확인했습니다. 실제 HTTP/브라우저·장비 상태 갱신은 미검증입니다.

10. 테스트 결과

중복 실행을 제외한 집중 테스트 총 147개가 통과했습니다.

- v2/v3·SHA mismatch·fresh stream·bounded write·USB memory와 daemon recovery/fallback: 59개 통과했습니다.
- HUD/inference 순서·단일 owner·navigation·worker 종료: 21개 통과했습니다.
- 7000 JSON/TCP·기존 offroad network helper mock: 30개 통과했습니다.
- LeadBrakeLights OFF/ON·stale/unknown·NV12 픽셀 동일성/출력 소유권: 36개 통과했습니다.
- Jetson HUD 배포 reader 의존성: 1개 통과했습니다.

메모리 종료 보호 검사 4개를 마지막으로 재실행하여 통과했으며 총계에 중복 가산하지 않았습니다. 메모리 테스트는 수정 전 실패 재현 후 수정하여 통과했습니다. 테스트 코드에서 발견한 Ruff E731도 수정했습니다.

Python 22개 파일 구문, settings JSON, git diff --check, 7000 JavaScript 구문·mock DOM 검사를 통과했습니다. 새 Ruff 진단은 0개이며 최신 NEXO에도 존재하는 기존 21개는 변경하지 않았습니다. NV12 microbenchmark와 이전 전체 393개 회귀는 반복하지 않았습니다. 이전 보고서의 기존 주차센서 timeout 실패를 이번 수정/통과 결과로 취급하지 않습니다.

11. 제한 시간과 스킵

Git 명령은 대체로 30초, fetch는 최대 90초, Ruff는 파일당 최대 15초, 테스트 그룹은 프로세스 전체 최대 90초로 실행했습니다. 테스트 그룹 전체 제한으로 개별 테스트도 120초를 넘길 수 없습니다. 이번 집중 테스트에서 timeout 종료는 없었습니다.

pytest --noconftest와 별도 설정으로 Linux native 장비 초기화 fixture와 병렬 자동 실행을 제외했습니다. 실제 USB/TCP/NCM 접속, HTTP socket 서버, foreground daemon, AGNOS 명령, 전체 SCons 빌드는 실행하지 않았습니다. 대기 테스트는 메모리 transport 또는 OS/시간 mock입니다. Linux cereal/msgq/Params/native renderer 전체 통합에는 Linux/AGNOS 환경이 필요합니다.

12. 실물·실차 검증 필요 항목

Comma4 선부팅 후 Jetson 지연 부팅, 긴 TensorRT 준비, USB 분리/재연결, Jetson 재부팅·전원 복구, 실제 NCM 실패와 TCP 복구가 필요합니다. AGNOS/adb/network gadget 동시 존재 시 소유권 보존도 확인해야 합니다.

실제 v2/v3 NEXO 모델 SHA/contract·연속 recurrent 출력·지연·온도·CPU 부하, HUD/navigation 동시 전송, 반복 재연결의 RSS/PSS·GPU heap·thread/fd 수를 확인해야 합니다. 주차 상태에서 local fallback 및 활성 추론 장애 시 안전한 modeld 재시작을 먼저 점검해야 합니다. LeadBrakeLights 야간/우천/차종별 오검출은 미검증입니다.

13. 보호 대상 변경 여부

최신 origin/NEXO 대비 steering·longitudinal·MED·SCC·radar·Panda·CAN·safety·Driver Monitoring 구현 경로에 변경이 없습니다. modeld.py·dmonitoringmodeld.py·Jetlink model.py/spec.py·driving_supercombo.onnx도 동일합니다. process_config.py에는 관찰 전용 프로세스/조건만, params_keys.h에는 기본 0인 LeadBrakeLights 키만 추가됐습니다. 기존 DM 설정/시선 처리 및 주차·후진/측면 카메라 로직은 변경하지 않았습니다.

14. merge/push 여부

NEXO 브랜치에 직접 작업하거나 merge/push하지 않았습니다. carrot 전체 merge/cherry-pick·feature 원격 push·PR 생성·차량 설치도 하지 않았습니다. 이번 결과는 로컬 feature 커밋입니다. 최종 git status는 커밋 후 확인합니다.
