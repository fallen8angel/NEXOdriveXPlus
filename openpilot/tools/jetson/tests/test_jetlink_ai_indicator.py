"""Passive status and HUD regressions with simulated records/rendering only."""
import json
from types import SimpleNamespace

import pytest

from openpilot.tools.jetson import jetlink_ai_indicator as badge
from openpilot.tools.jetson import snapshot as hud_snapshot
from openpilot.selfdrive.modeld.jetlink.display import snapshot_packet


@pytest.fixture
def records(tmp_path, monkeypatch):
  paths = {}
  for name in ('MODEL_STATUS', 'LINK_STATUS', 'DISPLAY_STATUS', 'STATUS'):
    path = tmp_path / name
    monkeypatch.setattr(badge, name, path)
    paths[name] = path
  return paths


def write(records, name, **values):
  records[name].write_text(json.dumps({'updated': 100., **values}))


def live(records, **model):
  write(records, 'LINK_STATUS', state='ready', sha256='a' * 64, telemetry_updated=100., last_infer_monotonic=100.)
  write(records, 'MODEL_STATUS', **{
    'model': badge.MODEL_NAME, 'sha256': 'a' * 64, 'active': True, 'ready': True,
    'external_inference_verified': True, 'completed_inferences': 10,
    'last_inference_at_mono': 100., 'jetson_execution_us': 2930., 'modeld_roundtrip_ms': 8.1,
    **model,
  })


def minimum_hud(status=None):
  return {'version': 1, 'sent': 100., 'updated': 500., 'events': {}, 'params': {},
          'received': {}, 'mono': {}, 'valid': {}, 'alive': {}, 'cameras': {},
          'jetlink_ai': status or {'connected': True, 'engine_ready': True, 'inference_active': True,
                                   'inference_remaining_s': 1.25, 'inference_ms': 2.93, 'roundtrip_ms': 8.1}}


def test_completed_native_inference_has_independent_proof_and_timings(records):
  live(records)
  value = badge.status_on_comma(101.)
  assert value == {'connected': True, 'engine_ready': True, 'inference_active': True,
                   'inference_remaining_s': 1.25, 'inference_ms': 2.93, 'roundtrip_ms': 8.1}
  assert badge.verified_on_comma(101.) is True


def test_read_only_control_roundtrip_requires_a_fresh_completed_reply(records, monkeypatch):
  monkeypatch.setattr(badge._RATE, 'sample', lambda *a: pytest.fail('RTT read must not touch HUD inference FPS'))
  write(records, 'DISPLAY_STATUS', state='connected', hello_complete=True, last_peer_rx_mono=100., state_rtt_ms=12.5)
  assert badge.display_roundtrip_ms(100.1) == 12.5
  assert badge.display_roundtrip_ms(102.3) is None
  write(records, 'DISPLAY_STATUS', state='connected', hello_complete=True, last_peer_rx_mono=97., state_rtt_ms=12.5)
  assert badge.display_roundtrip_ms(100.1) is None


@pytest.mark.parametrize('changes', [
  {'active': False}, {'active': 1}, {'ready': False}, {'ready': 1}, {'model': 'experiment'},
  {'sha256': 'b' * 64}, {'external_inference_verified': False}, {'external_inference_verified': 1},
  {'completed_inferences': 0}, {'completed_inferences': True}, {'completed_inferences': '10'},
  {'last_inference_at_mono': None}, {'last_inference_at_mono': 98.75},
  {'last_inference_at_mono': 102.}, {'last_inference_at_mono': True},
  {'last_inference_at_mono': float('nan')}, {'last_inference_at_mono': 10 ** 400},
  {'updated': 98.75}, {'updated': 102.}, {'updated': 10 ** 400}, {'error': 'inference failed'},
])
def test_flags_or_refreshing_status_cannot_replace_completed_inference(records, changes):
  live(records, **changes)
  value = badge.status_on_comma(101.)
  assert value['connected'] is True and value['engine_ready'] is True
  assert value['inference_active'] is False
  assert 'inference_ms' not in value and 'roundtrip_ms' not in value


@pytest.mark.parametrize('changes', [
  {'state': 'retrying'}, {'state': 'stopped'}, {'error': 'disconnected'},
  {'updated': 98.75}, {'sha256': 'b' * 64}, {'sha256': ['bad']},
  {'last_infer_monotonic': None}, {'last_infer_monotonic': 98.75},
])
def test_link_loss_or_model_mismatch_removes_on(records, changes):
  live(records)
  write(records, 'LINK_STATUS', **{'state': 'ready', 'sha256': 'a' * 64, 'telemetry_updated': 100., 'last_infer_monotonic': 100., **changes})
  assert badge.status_on_comma(101.)['inference_active'] is False


def test_engine_and_gpu_benchmark_alone_never_prove_on(records):
  write(records, 'LINK_STATUS', state='ready', sha256='a' * 64, telemetry_updated=100.,
        engine_bytes=55_000_000, gpu_test='PASSED', gpu_average_ms=2.93)
  value = badge.status_on_comma(101.)
  assert value == {'connected': True, 'engine_ready': True, 'inference_active': False}
  assert badge.presentation(minimum_hud(value), 500.1)[0] == 'JETSON · AI 실험 대기'


def test_display_only_connection_and_rtt_are_separate_from_engine_and_inference(records):
  write(records, 'DISPLAY_STATUS', state='connected', hello_complete=True,
        last_peer_rx_mono=100., state_rtt_ms=4.2)
  value = badge.status_on_comma(101.)
  assert value == {'connected': True, 'engine_ready': False, 'inference_active': False, 'link_rtt_ms': 4.2}
  assert badge.presentation(minimum_hud(value), 500.1)[0] == 'JETSON 연결 · AI 대기'
  # Rewriting a connected file does not renew the last actual peer exchange.
  write(records, 'DISPLAY_STATUS', state='connected', hello_complete=True,
        last_peer_rx_mono=98.75, state_rtt_ms=4.2)
  assert badge.status_on_comma(101.) == {'connected': False, 'engine_ready': False, 'inference_active': False}


def test_companion_engine_ready_does_not_mean_inference(records):
  write(records, 'STATUS', usb_connected=True, last_receive_monotonic=100., model_ready=True)
  assert badge.status_on_comma(101.) == {'connected': True, 'engine_ready': True, 'inference_active': False}


@pytest.mark.parametrize('raw', ['{', '[]', 'x' * 65537])
def test_missing_or_corrupt_records_fail_closed(records, raw):
  for path in records.values():
    path.write_text(raw)
  assert badge.status_on_comma(101.) == {'connected': False, 'engine_ready': False, 'inference_active': False}


@pytest.mark.parametrize('metric', [True, -1, 10 ** 400, '2.93'])
def test_invalid_timings_are_omitted_without_hiding_live_status(records, metric):
  live(records, jetson_execution_us=metric, modeld_roundtrip_ms=metric)
  value = badge.status_on_comma(101.)
  assert value['inference_active'] is True
  assert 'inference_ms' not in value and 'roundtrip_ms' not in value


def test_snapshot_validation_drops_bad_optional_metadata_without_frame_failure():
  for item in ('yes', 1, None, [], {'connected': True}, {'connected': 1, 'engine_ready': True, 'inference_active': True}):
    value = minimum_hud()
    value['jetlink_ai'] = item
    assert 'jetlink_ai' not in hud_snapshot.validate_snapshot(value)
  for item in ('yes', 1, None, [], {'active': True}):
    value = minimum_hud()
    value['jetlink_inference_active'] = item
    assert 'jetlink_inference_active' not in hud_snapshot.validate_snapshot(value)


def test_ncm_snapshot_relay_keeps_status_and_does_not_change_vehicle_fields(records):
  live(records)
  value = minimum_hud(badge.status_on_comma(101.))
  value['gear_view'] = {'sample_ns': 99, 'gear': 'reverse', 'left': False,
                        'right': False, 'can_ok': True, 'park_enabled': True}
  forwarded = json.loads(snapshot_packet(json.dumps(value).encode(), 'peer-test'))
  assert forwarded['jetlink_ai'] == value['jetlink_ai']
  assert all(forwarded[key] == value[key] for key in ('gear_view', 'events', 'params', 'cameras'))


@pytest.mark.parametrize('value', [None, {}, {'jetlink_inference_active': True}, {'jetlink_inference_active': 1}])
def test_renderer_never_uses_legacy_boolean_as_live_proof(value):
  assert badge.presentation(value, 500.) is None


def test_renderer_expires_inference_even_while_hud_snapshots_keep_arriving(records):
  live(records)
  first = badge.status_on_comma(102.2)
  assert badge.presentation(minimum_hud(first), 500.01)[0] == 'JETSON AI ON'
  # Different host clock; remaining inference lease ends before the HUD lease.
  label, _, measurements = badge.presentation(minimum_hud(first), 500.1)
  assert label == 'JETSON · AI 실험 대기' and measurements == ''
  # The peer/status heartbeat advances but inference has stopped.
  write(records, 'LINK_STATUS', state='ready', sha256='a' * 64, telemetry_updated=102.3, last_infer_monotonic=100., updated=102.3)
  live_model = json.loads(records['MODEL_STATUS'].read_text())
  write(records, 'MODEL_STATUS', **{**live_model, 'updated': 102.3})
  assert badge.status_on_comma(102.3)['inference_active'] is False
  assert badge.presentation(minimum_hud(), 500.5) is None
  assert badge.presentation(minimum_hud(), 499.9) is None


@pytest.mark.parametrize('remaining', [None, True, 0, -1, 2.26, float('inf'), 10 ** 400])
def test_bad_inference_lease_falls_back_to_prepared_status(remaining):
  value = minimum_hud()
  value['jetlink_ai']['inference_remaining_s'] = remaining
  assert badge.presentation(value, 500.)[0] == 'JETSON · AI 실험 대기'


@pytest.mark.parametrize('gear', ['park', 'reverse', 'neutral', 'drive'])
def test_overlay_uses_monochrome_nvidia_mark_and_only_live_ai_shows_plus(monkeypatch, gear):
  events = []
  texture = SimpleNamespace(width=84, height=66)
  fake_rl = SimpleNamespace(
    rl_push_matrix=lambda: events.append('push'),
    rl_scalef=lambda *args: events.append(('scale', *args)),
    rl_pop_matrix=lambda: events.append('pop'),
    Rectangle=lambda *args: tuple(args),
    Vector2=lambda *args: tuple(args),
    draw_texture_pro=lambda *args: events.append(('image', args)),
    unload_texture=lambda *args: events.append(('unload', args)),
  )

  class Renderer:
    width, height = 1920, 480

    def render(self, state, signal_lights=None):
      events.append(('original', state, signal_lights))
      return 'original-result'

    def _rounded_rect(self, *args):
      events.append(('rounded', args))

    def _draw_text(self, *args, **kwargs):
      events.append(('text', args))

    def _load_icon_texture(self, *args):
      events.append(('load', args))
      return texture

    def close(self):
      events.append(('close',))

  renderer = SimpleNamespace(ClusterUiRenderer=Renderer, rl=fake_rl,
                             DESIGN_WIDTH=1920, DESIGN_HEIGHT=480)
  badge.install(renderer)
  badge.install(renderer)
  scene = Renderer()
  scene._jetson_gears = SimpleNamespace(mode=gear)
  monkeypatch.setattr(badge, 'ensure_logo', lambda: '/tmp/nvidia-mono-white-mark.png')
  monkeypatch.setattr(badge.time, 'monotonic', lambda: 500.1)
  current = [None]
  monkeypatch.setattr(hud_snapshot, 'read_snapshot', lambda: current[0])
  for value, expected in [
    (None, None), (minimum_hud(), '+'),
    (minimum_hud({'connected': True, 'engine_ready': True, 'inference_active': False}), ''),
    (minimum_hud({'connected': True, 'engine_ready': False, 'inference_active': False}), ''),
    (minimum_hud({'connected': False, 'engine_ready': False, 'inference_active': False}), None),
  ]:
    current[0] = value
    events.clear()
    assert scene.render(gear, (True, False)) == 'original-result'
    assert events[0] == ('original', gear, (True, False))
    assert sum(e[0] == 'original' for e in events) == 1
    text = [e[1][0] for e in events if e[0] == 'text']
    drawn = [e[1] for e in events if e[0] == 'image']
    if expected is not None:
      assert len(drawn) == 1 and events[-1] == 'pop'
      assert len([e for e in events if e[0] == 'rounded']) == 1
      assert text == (['+'] if expected == '+' else [])
      box = next(e[1] for e in events if e[0] == 'rounded')
      # Both the logo and its border are half their original dimensions,
      # but the logo retains the half-size chosen in the previous revision.
      _, _, drawn_width, drawn_height = drawn[0][2]
      assert box[2] == pytest.approx(205. * .5)
      assert box[3] == pytest.approx((48. if gear == 'park' else 64.) * .5)
      assert box[0] + box[2] == pytest.approx(renderer.DESIGN_WIDTH - 38.)
      assert drawn_height == pytest.approx(box[3] - 3.)
      assert drawn_width == pytest.approx((box[3] - 3.) * texture.width / texture.height)
      dest = drawn[0][2]
      assert box[0] < dest[0] and dest[0] + dest[2] <= box[0] + box[2]
      assert box[1] < dest[1] and dest[1] + dest[3] <= box[1] + box[3]
      if gear == 'park':
        assert box[1] + box[3] < 64  # Preserve the hydrogen title.
      else:
        assert box[0] >= renderer.DESIGN_WIDTH * .5
      assert all('JETSON' not in str(label) and 'RTT' not in str(label) for label in text)
    else:
      assert events == [('original', gear, (True, False))]
  events.clear()
  scene.close()
  assert sum(e[0] == 'unload' for e in events) == 1
  assert events[-1] == ('close',)


def test_logo_symbol_requires_connected_status_and_fresh_completed_native_ai():
  assert badge.status_mark(None, now=500.) is None
  assert badge.status_mark(minimum_hud({'connected': False, 'engine_ready': False, 'inference_active': False}), now=500.1) is None
  assert badge.status_mark(minimum_hud({'connected': True, 'engine_ready': False, 'inference_active': False}), now=500.1) == ''
  assert badge.status_mark(minimum_hud(), now=500.1) == '+'
  assert badge.status_mark(minimum_hud(), now=501.3) is None
  assert badge.status_mark(minimum_hud({'connected': True, 'engine_ready': True, 'inference_active': True,
                                       'inference_remaining_s': 0.01}), now=500.1) == ''



def test_fps_uses_completed_frame_delta_not_inverse_latency(records, monkeypatch):
  monkeypatch.setattr(badge, '_RATE', badge.InferenceRate())
  live(records)
  assert 'fps' not in badge.status_on_comma(100.1)
  live(records, updated=101., last_inference_at_mono=101., completed_inferences=30)
  write(records, 'LINK_STATUS', state='ready', sha256='a'*64, updated=101.,
        telemetry_updated=101., last_infer_monotonic=101.)
  value = badge.status_on_comma(101.1)
  assert value['fps'] == 20.0 and value['inference_ms'] == 2.93
  assert '20.0 FPS' in badge.presentation(minimum_hud(value), 500.1)[2]
  assert badge.status_on_comma(103.25)['inference_active'] is False
  assert 'fps' not in badge.status_on_comma(103.25)


def test_fps_resets_on_counter_or_model_restart():
  rate = badge.InferenceRate()
  assert rate.sample({'sha256': 'a', 'completed_inferences': 10, 'last_inference_at_mono': 10.}, True) is None
  assert rate.sample({'sha256': 'a', 'completed_inferences': 30, 'last_inference_at_mono': 11.}, True) == 20.
  assert rate.sample({'sha256': 'a', 'completed_inferences': 1, 'last_inference_at_mono': 12.}, True) is None
  assert rate.sample({}, False) is None
