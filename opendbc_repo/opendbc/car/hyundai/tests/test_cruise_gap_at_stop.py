"""Portable feedback simulation and controller arbitration tests (no CAN hardware)."""
import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

HYUNDAI = Path(__file__).resolve().parents[1]
ROOT = HYUNDAI.parents[3]
spec = importlib.util.spec_from_file_location("cruise_gap_at_stop", HYUNDAI / "cruise_gap_at_stop.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CruiseGapAtStop, GapState = module.CruiseGapAtStop, module.GapState


class TestGapAtStop(unittest.TestCase):
  def setUp(self):
    self.logs = []
    self.fsm = CruiseGapAtStop(self.logs.append)

  def tick(self, now, gap=4, **kw):
    values = dict(enabled=True, valid=True, gap=gap, speed=0., standstill=True)
    values.update(kw)
    return self.fsm.update(now, **values)

  def test_all_gaps_and_both_cycle_directions(self):
    for initial in (1, 2, 3, 4):
      for direction in (-1, 1):
        with self.subTest(initial=initial, direction=direction):
          self.setUp()
          gap, sends, reduce_seen = initial, [], False
          for frame in range(2200):
            now = frame / 100
            moving = now >= 8
            if self.tick(now, gap, speed=1. if moving else 0., standstill=not moving):
              sends.append(now)
              gap = (gap - 1 + direction) % 4 + 1
            if self.fsm.state == GapState.WAIT_START:
              reduce_seen = True
              self.assertEqual(gap, 1)
              self.assertEqual(self.fsm.saved_gap, initial)
          self.assertTrue(reduce_seen)
          self.assertEqual(gap, initial)
          self.assertIsNone(self.fsm.saved_gap)
          self.assertEqual(self.fsm.state, GapState.IDLE)
          self.assertTrue(all(b - a >= 1. for a, b in zip(sends, sends[1:])))
          self.assertEqual(sum('saved gap=' in s for s in self.logs), 1)
          if initial == 1:
            self.assertFalse(sends)

  def test_stop_debounce(self):
    self.assertFalse(self.tick(0))
    self.assertFalse(self.tick(.99))
    self.assertFalse(self.tick(1, speed=.2))
    self.assertFalse(self.tick(1.1))
    self.assertFalse(self.tick(2.09))
    self.assertTrue(self.tick(2.11))

  def test_feedback_timeout_no_blind_retry(self):
    self.tick(0)
    self.assertTrue(self.tick(1))
    self.assertFalse(self.tick(2))
    self.assertFalse(self.tick(2.5))
    self.assertEqual(self.fsm.state, GapState.LOCKOUT)
    self.assertFalse(self.tick(30))
    self.assertIsNone(self.fsm.saved_gap)

  def test_invalid_and_cancel_during_every_phase(self):
    bad_values = [dict(enabled=False), dict(valid=False), dict(cancel=True), dict(manual_gap=True)]
    bad_values += [dict(gap=v) for v in (None, 0, -1, 5, 1.5, True, float('nan'))]
    for phase in (GapState.WAIT_STOP, GapState.REDUCING, GapState.WAIT_START, GapState.RESTORING):
      for values in bad_values:
        with self.subTest(phase=phase, values=values):
          self.setUp()
          self.fsm.state, self.fsm.saved_gap = phase, 4
          self.assertFalse(self.tick(0, **values))
          self.assertIsNone(self.fsm.saved_gap)
          self.assertEqual(self.fsm.state, GapState.LOCKOUT)
          self.assertFalse(self.tick(10))

  def test_manual_lockout_rearms_only_after_moving(self):
    self.tick(0)
    self.tick(1)
    self.tick(1.1, manual_gap=True)
    self.assertFalse(self.tick(100, gap=2))
    self.tick(101, gap=2, speed=1, standstill=False)
    self.tick(101.31, gap=2, speed=1, standstill=False)
    self.tick(102, gap=2)
    self.assertTrue(self.tick(103, gap=2))
    self.assertEqual(self.fsm.saved_gap, 2)

  def test_busy_and_attempt_limit(self):
    self.tick(0)
    for t in (1, 2, 3):
      self.assertFalse(self.tick(t, can_send=False))
    for n in range(6):
      self.assertTrue(self.tick(4+n, gap=4 if n % 2 == 0 else 3))
    self.assertFalse(self.tick(10, gap=4))
    self.assertEqual(self.fsm.state, GapState.LOCKOUT)

  def test_early_start_restores_partial_reduction(self):
    self.tick(0)
    self.tick(1)
    self.tick(1.1, gap=3)
    self.tick(1.2, gap=3, speed=1, standstill=False)
    self.assertFalse(self.tick(1.51, gap=3, speed=1, standstill=False))
    self.assertEqual(self.fsm.state, GapState.RESTORING)
    self.assertTrue(self.tick(2.01, gap=3, speed=1, standstill=False))
    self.tick(2.1, gap=4, speed=1, standstill=False)
    self.assertIsNone(self.fsm.saved_gap)

  def test_start_debounce_and_restore_stop_cancel(self):
    self.tick(0)
    self.tick(1)
    self.tick(1.1, gap=1)
    self.tick(2, gap=1, speed=1, standstill=False)
    self.tick(2.2, gap=1)
    self.assertEqual(self.fsm.state, GapState.WAIT_START)
    self.tick(3, gap=1, speed=1, standstill=False)
    self.tick(3.31, gap=1, speed=1, standstill=False)
    self.tick(3.32, gap=2)
    self.assertEqual(self.fsm.state, GapState.LOCKOUT)

  def test_busy_operation_times_out(self):
    self.tick(0)
    self.tick(1, can_send=False)
    self.assertFalse(self.tick(11, can_send=False))
    self.assertEqual(self.fsm.state, GapState.LOCKOUT)

  def test_second_stop_saves_new_driver_gap(self):
    for start, initial in ((0., 4), (20., 2)):
      gap = initial
      for frame in range(1500):
        moving = frame >= 700
        if self.tick(start + frame / 100, gap, speed=1. if moving else 0., standstill=not moving):
          gap = gap % 4 + 1
      self.assertEqual(gap, initial)
      self.assertIsNone(self.fsm.saved_gap)
    self.assertEqual(sum('saved gap=' in s for s in self.logs), 2)


class TestControllerGate(unittest.TestCase):
  def setUp(self):
    # Execute the actual integration method with message/vehicle test doubles.
    tree = ast.parse((HYUNDAI / 'carcontroller.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CarController')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'update_cruise_gap_at_stop')
    self.builders = []
    def builder(*args):
      self.builders.append(args)
      return (0x4F1, b'gap', 0)
    env = dict(CAR=NS(HYUNDAI_NEXO_1ST_GEN='nexo'), HyundaiFlags=NS(CANFD=1, CC_ONLY_CAR=2),
               Buttons=NS(GAP_DIST=3, CANCEL=4, NONE=0), DT_CTRL=.01,
               structs=NS(CarState=NS(GearShifter=NS(drive='D'),
                                      ButtonEvent=NS(Type=NS(gapAdjustCruise='gap', cancel='cancel')))),
               hyundaican=NS(create_clu11_button=builder))
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<controller method>', 'exec'), env)
    self.method = env['update_cruise_gap_at_stop']
    self.controller = NS(CP=NS(carFingerprint='nexo', openpilotLongitudinalControl=False, flags=0),
                         cruise_gap_auto_reduce_at_stop=True, cruise_gap_at_stop=CruiseGapAtStop(lambda _: None),
                         frame=1000, last_button_frame=0, packer=object())
    self.cc = NS(enabled=True, cruiseControl=NS(cancel=False, resume=False))
    self.cs = NS(cruise_buttons=[0], clu11={}, out=NS(
      canValid=True, canTimeout=False, accFaulted=False, gearShifter='D',
      cruiseState=NS(available=True, enabled=True, nonAdaptive=False),
      pcmCruiseGap=4, vEgo=0., standstill=True, buttonEvents=[], brakePressed=False, brakeHoldActive=False))

  def run_ticks(self, messages=None):
    messages = [] if messages is None else messages
    self.method(self.controller, self.cc, self.cs, 0, messages)
    self.method(self.controller, self.cc, self.cs, 1_000_000_000, messages)
    return messages

  def test_sender_reuses_existing_constructor(self):
    self.assertEqual(self.run_ticks(), [(0x4F1, b'gap', 0)])
    self.assertEqual(self.builders[0][3], 3)

  def test_off_and_unsupported_modes_no_messages(self):
    for target, name, value in [('controller', 'cruise_gap_auto_reduce_at_stop', False),
                                ('CP', 'openpilotLongitudinalControl', True), ('CP', 'carFingerprint', 'other'),
                                ('CP', 'flags', 1), ('CP', 'flags', 2)]:
      self.setUp()
      setattr(self.controller if target == 'controller' else self.controller.CP, name, value)
      messages = [(123, b'original', 0)]
      self.assertEqual(self.run_ticks(messages), [(123, b'original', 0)])
      self.assertFalse(self.builders)

  def test_vehicle_cancels_and_existing_buttons_win(self):
    for name, value in [('canValid', False), ('canTimeout', True), ('accFaulted', True),
                        ('gearShifter', 'R'), ('gearShifter', 'N'), ('pcmCruiseGap', 0),
                        ('brakePressed', True), ('brakeHoldActive', True)]:
      self.setUp()
      setattr(self.cs.out, name, value)
      self.assertFalse(self.run_ticks(), name)
    for name in ('available', 'enabled'):
      self.setUp()
      setattr(self.cs.out.cruiseState, name, False)
      self.assertFalse(self.run_ticks())
    for button in (1, 2, 3, 4):
      self.setUp()
      self.cs.cruise_buttons = [button, 0]
      self.assertFalse(self.run_ticks())
    for name in ('cancel', 'resume'):
      self.setUp()
      setattr(self.cc.cruiseControl, name, True)
      self.assertFalse(self.run_ticks())
    self.setUp()
    existing = [(0x4F1, b'existing', 0)]
    self.assertEqual(self.run_ticks(existing), [(0x4F1, b'existing', 0)])

  def test_option_off_cancels_pending_restore(self):
    self.run_ticks()
    self.controller.cruise_gap_auto_reduce_at_stop = False
    messages = []
    self.method(self.controller, self.cc, self.cs, 2_000_000_000, messages)
    self.assertFalse(messages)
    self.assertIsNone(self.controller.cruise_gap_at_stop.saved_gap)
    self.controller.cruise_gap_auto_reduce_at_stop = True
    self.method(self.controller, self.cc, self.cs, 10_000_000_000, messages)
    self.assertFalse(messages)


class TestSettings(unittest.TestCase):
  def test_toggle_registered_and_in_gap_menu(self):
    data = json.loads((ROOT / 'openpilot/selfdrive/carrot_settings.json').read_text(encoding='utf-8'))
    name = 'CruiseGapAutoReduceAtStop'
    entry = next(p for p in data['params'] if p['name'] == name)
    self.assertEqual((entry['control'], entry['min'], entry['max'], entry['default']), ('toggle', 0, 1, 0))
    self.assertEqual(entry['title'], '정차 시 차간거리 1단계')
    def visit(nodes):
      for node in nodes:
        if node.get('id') == 'CRUISE_GAP':
          return node
        child = visit(node.get('groups', []))
        if child:
          return child
    self.assertIn(name, visit(data['menu'])['params'])
    keys = (ROOT / 'openpilot/common/params_keys.h').read_text(encoding='utf-8')
    self.assertIn('{"CruiseGapAutoReduceAtStop", {PERSISTENT, BOOL, "0"}}', keys)


if __name__ == '__main__':
  unittest.main()
