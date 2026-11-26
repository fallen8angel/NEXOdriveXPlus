"""Read-only NEXO fuel diagnostics must not invent a hydrogen percentage."""
from types import SimpleNamespace

from openpilot.selfdrive.carrot.server.features.tools.nexo_can_diag import PontusHydrogenProbe, hydrogen_probe_lines


def report(*, nexo=True, gauge=0.0, valid=True, fresh=True, raw=None):
  state = SimpleNamespace(fuelGauge=gauge)
  lines = hydrogen_probe_lines(nexo, state, valid, fresh, len(raw or []), raw or [])
  return "\n".join(lines)


def test_valid_generic_gauge_is_only_an_unverified_candidate():
  text = report(gauge=0.86)
  assert "비확정 환산 후보=86.0%" in text
  assert "수소 잔량(%) 확정값: 미확인" in text
  assert "주행 가능 거리(km): 미확인" in text
  assert "HUD 값으로 사용 금지" in text


def test_zero_missing_and_stale_gauge_never_make_a_hydrogen_percent():
  for kwargs in ({"gauge": 0.0}, {"gauge": None},
                 {"gauge": 0.86, "fresh": False}, {"gauge": 0.86, "valid": False},
                 {"gauge": float("nan")}, {"gauge": 1.2},
                 {"gauge": 0.86, "nexo": False}):
    text = report(**kwargs)
    assert "비확정 환산 후보=" not in text
    assert "수소 잔량(%) 확정값: 미확인" in text


def test_clu13_candidate_is_5_bit_step_not_percentage():
  text = report(raw=["80AC00ED05000040", "0000440000000000"])
  assert "수신=2회" in text
  assert "STEP=0/31" in text
  assert "STEP=17/31" in text
  assert "수소 잔량 % 아님" in text
  assert "넥쏘 CLU13 신호와 수소 백분율의 대응 관계는 미검증" in text


def test_diagnostic_without_car_state_is_still_complete():
  lines = hydrogen_probe_lines(True, None, False, False, 0, [])
  text = "\n".join(lines)
  assert "원본=None" in text
  assert "수소 잔량(%) 확정값: 미확인" in text
  assert "[추가 확인 필요]" in text


def test_bus1_4e2_can_candidate_is_diagnostic_only():
  from collections import Counter
  report = hydrogen_probe_lines(True, None, True, True, 0, [], 200, Counter({83: 200}))
  joined = '\n'.join(report)
  assert '0x4E2 byte4 원시값=83' in joined
  assert '수소 잔량(%) 확정값: 미확인' in joined
  assert '계기판 숫자와 일치해도 우연일 수 있습니다' in joined


def test_pontus_probe_observes_candidates_on_each_bus():
  probe = PontusHydrogenProbe()
  probe.observe(1, 0x4E2, bytes([0, 0, 0, 0, 83, 0, 0, 0]))
  probe.observe(2, 0x26F, bytes([0, 82, 0, 0, 0, 0, 0, 0]))
  probe.observe(0, 0x50C, bytes([0, 0, 68, 0, 0, 0, 0, 0]))
  probe.observe(1, 0x4E2, b"")
  probe.observe(129, 0x4E2, bytes(8))
  text = "\n".join(probe.report_lines())
  assert "CAN source 1 0x4E2 byte4 원시값=83:1회" in text
  assert "CAN source 2 0x26F byte1 원시값=82:1회" in text
  assert "CAN source 0 0x50C byte2 원시값=68:1회" in text
  assert "source 129" not in text
  assert "확정 수소 %: 미확인" in text
  assert "HUD 반영 없음" in text


def test_pontus_probe_diagnostic_addresses_are_only_observations():
  probe = PontusHydrogenProbe()
  probe.observe(0, 0x7DF, bytes(8))
  probe.observe(0, 0x7E8, bytes(8))
  probe.observe(2, 0x18DAF110, bytes(8))
  probe.observe(2, 0x123, bytes(8))
  text = "\n".join(probe.report_lines())
  assert "0x7DF: 1회" in text
  assert "0x7E8: 1회" in text
  assert "0x18DAF110: 1회" in text
  assert "0x123" not in text
  assert "수소 응답이라고 확정할 수 없습니다" in text
  assert "CAN/UDS 송신 없음" in text


def test_pontus_probe_empty_and_bounded():
  probe = PontusHydrogenProbe()
  assert "0x4E2 byte4: 미관측" in "\n".join(probe.report_lines())
  for addr in range(0x18DA0000, 0x18DA0080):
    probe.observe(0, addr, bytes(8))
  assert len(probe.diagnostic_ids) == 64
