"""NEXO cruise/card timing is observational and bounded, not a control policy."""
import ast
from pathlib import Path

from openpilot.selfdrive.car.cruise_timing import CruiseTimingWindow


def test_correlated_card_and_cruise_peaks_and_averages():
  window = CruiseTimingWindow(window_ns=3_000_000_000)
  base = 10_000_000_000
  first = {'card_total_ms': 17., 'card_tail_after_receive_ms': 9.,
           'card_total_cpu_ms': 2., 'cruise_total_ms': 2., 'cruise_params_ms': 1.}
  slow = {'card_total_ms': 30., 'card_tail_after_receive_ms': 19.,
          'card_total_cpu_ms': 3., 'cruise_total_ms': 11., 'cruise_params_ms': 10.}
  normal = {'card_total_ms': 8., 'card_tail_after_receive_ms': 4.,
            'card_total_cpu_ms': 3., 'cruise_total_ms': 1., 'cruise_params_ms': .4}
  assert window.record(first, frame=1, receive_mono_ns=base-1_000_000,
                       finished_mono_ns=base, params_refreshed=True) is None
  assert window.record(slow, frame=2, receive_mono_ns=base+1_500_000_000,
                       finished_mono_ns=base+1_500_000_000, params_refreshed=False) is None
  report = window.record(normal, frame=3, receive_mono_ns=base+3_000_000_000,
                         finished_mono_ns=base+3_000_000_000, params_refreshed=False)
  assert report['read_only'] is True and report['samples'] == 3
  assert report['slow_card_tail_over_10ms'] == 1
  assert report['slow_cruise_over_5ms'] == 1
  assert report['peak_card']['cruise_frame'] == 2
  assert report['peak_cruise']['cruise_frame'] == 2
  assert report['peak_card']['params_refreshed'] is False
  assert report['peak_card']['stages_ms']['cruise_params_ms'] == 10.
  assert report['max_ms']['card_tail_after_receive_ms'] == 19.
  assert report['avg_ms']['cruise_total_ms'] == 4.667
  assert window.sample_count == 0  # resets after each bounded rollup


def test_receive_wait_alone_is_not_reported_as_slow_card():
  window = CruiseTimingWindow(window_ns=10)
  metrics = {'card_total_ms': 22., 'card_tail_after_receive_ms': 2., 'cruise_total_ms': 1.}
  window.record(metrics, frame=4, receive_mono_ns=1, finished_mono_ns=100, params_refreshed=False)
  report = window.record(metrics, frame=5, receive_mono_ns=11, finished_mono_ns=110, params_refreshed=False)
  assert report['slow_card_tail_over_10ms'] == 0
  assert report['peak_card']['stages_ms']['card_total_ms'] == 22.


def test_invalid_unknown_and_missing_stages_never_pollute_summary():
  window = CruiseTimingWindow(window_ns=100)
  assert window.record({'cruise_total_ms': float('nan'), 'extra': 55.},
                       frame=0, receive_mono_ns=None, finished_mono_ns=10,
                       params_refreshed=False) is None
  assert window.sample_count == 0
  window.record({'cruise_total_ms': 1., 'cruise_params_ms': -3.,
                 'cruise_buttons_ms': float('inf'), 'card_total_ms': True},
                frame=1, receive_mono_ns=None, finished_mono_ns=20,
                params_refreshed=False)
  report = window.record({'cruise_total_ms': 2.}, frame=2,
                         receive_mono_ns=None, finished_mono_ns=120, params_refreshed=False)
  assert report['samples'] == 2
  assert report['avg_ms'] == {'cruise_total_ms': 1.5}
  assert report['peak_cruise']['receive_mono_ns'] is None
  assert report['peak_card'] is None


def test_clock_reversal_resets_old_observations():
  window = CruiseTimingWindow(window_ns=100)
  window.record({'cruise_total_ms': 20.}, frame=1, receive_mono_ns=None,
                finished_mono_ns=500, params_refreshed=False)
  window.record({'cruise_total_ms': 1.}, frame=2, receive_mono_ns=None,
                finished_mono_ns=200, params_refreshed=False)
  report = window.record({'cruise_total_ms': 2.}, frame=3, receive_mono_ns=None,
                         finished_mono_ns=300, params_refreshed=False)
  assert report['samples'] == 2 and report['max_ms']['cruise_total_ms'] == 2.


def test_production_integration_does_not_change_control_data_or_send_frame_logs():
  root = Path(__file__).resolve().parents[1]
  card_src = (root / 'card.py').read_text()
  cruise_src = (root / 'cruise.py').read_text()
  ast.parse(card_src)
  ast.parse(cruise_src)
  assert 'self.card_can_received_ns = time.monotonic_ns()' in card_src
  assert 'self.cruise_timing.record(' in card_src
  assert "cloudlog.warning('NEXO_CRUISE_TIMING '" in card_src
  assert 'self.runtime_timing = {' in cruise_src
  for key in ('cruise_params_cpu_ms', 'cruise_carrot_command_ms',
              'cruise_button_prepare_ms', 'cruise_brake_gas_ms'):
    assert key in cruise_src
