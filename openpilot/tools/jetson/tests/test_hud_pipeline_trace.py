"""Hardware-free tests for read-only HUD pipeline diagnostics."""
import json

from openpilot.tools.jetson.hud_pipeline_trace import HudPipelineTrace


def test_blackout_trace_distinguishes_snapshot_loss_encoder_drop_and_panel_gap():
  trace = HudPipelineTrace(max_events=8, panel_gap_threshold_s=.4)
  trace.input('vehicle', age_s=.1, now=100.)
  trace.panel(now=100.01)
  trace.encoded(expired=False, mode='vehicle', now=100.02)
  trace.input('waiting_data', reason='encoder_lease_short', age_s=.43, now=100.1)
  trace.encoded(expired=True, mode='vehicle', lease_remaining_s=-.03, now=100.2)
  trace.input('vehicle', age_s=.05, now=100.3)
  trace.panel(now=100.65)
  value = trace.snapshot(now=100.7)
  assert value['input_mode'] == 'vehicle'
  assert value['input_reason'] is None
  assert value['encoder_sent_count'] == 1
  assert value['encoder_expired_count'] == 1
  assert value['panel_gap_count'] == 1
  assert abs(value['panel_write_age_s'] - .05) < .001
  assert [x['kind'] for x in value['events']].count('input_mode') == 3
  assert {'encoder_expired', 'panel_write_gap'} <= {x['kind'] for x in value['events']}
  assert any(x.get('reason') == 'encoder_lease_short' for x in value['events'])
  json.dumps(value, allow_nan=False)


def test_no_duplicate_events_for_stable_mode_or_normal_panel_chunks():
  trace = HudPipelineTrace(max_events=4)
  trace.input('vehicle', now=1.)
  trace.input('vehicle', now=1.1)
  trace.panel(now=1.)
  trace.panel(now=1.1)
  trace.panel(now=1.2)
  assert [e['kind'] for e in trace.snapshot(now=1.3)['events']] == ['input_mode']


def test_events_bounded_and_no_raw_vehicle_data_or_nan():
  trace = HudPipelineTrace(max_events=3)
  for i in range(9):
    trace.event('encoder_read_failed', now=float(i), error_type='RuntimeError', value=float('nan'))
  output = trace.snapshot(now=9.)
  assert len(output['events']) == 3
  assert all('value' not in event for event in output['events'])
  assert all(event['kind'] == 'encoder_read_failed' for event in output['events'])
  json.dumps(output, allow_nan=False)


def test_waiting_episode_durations_do_not_count_reason_changes_twice():
  trace = HudPipelineTrace(max_events=16)
  trace.input('vehicle', now=10.)
  trace.input('waiting_data', reason='encoder_lease_short', age_s=.42, now=11.)
  trace.input('waiting_data', reason='tcp_interrupted', now=11.05)
  trace.input('vehicle', age_s=.12, now=11.2)
  trace.input('waiting_data', reason='encoder_lease_short', now=12.)
  trace.input('vehicle', now=12.15)
  output = trace.snapshot(now=12.2)
  assert output['waiting_episodes'] == 2
  assert abs(output['waiting_completed_total_s'] - .35) < 1e-4
  assert abs(output['waiting_max_s'] - .2) < 1e-4
  assert [event['kind'] for event in output['events']].count('waiting_end') == 2
  assert output['input_mode'] == 'vehicle'


def test_default_panel_trace_catches_sub_400ms_blackouts_without_raising_faults():
  trace = HudPipelineTrace()
  assert trace.snapshot(now=1.)['panel_gap_threshold_s'] == .15
  trace.panel(now=10.)
  trace.panel(now=10.10)
  trace.panel(now=10.31)  # 210ms gap was invisible to the old 400ms observer
  value = trace.snapshot(now=10.32)
  assert value['panel_gap_count'] == 1
  assert abs(value['panel_gap_max_s'] - .21) < .0001
  assert any(e['kind'] == 'panel_write_gap' and abs(e['duration_s'] - .21) < .0001 for e in value['events'])


def test_adaptive_reserve_only_after_expired_idr_and_automatically_recovers():
  trace = HudPipelineTrace()
  assert trace.encoder_reserve_s(now=10., baseline=.1, recovery=.15, hold_s=4.) == .1
  trace.encoded(expired=False, mode='vehicle', now=10.1)
  assert trace.encoder_reserve_s(now=10.2) == .1
  trace.encoded(expired=True, mode='vehicle', lease_remaining_s=-.01, now=11.)
  assert trace.encoder_reserve_s(now=11.01) == .15
  assert trace.encoder_reserve_s(now=14.99) == .15
  assert trace.encoder_reserve_s(now=15.) == .1
  assert trace.encoder_reserve_s(now=10.99) == .1  # clock rollback cannot extend guard
  output = trace.snapshot(now=16.)
  assert output['encoder_expired_count'] == 1
  assert output['adaptive_guard_uses'] == 2
  assert output['encoder_last_expiry_age_s'] == 5.


def test_adaptive_reserve_never_reduces_baseline_limit():
  trace = HudPipelineTrace()
  trace.encoded(expired=True, mode='vehicle', now=20.)
  assert trace.encoder_reserve_s(now=20.1, baseline=.2, recovery=.15) == .2
