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


def test_gap_has_window_and_last_normal_write_excludes_waiting_frames():
  trace = HudPipelineTrace()
  trace.panel(mode='vehicle', now=10.)
  trace.panel(mode='waiting_data', now=10.31)
  state = trace.snapshot(now=10.5)
  assert state['last_vehicle_write_mono'] == 10.
  assert state['last_gap'] == dict(start_mono=10., end_mono=10.31, duration_s=.31,
                                   evidence='completed_frame_gap', physical_hud_verified=False)
  assert state['events'][-1]['jetson_unix'] > 0


def test_continuous_panel_writes_still_identify_waiting_pixels_and_return():
  trace = HudPipelineTrace()
  trace.panel(mode='vehicle', now=20.)
  trace.panel(mode='waiting_data', reason='hud_receive_delayed', now=20.1)
  trace.panel(mode='waiting_data', reason='tcp_interrupted', now=20.2)
  trace.panel(mode='vehicle', now=20.3)
  summary = trace.snapshot(now=20.31)
  assert summary['panel_gap_count'] == 0
  assert [(e['mode'], e['reason']) for e in summary['events'] if e['kind'] == 'panel_content_changed'] == [
    ('vehicle', ''), ('waiting_data', 'hud_receive_delayed'), ('waiting_data', 'tcp_interrupted'), ('vehicle', '')]


def test_short_wait_is_counted_separately_from_physical_panel_gap():
  from openpilot.tools.jetson.hud_pipeline_trace import HudPipelineTrace
  trace = HudPipelineTrace()
  trace.input('vehicle', now=10.)
  trace.panel(mode='vehicle', now=10.)
  trace.input('waiting_data', reason='hud_receive_delayed', now=10.1)
  trace.panel(mode='waiting_data', now=10.12)
  trace.input('vehicle', now=10.205)
  report = trace.snapshot(now=10.21)
  assert report['waiting_short_count'] == 1
  assert report['waiting_short_max_s'] == .105
  assert report['panel_gap_count'] == 0
  assert report['waiting_episodes'] == 1
