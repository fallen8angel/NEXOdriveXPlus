"""Carrot-style read-coalescing without weakening the NEXO 500ms HUD lease."""
from unittest.mock import patch

from openpilot.tools.jetson import snapshot


def sample(updated):
  return {'version': 1, 'updated': updated, 'sent': updated,
          'events': {}, 'params': {}, 'mono': {}, 'received': {},
          'valid': {}, 'alive': {}, 'cameras': {}}


def test_repeated_reads_coalesced_but_newest_frame_visible_after_20ms():
  reader = snapshot.CachedSnapshotReader()
  now = [100.]
  values = [sample(99.9), sample(100.015)]
  calls = []

  def load(*args, **kwargs):
    calls.append((args, kwargs))
    return values[min(len(calls) - 1, 1)]

  with patch.object(snapshot.time, 'monotonic', side_effect=lambda: now[0]), \
       patch.object(snapshot, 'read_fresh', side_effect=load):
    assert reader.read()['updated'] == 99.9
    now[0] = 100.019
    assert reader.read()['updated'] == 99.9
    assert len(calls) == 1
    now[0] = 100.02
    assert reader.read()['updated'] == 100.015
    assert len(calls) == 2
    assert all(kwargs['limit'] == snapshot.MAX_SNAPSHOT + 1024 for _, kwargs in calls)
    assert all(kwargs['now'] in (100., 100.02) for _, kwargs in calls)


def test_cached_snapshot_never_leaks_past_half_second_or_future():
  reader = snapshot.CachedSnapshotReader()
  now = [200.]
  with patch.object(snapshot.time, 'monotonic', side_effect=lambda: now[0]), \
       patch.object(snapshot, 'read_fresh', return_value=sample(199.501)) as load:
    assert reader.read() is not None
    now[0] = 200.001
    assert reader.read() is None
    # No extra disk read within 20ms even though the stored lease expired.
    assert load.call_count == 1
    now[0] = 199.99  # monotonic clock rollback: never show future snapshot
    assert reader.read() is not None  # original updated time is still in past

  future = snapshot.CachedSnapshotReader()
  with patch.object(snapshot.time, 'monotonic', return_value=300.), \
       patch.object(snapshot, 'read_fresh', return_value=sample(300.01)):
    assert future.read() is None


def test_missing_or_invalid_snapshot_does_not_replay_last_good_value():
  reader = snapshot.CachedSnapshotReader()
  now = [300.]
  values = [sample(299.9), None, sample(300.03)]
  with patch.object(snapshot.time, 'monotonic', side_effect=lambda: now[0]), \
       patch.object(snapshot, 'read_fresh', side_effect=values):
    assert reader.read() is not None
    now[0] = 300.025
    assert reader.read() is None
    now[0] = 300.03
    assert reader.read() is None  # cached absence, no stale fallback
    now[0] = 300.05
    assert reader.read() is not None


def test_invalid_snapshot_is_rejected_not_cached_as_vehicle_data():
  reader = snapshot.CachedSnapshotReader()
  with patch.object(snapshot.time, 'monotonic', return_value=400.), \
       patch.object(snapshot, 'read_fresh', return_value={'updated': 399.9, 'version': 99}):
    assert reader.read() is None
