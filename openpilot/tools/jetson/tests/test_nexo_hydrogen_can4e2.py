"""Keep an observed 0x4E2 CAN byte distinct from a proven NEXO hydrogen percentage."""
from types import SimpleNamespace

from openpilot.tools.jetson.snapshot import (
  nexo_4e2_candidate, nexo_hydrogen_observation, validate_snapshot,
)
from openpilot.tools.jetson.gear_view import park_summary, unverified_hydrogen_can_preview


def observed(value=83, stamp=100_000_000_000, streak=3, now=100.1, car_valid=True):
  return nexo_hydrogen_observation(
    0.0, car_valid, now, 100_000_000_000, None, 0,
    value, stamp, streak,
  )


def test_raw_can_4e2_byte4_is_read_only_not_confirmed_percent():
  assert nexo_4e2_candidate(bytes.fromhex('1807180953000000')) == 83
  assert nexo_4e2_candidate(bytes.fromhex('18071809530000')) is None
  assert nexo_4e2_candidate(bytes.fromhex('18071809FF000000')) is None
  assert nexo_4e2_candidate(None) is None
  candidate = observed()
  assert candidate['can_4e2_bus1_byte4_raw_unverified'] == 83
  assert candidate['can_4e2_bus1_observed'] is True
  assert candidate['can_4e2_mapping_confirmed_for_nexo'] is False
  assert candidate['car_state_percent'] is None
  assert unverified_hydrogen_can_preview({'hydrogen_probe': candidate}, car_fresh=True) == 83


def test_missing_stale_or_unstable_candidate_is_hidden():
  for probe in (
    observed(streak=2),
    observed(value=101),
    observed(stamp=98_000_000_000),
    observed(now=101.0),
    observed(car_valid=False),
  ):
    assert probe['can_4e2_bus1_observed'] is False
    assert probe['can_4e2_bus1_byte4_raw_unverified'] is None
    assert unverified_hydrogen_can_preview({'hydrogen_probe': probe}, car_fresh=True) is None
  assert unverified_hydrogen_can_preview({'hydrogen_probe': observed()}, car_fresh=False) is None
  assert unverified_hydrogen_can_preview(None, car_fresh=True) is None


def test_zero_generic_gauge_never_displays_zero_percent():
  state = SimpleNamespace(fuel_gauge=0.0, energy_gauge_label='fuel')
  assert park_summary(state, fresh=True)['hydrogen'] == '--'


def test_hud_snapshot_validation_checks_candidate_provenance():
  probe = observed()
  source = {
    'version': 1, 'sent': 100.1, 'events': {}, 'params': {},
    'mono': {}, 'received': {}, 'valid': {}, 'alive': {}, 'cameras': {},
    'hydrogen_probe': probe,
  }
  assert validate_snapshot(dict(source))['hydrogen_probe']['can_4e2_bus1_byte4_raw_unverified'] == 83
  forged = dict(source)
  forged['hydrogen_probe'] = dict(probe, can_4e2_mapping_confirmed_for_nexo=True)
  assert 'hydrogen_probe' not in validate_snapshot(forged)
