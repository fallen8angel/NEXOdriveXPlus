"""Read-only Jetlink AI HUD indicator regression tests.

All statuses and rendering calls are simulated. These tests do not prove
actual Jetson TensorRT engine, model inference, or TURZX panel output.
"""
import json
from types import SimpleNamespace

import pytest

from openpilot.tools.jetson import jetlink_ai_indicator as badge
from openpilot.tools.jetson import snapshot as hud_snapshot
from openpilot.selfdrive.modeld.jetlink.display import snapshot_packet


@pytest.mark.parametrize('flags,visible', [
  ({'active': True, 'ready': True}, True),
  ({'active': False, 'ready': True}, False),
  ({'active': True, 'ready': False}, False),
  ({'active': 1, 'ready': True}, False),
  ({'active': True, 'ready': 1}, False),
])
def test_only_live_successful_external_inference_is_advertised(monkeypatch, flags, visible):
  record = {'updated': 100.0, 'model': badge.MODEL_NAME, **flags}
  observed = []

  def reader(path, seconds, now=None, limit=None):
    observed.append((path, seconds, now, limit))
    return record

  monkeypatch.setattr(badge, 'read_fresh', reader)
  assert badge.verified_on_comma(now=101.0) is visible
  assert observed[0][1] == badge.MODEL_LEASE_S
  assert observed[0][2] == 101.0


@pytest.mark.parametrize('record', [
  None,
  {},
  {'active': True, 'ready': True, 'model': 'other model'},
  {'active': 'true', 'ready': True, 'model': 'other model'},
])
def test_missing_stale_or_wrong_model_never_advertised(monkeypatch, record):
  monkeypatch.setattr(badge, 'read_fresh', lambda *a, **k: record)
  assert badge.verified_on_comma() is False


def minimum_hud(active=True):
  return {'version': 1, 'sent': 100., 'events': {}, 'params': {},
          'received': {}, 'mono': {}, 'valid': {}, 'alive': {}, 'cameras': {},
          'jetlink_inference_active': active}


def test_snapshot_validation_rejects_invalid_optional_status_without_frame_failure():
  for item in ('yes', 1, None, [], {'active': True}):
    value = minimum_hud()
    value['jetlink_inference_active'] = item
    assert 'jetlink_inference_active' not in hud_snapshot.validate_snapshot(value)
  assert hud_snapshot.validate_snapshot(minimum_hud())['jetlink_inference_active'] is True


def test_ncm_snapshot_relay_keeps_active_flag():
  raw = json.dumps(minimum_hud()).encode()
  forwarded = json.loads(snapshot_packet(raw, 'peer-test'))
  assert forwarded['jetlink_inference_active'] is True


@pytest.mark.parametrize('value,expected', [
  (None, False),
  ({}, False),
  ({'jetlink_inference_active': False}, False),
  ({'jetlink_inference_active': 'true'}, False),
  ({'jetlink_inference_active': 1}, False),
  ({'jetlink_inference_active': True}, True),
])
def test_renderer_requires_explicit_boolean(value, expected):
  assert badge.visible_from_snapshot(value) is expected


def test_renderer_draws_indicator_only_on_verified_fresh_snapshot(monkeypatch):
  events = []
  fake_rl = SimpleNamespace(
    rl_push_matrix=lambda: events.append('push'),
    rl_scalef=lambda *args: events.append(('scale', *args)),
    rl_pop_matrix=lambda: events.append('pop'),
  )

  class Renderer:
    width, height = 1920, 1080

    def _draw_hud(self, state, signal_lights=None):
      events.append('original')

    def _rounded_rect(self, *args):
      events.append(('rounded', args))

    def _draw_text(self, *args):
      events.append(('text', args))

  renderer = SimpleNamespace(ClusterUiRenderer=Renderer, rl=fake_rl,
                             DESIGN_WIDTH=1920, DESIGN_HEIGHT=1080)
  badge.install(renderer)
  badge.install(renderer)  # Idempotent; no duplicate overlays.
  scene = Renderer()

  monkeypatch.setattr(hud_snapshot, 'read_snapshot', lambda: None)
  scene._draw_hud(None)
  assert events == ['original']

  events.clear()
  monkeypatch.setattr(hud_snapshot, 'read_snapshot', lambda: minimum_hud(False))
  scene._draw_hud(None)
  assert events == ['original']

  events.clear()
  monkeypatch.setattr(hud_snapshot, 'read_snapshot', lambda: minimum_hud(True))
  scene._draw_hud(None)
  assert events[0] == 'original'
  assert 'push' in events
  assert ('scale', 1.0, 1.0, 1.0) in events
  assert len([e for e in events if isinstance(e, tuple) and e[0] == 'text']) == 1
  assert 'JETSON AI ON' in events[-2][1][0]
  assert events[-1] == 'pop'
