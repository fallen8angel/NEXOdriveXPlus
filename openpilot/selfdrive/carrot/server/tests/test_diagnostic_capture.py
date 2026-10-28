import json
import pytest
from openpilot.tools.jetson import health
from openpilot.selfdrive.carrot.server.services.diagnostic_capture import receive_batch


def test_collector_reads_original_sockets_and_does_not_starve_states():
  class Socket:
    def __init__(self, values):
      self.values = iter(values)
    def receive(self, non_blocking):
      assert non_blocking
      return next(self.values, None)
  sockets = {'can': Socket([b'can'] * 1000), 'carState': Socket([b'car']),
             'controlsState': Socket([b'controls']), 'pandaStates': Socket([b'panda'])}
  batch = list(receive_batch(sockets, 4))
  assert [name for name, _ in batch] == ['can'] * 4 + ['carState', 'controlsState', 'pandaStates']
  assert len(list(receive_batch(sockets, 1000))) == 996


def test_remote_ages_do_not_compare_host_monotonic_clocks(tmp_path, monkeypatch):
  monkeypatch.setattr(health, 'RUNTIME', tmp_path)
  monkeypatch.setattr(health, 'source_revision', lambda: 'a' * 40)
  (tmp_path / 'ncm-display.json').write_text(json.dumps({'updated': 100, 'last_peer_rx_mono': 99.8,
    'last_hud_tx_mono': 99.9, 'peer': {'hud_rx_age': .1, 'hud_pipeline': {'last_panel_send_age_s': .2}}}))
  result = health.diagnostic_evidence(100)
  assert result['jetson_hud_rx_age_s'] == pytest.approx(.3)
  assert result['panel_write_age_s'] == pytest.approx(.4)
  assert health.diagnostic_evidence(103)['panel_write_age_s'] is None
