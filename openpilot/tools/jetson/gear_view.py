"""Jetson-only scene selection inside the live cluster render target."""
from dataclasses import replace
from pathlib import Path
import math
import time

from openpilot.tools.jetson.hud_pipeline_trace import HUD_TRACE
from openpilot.tools.jetson.state import finite


def fresh_service(snapshot, service, now, limit=.5):
  """Combine age at source with time elapsed since receipt on this Jetson."""
  stamp = snapshot['mono'].get(service, 0)
  elapsed = now - snapshot['updated']
  age = snapshot['sent'] - stamp * 1e-9
  return (stamp > 0 and 0 <= elapsed < .5 and 0 <= age + 1e-6
          and age + elapsed < limit and snapshot['alive'].get(service) is True
          and snapshot['valid'].get(service) is True)


class GearSelector:
  def __init__(self):
    self.session = None
    self.gear = None
    self.candidate = None
    self.first_stamp = 0
    self.last_stamp = 0
    self.error = None

  def update(self, snapshot, now):
    view = (snapshot or {}).get('gear_view')
    if snapshot and snapshot.get('session') != self.session:
      self.__init__()
      self.session = snapshot.get('session')
    if not isinstance(view, dict):
      view = {}
    sample = view.get('sample_ns')
    fresh = bool(snapshot and type(sample) is int and sample > 0 and fresh_service(snapshot, 'carState', now)
                 and 0 <= snapshot['sent'] - sample*1e-9 + 1e-6
                 and snapshot['sent'] - sample*1e-9 + now - snapshot['updated'] < .5 and view.get('can_ok') is True)
    gear = view.get('gear') if fresh else None
    error = None if gear in ('park', 'drive', 'neutral', 'reverse') else ('unknown_gear' if fresh else 'gear_unavailable')
    if error != self.error:
      HUD_TRACE.event('gear_input', reason=error or 'restored', now=now)
      self.error = error
    if error == 'unknown_gear':
      # A *confirmed* conflicting raw code invalidates the pending candidate.
      self.candidate = None
    elif not error:
      stamp = min(snapshot['mono']['carState'], sample)
      if stamp > self.last_stamp:
        # EMS20 may be slower than the 10 Hz HUD. Keep a candidate across
        # missing/expired samples, but require two DISTINCT raw observations
        # within two seconds before accepting a gear change.
        if gear != self.candidate or stamp - self.last_stamp > 2_000_000_000:
          self.candidate, self.first_stamp = gear, stamp
        elif stamp - self.first_stamp >= 50_000_000 and self.gear != gear:
          self.gear = gear
          HUD_TRACE.event('gear_change', gear=gear, now=now)
        self.last_stamp = stamp
    return self.gear, fresh and error is None


ASSET = Path(__file__).with_name('nexo_park.jpg')
WHITE = (231, 237, 240)
MUTED = (153, 169, 179)
MINT = (124, 220, 193)
# SPAS12 supplies three indication stages, not calibrated metres.
# Zero/unknown channels stay neutral: green is stage 1, not 'safe'.
SENSOR_COLORS = {
  0: (62, 78, 89), 1: (89, 215, 145),
  2: (248, 199, 73), 3: (255, 86, 66),
}
REVERSE_PANEL_BG = (17, 29, 41, 255)
REVERSE_PANEL_LINE = (47, 76, 100, 255)


def reverse_sensor_display(indications, fresh):
  """Return only validated SPAS12 positions and a short truthful warning.

  Five front/rear positions are ordered left to right viewed from above.
  An absent/old CAN source is not the same as a zero-detection source.
  """
  empty = (0,) * 5
  if not fresh:
    return empty, empty, '센서 정보 미수신', MUTED, False

  def positions(name):
    raw = getattr(indications, name, ())
    if not isinstance(raw, (tuple, list)):
      return empty
    codes = tuple(value if type(value) is int and value in (1, 2, 3) else 0 for value in raw[:5])
    return (codes + empty)[:5]

  front, rear = positions('front_codes'), positions('rear_codes')
  severity = max(front + rear)
  if severity == 0:
    return front, rear, '감지 표시 없음', MUTED, True
  # On a reversing HUD prefer rear at equal severity; never imply a distance.
  for end, codes in (('후방', rear), ('전방', front)):
    for idx, code in enumerate(codes):
      if code == severity:
        side = '좌측' if idx < 2 else '중앙' if idx == 2 else '우측'
        return front, rear, f'{end} {side} 감지', SENSOR_COLORS[severity], True
  return front, rear, '감지 표시 없음', MUTED, True



def park_summary(state, *, fresh, metric=True):
  """Read-only P screen values, using only decoded and fresh HUD state."""
  empty = (('거리', '--'), ('시간', '--'), ('평균', '--'), ('자동주행', '--'))
  if not fresh:
    return {'hydrogen': '--', 'front': '--/--', 'rear': '--/--', 'trip': empty}

  level = getattr(state, 'fuel_gauge', None)
  hydrogen = (f'{level * 100:.0f}%' if getattr(state, 'energy_gauge_label', None) == 'fuel'
              and finite(level) and 0 <= level <= 1 else '--')
  tpms = getattr(state, 'tpms', None)
  def pressure(key):
    value = getattr(tpms, key, None)
    return f'{value:.0f}' if finite(value) and 0 < value < 100 else '--'
  front, rear = f'{pressure("fl")}/{pressure("fr")}', f'{pressure("rl")}/{pressure("rr")}'

  trip = getattr(state, 'trip_report', None)
  if trip is None:
    rows = empty
  else:
    distance = getattr(trip, 'distance_m', None)
    duration = getattr(trip, 'duration_s', None)
    average = getattr(trip, 'average_speed_kph', None)
    auto_ratio = getattr(trip, 'auto_ratio_percent', None)
    rows = (
      ('거리', (f'{distance / (1000 if metric else 1609.344):.2f} {"km" if metric else "mi"}'
               if finite(distance) and 0 <= distance <= 5e7 else '--')),
      ('시간', (f'{int(duration // 3600)}시간 {int(duration // 60) % 60:02d}분' if duration >= 3600
               else f'{int(duration // 60)}분') if finite(duration) and 0 <= duration <= 1e7 else '--'),
      ('평균', (f'{average * (1 if metric else .621371192):.1f} {"km/h" if metric else "mph"}'
               if finite(average) and 0 <= average <= 300 else '--')),
      ('자동주행', (f'{auto_ratio:.0f}%' if finite(auto_ratio) and 0 <= auto_ratio <= 100 else '--')),
    )
  return {'hydrogen': hydrogen, 'front': front, 'rear': rear, 'trip': rows}


class GearView:
  def __init__(self):
    self.selector = GearSelector()
    self.texture = None
    self.tried_texture = False
    self.mode = None
    self.entered = 0.
    self.park_enabled = True
    self.last_valid_time = None

  def _selected_gear(self, gear, valid, now):
    # A dropped raw EMS20 observation must not turn a confirmed parked car
    # into the driving scene. Do not keep stale P/R indefinitely, either:
    # after the bounded grace period use an explicit unknown-gear screen.
    if valid and gear is not None:
      self.last_valid_time = now
      return gear
    elapsed = now - self.last_valid_time if self.last_valid_time is not None else float('inf')
    if 0 <= elapsed <= (3.0 if gear == 'park' else 0.35 if gear == 'reverse' else 1.0):
      return gear if gear is not None else 'unavailable'
    return 'unavailable'

  def close(self, rl):
    if self.texture is not None:
      rl.unload_texture(self.texture)
      self.texture = None

  def render(self, renderer, rl, original, state, lights, snapshot, clock, now):
    gear, valid = self.selector.update(snapshot, now)
    view = (snapshot or {}).get('gear_view') or {}
    if isinstance(view, dict) and isinstance(view.get('park_enabled'), bool):
      self.park_enabled = view['park_enabled']
    gear = self._selected_gear(gear, valid, now)
    # Unlike a temporarily missed sample, an actively conflicting raw CAN
    # observation must never keep the last P/R scene on screen.
    if self.selector.error == 'unknown_gear':
      gear = 'unavailable'
    mode = 'drive' if gear == 'park' and not self.park_enabled else gear
    if mode != self.mode:
      self.mode, self.entered = mode, now
    if mode == 'unavailable':
      # No gear proof: never show the normal driving scene as a P fallback.
      # Do not retain a previous reverse/side-camera frame in this state.
      renderer._side_camera.close()
      renderer._reverse_camera.close()
      self.draw_gear_wait(renderer, rl, snapshot or {}, clock)
      renderer._draw_alert_overlay(state.alert)
      return
    if mode not in ('park', 'reverse'):
      if mode == 'neutral' and state.gear_text != 'N':
        state = replace(state, gear_text='N')
      return original(renderer, state, lights)

    renderer._side_camera.close()
    # The existing reverse session is already wired to the shared Comma4
    # camerad DRIVER stream in hud.install_adapters(). Keep it alive only in R.
    if mode != 'reverse':
      renderer._reverse_camera.close()
    if mode == 'park':
      self.draw_park(renderer, rl, state, snapshot or {}, view if valid else {}, clock, now, valid)
    else:
      self.draw_reverse(renderer, rl, state, snapshot, valid, now)
    renderer._draw_alert_overlay(state.alert)

  def draw_gear_wait(self, ui, rl, snapshot, clock):
    # Intentionally omit the last speed, parking image and camera pixels.
    w, h = ui.width, ui.height
    rl.clear_background((9, 12, 17, 255))
    ui._draw_text(clock(snapshot) if snapshot else '--:--:--', w*.5, h*.18, h*.07,
                  MUTED, anchor='center')
    ui._draw_text('기어 신호 확인 중', w*.5, h*.50, h*.15, WHITE, anchor='center')
    ui._draw_text('P · R · N · D 상태를 기다리고 있습니다', w*.5, h*.75, h*.06,
                  MUTED, anchor='center')

  def draw_park(self, ui, rl, state, snapshot, view, clock, now, valid):
    w, h = ui.width, ui.height
    rl.clear_background((9, 12, 17, 255))
    if not self.tried_texture:
      self.texture = ui._load_icon_texture(ASSET, 'NEXO park')
      self.tried_texture = True
    if self.texture is not None:
      # The two narrow edge samples extend the studio floor across wide panels.
      # Only the middle photograph moves, subtly, during the first two ticks.
      t = self.texture
      z = 1 + .025 * max(0., 1 - (now - self.entered) / .18)
      dh = min(h * .98, w * .60 / 1.5) * z
      dw = dh * t.width / t.height
      x, y = (w - dw) / 2, (h - dh) / 2 - h * .012
      def part(sx, sw, dx, width):
        rl.draw_texture_pro(t, rl.Rectangle(sx, 0, sw, t.height), rl.Rectangle(dx, y, width, dh),
                            rl.Vector2(0, 0), 0, rl.WHITE)
      part(0, 1, 0, x + 1)
      part(t.width - 1, 1, x + dw - 1, x + 1)
      part(0, t.width, x, dw)
      if int(now * 2.5) % 2 == 0:
        # Coordinates on the unmirrored front-left photograph. The shared
        # portrait/mirror transform is applied to lamps and chassis together.
        for key, a, b in (('left', (.356, .486), (.457, .465)),
                          ('right', (.098, .476), (.12, .491))):
          if view.get(key) is True:
            rl.draw_line_ex(rl.Vector2(x+a[0]*dw, y+a[1]*dh), rl.Vector2(x+b[0]*dw, y+b[1]*dh),
                            max(2., dh*.009), (255, 176, 45, 255))
    else:
      ui._draw_text('차량 이미지 미수신', w*.5, h*.50, h*.06, MUTED, anchor='center')
    label = ui._draw_text
    label('P', w*.040, h*.23, h*.29, WHITE)
    label('주차', w*.044, h*.43, h*.067, MINT)
    label('HYUNDAI', w*.042, h*.66, h*.037, MUTED)
    label('NEXO', w*.04, h*.75, h*.13, WHITE)
    label(clock(snapshot) if snapshot else '--:--:--', w*.5, h*.079, h*.076, WHITE, anchor='center')
    # Benchmark Carrot's parked summary without its EV6 photo, weather
    # requests, or extra GPU resources. An unknown NEXO range stays unknown.
    details = park_summary(state, fresh=valid and fresh_service(snapshot, 'carState', now),
                           metric=getattr(ui, 'is_metric', True))
    label('수소 잔량', w*.96, h*.16, h*.050, MUTED, anchor='right')
    label(details['hydrogen'], w*.96, h*.26, h*.066, WHITE, anchor='right')
    label('공기압(psi)', w*.73, h*.35, h*.043, MUTED)
    label(f"앞 {details['front']}   뒤 {details['rear']}", w*.96, h*.35, h*.047, WHITE, anchor='right')
    label('이번 주행', w*.73, h*.45, h*.060, MINT)
    for i, (name, value) in enumerate(details['trip']):
      y = h * (.54 + .078 * i)
      label(name, w*.74, y, h*.052, MUTED)
      label(value, w*.96, y, h*.055, WHITE, anchor='right')
    panel_age = HUD_TRACE.snapshot(now=now)['panel_write_age_s']
    status = (
      ('openpilot', '연결' if snapshot and fresh_service(snapshot, 'selfdriveState', now) else '미수신'),
      ('Jetson', '수신 중' if snapshot and 0 <= now - snapshot['updated'] < .5 else '대기'),
      ('차량 통신', '수신 중' if view else '미수신'),
      ('HUD', '전송 중' if finite(panel_age) and panel_age < 1 else '출력 대기'),
    )
    rl.draw_rectangle(0, int(h*.87), w, h-int(h*.87), (18, 25, 31, 255))
    for i, (name, value) in enumerate(status):
      x = w * (.04 + i*.25)
      color = MINT if value in ('연결', '수신 중', '전송 중') else MUTED
      rl.draw_circle(int(x), int(h*.936), max(2., h*.011), (*color, 255))
      label(f'{name}  {value}', x + h*.034, h*.936, min(w*.019, h*.053), WHITE)

  def draw_reverse(self, ui, rl, state, snapshot, valid, now):
    w, h = ui.width, ui.height
    rl.clear_background((15, 22, 29, 255))
    label = ui._draw_text
    label('R', w*.04, h*.20, h*.27, WHITE)
    label('후진', w*.044, h*.40, h*.065, MINT)
    # Comma4's interior-facing camera is *not* a rear-facing parking camera.
    # Read only the existing driver preview; no new camera device, process,
    # network channel, or driver-monitoring inference is started on Jetson.
    # ReverseCameraSession handles stream errors and closes its GPU texture
    # when leaving R. RemoteCamera enforces the 300 ms image lease.
    camera_ok = bool(valid and ui._reverse_camera.draw(
      rl.Rectangle(w*.18, h*.05, w*.47, h*.83), fit=True, mirror=True))
    rl.draw_rectangle(int(w*.25), int(h*.06), int(w*.31), int(h*.10), (0, 0, 0, 184))
    label('콤마4 실내 카메라', w*.405, h*.11, h*.062, WHITE, anchor='center')
    if not camera_ok:
      label('실내 카메라 영상 미수신', w*.405, h*.43, h*.065, WHITE, anchor='center')
      label('후방 영상은 차량 카메라로 확인하세요', w*.405, h*.57, h*.037, MUTED, anchor='center')
    label('실내 카메라 · 후방 시야가 아닙니다', w*.405, h*.94, h*.048, MUTED, anchor='center')
    # Preserve the existing left-hand cabin camera and its status labels.
    # Only replace the right-side text/bars with a low-overhead 2D NEXO view.
    sensors_ok = bool(valid and snapshot and fresh_service(snapshot, 'can', now, 1.5))
    front, rear, warning, warning_color, received = reverse_sensor_display(
      getattr(state, 'parking_indications', None), sensors_ok)
    self._draw_reverse_assist_panel(ui, rl, state, front, rear, warning, warning_color, received)

  @staticmethod
  def _draw_reverse_assist_panel(ui, rl, state, front, rear, warning, warning_color, received):
    """Render-only R assistant: no textures, CAN writes, model or camera changes."""
    w, h = ui.width, ui.height
    text = ui._draw_text
    px, py = w*.667, h*.060
    pw, ph = w*.321, h*.87
    rl.draw_rectangle(int(px), int(py), int(pw), int(ph), REVERSE_PANEL_BG)
    rl.draw_line_ex(rl.Vector2(px, py), rl.Vector2(px+pw, py), max(1., h*.005), REVERSE_PANEL_LINE)
    rl.draw_line_ex(rl.Vector2(px, py+ph), rl.Vector2(px+pw, py+ph), max(1., h*.005), REVERSE_PANEL_LINE)
    text('주차 보조 · R단', w*.682, h*.183, h*.080, WHITE)
    rl.draw_line_ex(rl.Vector2(w*.682, h*.231), rl.Vector2(w*.975, h*.231), max(1., h*.004),
                    REVERSE_PANEL_LINE)

    # Stable top-down car symbol. A lightweight vector replaces a 3D asset
    # and cannot trigger new GL textures or encoder reinitialisation.
    cx, cy = w*.781, h*.531
    cw, ch = h*.161, h*.335
    rl.draw_rectangle(int(cx-cw*.50+3), int(cy-ch*.5+5), int(cw), int(ch), (8, 12, 19, 255))
    rl.draw_rectangle(int(cx-cw*.50), int(cy-ch*.50), int(cw), int(ch), (173, 193, 209, 255))
    rl.draw_rectangle(int(cx-cw*.39), int(cy-ch*.34), int(cw*.78), int(ch*.20), (34, 54, 77, 255))
    rl.draw_rectangle(int(cx-cw*.39), int(cy+ch*.035), int(cw*.78), int(ch*.25), (45, 65, 84, 255))
    rl.draw_line_ex(rl.Vector2(cx-cw*.47, cy-ch*.01), rl.Vector2(cx+cw*.47, cy-ch*.01),
                    max(1., h*.006), (241, 247, 249, 255))
    for x in (cx-cw*.42, cx+cw*.42):
      rl.draw_rectangle(int(x-h*.014), int(cy-ch*.27), max(2, int(h*.028)), max(3,int(h*.11)),
                        (31, 36, 43, 255))
      rl.draw_rectangle(int(x-h*.014), int(cy+ch*.16), max(2, int(h*.028)), max(3,int(h*.11)),
                        (31, 36, 43, 255))
    # The red lamps mark the rear of the icon, not a live brake input.
    for x in (cx-cw*.34, cx+cw*.27):
      rl.draw_rectangle(int(x), int(cy+ch*.45), max(2,int(cw*.08)), max(2,int(h*.016)),
                        (255, 86, 66, 255))

    # Each colored sector corresponds to one SPAS12 left-to-right position.
    for is_front, codes in ((True, front), (False, rear)):
      for idx, code in enumerate(codes):
        offset = idx-2
        x = cx + offset*h*.078
        y = cy + (-1 if is_front else 1)*(h*.302 - abs(offset)*h*.023)
        color = SENSOR_COLORS[code] if received else SENSOR_COLORS[0]
        rl.draw_line_ex(rl.Vector2(x-h*.029, y), rl.Vector2(x+h*.029, y),
                        max(2.0, h*.026), (*color, 255))

    # Short blue strokes show steering direction only; NOT a calibrated
    # vehicle trajectory or a free-space/obstacle distance measurement.
    angle = getattr(state, 'steering_angle_deg', None)
    angle_ok = finite(angle) and abs(angle) <= 1080
    angle = float(angle) if angle_ok else 0.
    for side in (-1, 1):
      last = None
      for step in range(10):
        progress = step/9
        gx = cx + side*cw*.63 - math.copysign(1., angle)*min(abs(angle)/540., 1.)*h*.22*progress*progress
        gy = cy + ch*.56 + h*.18*progress
        next_point = rl.Vector2(gx, gy)
        if last is not None and step % 2 == 1 and angle_ok:
          rl.draw_line_ex(last, next_point, max(1., h*.009), (46, 136, 208, 255))
        last = next_point

    # Compact info column fits the existing 960 x 240 external display.
    text('전방', w*.860, h*.356, h*.055, MUTED)
    text('후방', w*.860, h*.468, h*.055, MUTED)
    for row_y, codes in ((.333, front), (.445, rear)):
      for idx, code in enumerate(codes):
        color = SENSOR_COLORS[code] if received else SENSOR_COLORS[0]
        rl.draw_rectangle(int(w*(.911+idx*.0130)), int(h*row_y),
                          max(2,int(w*.010)), max(2,int(h*.033)), (*color, 255))

    steering = ('--' if not angle_ok else
                '중앙' if abs(angle) < 5 else
                f"{'좌' if angle > 0 else '우'} {abs(angle):.0f}°")
    speed = getattr(state, 'speed_kph', None)
    speed_text = f'{abs(speed):.0f} km/h' if finite(speed) and 0 <= abs(speed) < 300 else '--'
    text('핸들', w*.858, h*.606, h*.053, MUTED)
    text(steering, w*.975, h*.606, h*.060, WHITE, anchor='right')
    text('속도', w*.858, h*.711, h*.053, MUTED)
    text(speed_text, w*.975, h*.711, h*.060, WHITE, anchor='right')

    # No invented metres, speaker alert, rear-facing camera, or AI detection.
    banner = (*warning_color, 255)
    rl.draw_rectangle(int(w*.842), int(h*.772), int(w*.143), int(h*.135),
                      (38, 49, 57, 255))
    rl.draw_rectangle(int(w*.842), int(h*.772), max(2,int(w*.004)),
                      int(h*.135), banner)
    text(warning, w*.856, h*.854, h*.053, warning_color)



def install(module, snapshot_for_scene, clock):
  cls = module.ClusterUiRenderer
  original, close = cls.render, cls.close
  def render(self, state, signal_lights=None):
    if not hasattr(self, '_jetson_gears'):
      self._jetson_gears = GearView()
    return self._jetson_gears.render(self, module.rl, original, state, signal_lights,
                                     snapshot_for_scene(), clock, time.monotonic())
  def release(self):
    if hasattr(self, '_jetson_gears'):
      self._jetson_gears.close(module.rl)
      del self._jetson_gears
    close(self)
  cls.render, cls.close = render, release
