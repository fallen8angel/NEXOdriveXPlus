"""Never mistake NEXO fuel-cell hydrogen range for unrelated battery SOC."""
from openpilot.tools.jetson.snapshot import nexo_clu13_candidate, nexo_hydrogen_observation


def test_generic_clu13_step_is_observation_only_not_percent():
  # CF_Clu_FuelDispLvl: start bit 18, length 5, little endian.
  raw = bytes((0, 0, (17 << 2), 0, 0, 0, 0, 0))
  assert nexo_clu13_candidate(raw) == 17
  assert nexo_clu13_candidate(b'') is None
  result = nexo_hydrogen_observation(0.0, True, 100.0, 100_000_000_000,
                                     17, 100_000_000_000)
  assert result['car_state_gauge_valid'] is False
  assert result['car_state_percent'] is None
  assert result['clu13_observed'] is True
  assert result['clu13_fuel_display_step_unverified'] == 17
  assert result['clu13_mapping_confirmed_for_nexo'] is False


def test_fresh_carstate_fuel_gauge_is_passed_through_unchanged():
  report = nexo_hydrogen_observation(.67, True, 100.1, 100_000_000_000,
                                    None, 0)
  assert report['car_state_gauge_valid'] is True
  assert report['car_state_percent'] == 67.0
  assert report['clu13_observed'] is False
  for missing in (float('nan'), float('inf'), -1., 1.2, 0., True, None):
    result = nexo_hydrogen_observation(missing, True, 100.1,
                                       100_000_000_000, None, 0)
    assert result['car_state_percent'] is None


def test_older_can_or_carstate_samples_do_not_become_current_fuel():
  stale = nexo_hydrogen_observation(.72, True, 102., 100_000_000_000,
                                   20, 100_000_000_000)
  assert stale['car_state_fresh'] is False
  assert stale['car_state_percent'] is None
  assert stale['clu13_observed'] is False
  assert stale['clu13_fuel_display_step_unverified'] is None
  assert nexo_hydrogen_observation(.5, False, 100., 100_000_000_000,
                                  10, 100_000_000_000)['car_state_percent'] is None


def test_zip_uses_only_fresh_receiver_evidence(tmp_path, monkeypatch):
  import json
  from pathlib import Path
  from openpilot.tools.jetson import manage
  snap = tmp_path / 'hud.json'
  monkeypatch.setattr(manage, 'Path', lambda name: tmp_path / Path(name).name)
  def read():
    return manage.hud_receipt(100, include_hydrogen=True)['hydrogen_source']
  assert read()['state'] == 'snapshot_unavailable'
  value = {'version': 1, 'updated': 99.1}
  snap.write_text(json.dumps(value))
  assert read()['state'] == 'snapshot_not_fresh'
  value['updated'] = 99.9
  snap.write_text(json.dumps(value))
  assert read()['state'] == 'fresh_snapshot_without_probe'
  value['hydrogen_probe'] = nexo_hydrogen_observation(0, True, 100, 100_000_000_000, 12, 100_000_000_000, 83, 100_000_000_000, 4)
  value['hydrogen_probe']['extra'] = 'private text'
  snap.write_text(json.dumps(value))
  result = read()
  assert result['state'] == 'observed' and result['receiver_file'] == 'hud.json'
  assert result['probe']['can_4e2_bus1_byte4_raw_unverified'] == 83
  assert result['probe']['car_state_percent'] is None and 'extra' not in result['probe']
  value['hydrogen_probe']['can_4e2_mapping_confirmed_for_nexo'] = True
  snap.write_text(json.dumps(value))
  assert read()['state'] == 'invalid_probe'
