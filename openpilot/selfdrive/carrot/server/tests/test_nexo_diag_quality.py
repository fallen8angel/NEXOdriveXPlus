"""Purely passive diagnostic summaries; tests never access a vehicle."""
from openpilot.selfdrive.carrot.server.features.tools.nexo_diag_quality import interpretation
from openpilot.selfdrive.carrot.server.services.jetson_diagnostics import connection_evidence


def test_parked_report_is_not_misclassified_as_fault():
  raw = (
    "[2] Panda fault · 안전상태\\n현재 fault: 없음\\n8초 관측 fault: 없음\\n"
    "[15] AI 방식 비교 자동판정\\nOFF 관측=True | MED/READY 관측=False | SPEED_CONTROL 관측=False\\n"
    "[17] 방향지시등 · 콤마 표시 진단\\n0.10s carState left=True right=True\\n"
    "[19] Panda TX 차단 상세 · source192 상관관계\\n"
    "1.41s safetyTxBlocked 0->21 delta=+21 | reason=not_exposed_by_pandaStates\\n"
    "[20] MODE · RES · SET · GAP · CANCEL DBC 해석\\nCLU11 버튼 변화 관측 없음\\n"
    "[27] 전·후방 주차센서 진단\\n[현재 신호로 상태 판정 불가] 후보 메시지 고정\\n"
    "[31] Jetson Jetlink 진단\\nJetlink: enabled=False | daemon=False\\n"
  ).replace("\\n", "\n")
  value = interpretation(raw)
  assert "fault는 없음" in value
  assert "증가한 안전 차단" in value
  assert "MED 고장" in value and "판정할 수 없습니다" in value
  assert "비상등 후보" in value
  assert "주차센서" in value and "확정할 수 없습니다" in value
  assert "외부 추론 OFF" in value


def test_independent_blinkers_are_recognized_separately():
  value = interpretation("[17] blinker\\ncarState left=True right=False\\ncarState left=False right=True".replace("\\n", "\n"))
  assert "좌·우 독립 ON" in value


def test_missing_safety_data_is_unknown_not_safe():
  value = interpretation("[15] OFF 관측=True")
  assert "상세 차단 카운터 자료가 없어" in value
  assert "fault가 없음을 확인하지 못했습니다" in value


def test_jetson_usb_evidence_does_not_promote_carrier_to_hello():
  result = {'connected': False, 'native_link': {'state': 'unavailable'}, 'status': {}}
  local = {'enabled': False, 'usb_carrier': True, 'usb_interface': 'usb0'}
  usb = {'udc_bound': False, 'ncm_interface': None, 'ncm_carrier': None}
  observed = connection_evidence(result, local, usb)
  assert observed['level'] == 'INTERFACE_CARRIER_ONLY'
  assert observed['carrier_not_ncm_proof'] is True
  assert observed['ncm_hello_verified'] is False
  assert observed['inference_enabled'] is False


def test_ncm_route_does_not_prove_peer_server_and_hello_does():
  usb = {'udc_bound': True, 'ncm_interface': 'usb0', 'ncm_interfaces': ['usb0'],
         'ncm_admin_up': True, 'ncm_carrier': True, 'comma_ipv4': '192.168.100.127',
         'peer_route_via_usb': True}
  local = {'enabled': False, 'usb_carrier': True, 'usb_interface': 'usb0'}
  result = {'connected': False, 'native_link': {'state': 'unavailable'}, 'status': {}}
  assert connection_evidence(result, local, usb)['level'] == 'NCM_ROUTE_AND_CARRIER'
  result.update(connected=True, native_link={'state': 'ready', 'transport': 'ncm-tcp'})
  assert connection_evidence(result, local, usb)['level'] == 'NCM_HELLO_CONFIRMED'
