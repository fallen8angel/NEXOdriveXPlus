"""Read-only NEXO fuel diagnostics must not invent a hydrogen percentage."""
from types import SimpleNamespace

from openpilot.selfdrive.carrot.server.features.tools.nexo_can_diag import hydrogen_probe_lines


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
