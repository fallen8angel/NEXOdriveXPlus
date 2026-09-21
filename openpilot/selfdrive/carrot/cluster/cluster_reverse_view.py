"""Dedicated reverse layout, independent of normal HUD layout and CAN decoding."""
from dataclasses import replace

import pyray as rl

from cluster_reverse import sensor_style
from cluster_scene import vehicle_box


def _parking_sensor_sectors(sensors, width, height, front: bool):
    """Screen-space fan sectors for five independent SPAS positions per bumper."""
    sectors = []
    center_x, center_y = width * .445, height * .45
    base_angle = 270 if front else 90
    for sensor in sensors:
        style = sensor_style(sensor)
        if style is None:
            continue
        color, count = style
        # Spread the five positions across the bumper while pointing away from the car.
        angle = base_angle + sensor.lateral * 58
        for band in range(count):
            inner = height * 1.36 * (.19 + band * .055)
            outer = inner + height * 1.36 * .043
            sectors.append((center_x, center_y, inner, outer, angle - 10, angle + 10, color))
    return tuple(sectors)


def rear_sensor_sectors(sensors, width, height):
    return _parking_sensor_sectors(sensors, width, height, False)


def front_sensor_sectors(sensors, width, height):
    return _parking_sensor_sectors(sensors, width, height, True)


def draw_reverse_hud(renderer, state, camera_session):
    w, h = renderer.width, renderer.height
    rl.clear_background(rl.Color(10, 16, 21, 255))
    # Subtle instrument-panel surround; no decorative sensor marks when clear.
    rl.draw_rectangle_rounded(rl.Rectangle(w*.008, h*.025, w*.984, h*.95), .20, 24,
                              rl.Color(76, 88, 96, 255))
    rl.draw_rectangle_rounded(rl.Rectangle(w*.010, h*.032, w*.980, h*.936), .20, 24,
                              rl.Color(3, 8, 12, 255))
    renderer._draw_text("R", w*.085, h*.40, h*.37, (255, 43, 40), anchor="center")
    renderer._draw_text("후진 중", w*.157, h*.34, h*.065, (238, 242, 247))
    renderer._draw_text("REVERSE", w*.157, h*.44, h*.035, (146, 166, 188))
    cx, cy = w*.058, h*.73
    rl.draw_triangle(rl.Vector2(cx, cy-h*.060), rl.Vector2(cx-h*.065, cy+h*.045),
                     rl.Vector2(cx+h*.065, cy+h*.045), rl.Color(255, 65, 54, 255))
    renderer._draw_text("!", cx, cy, h*.070, (5, 9, 12), anchor="center")
    renderer._draw_text("주변을 확인하세요", w*.087, cy, h*.045, (215, 223, 231))

    parking_sectors = (
        *front_sensor_sectors(state.rear_parking.front_sensors, w, h),
        *rear_sensor_sectors(state.rear_parking.sensors, w, h),
    )
    for sx, sy, inner, outer, start, end, color in parking_sectors:
        rl.draw_ring(rl.Vector2(sx, sy), inner, outer, start, end, 24, rl.Color(*color))

    vehicle = replace(vehicle_box(0, 0, 0, 3.6, (240, 240, 240), False), brake_lights=state.brake_lights)
    viewport = (int(w*.29), int(h*.035), int(w*.31), int(h*.70))
    vx, vy, vw, vh = viewport
    rl.begin_scissor_mode(*viewport)
    rl.rl_viewport(vx, h-vy-vh, vw, vh)
    camera = rl.Camera3D(rl.Vector3(0, -10, 3.6), rl.Vector3(0, 0, .5), rl.Vector3(0, 0, 1),
                         2.8, rl.CameraProjection.CAMERA_ORTHOGRAPHIC)
    rl.begin_mode_3d(camera)
    rl.rl_push_matrix()
    # Raylib's projection uses the full target aspect, not this sub-viewport.
    rl.rl_scalef((w / vw) * (vh / h), 1.0, 1.0)
    try:
        renderer._draw_nexo_ego(vehicle)
    finally:
        rl.rl_pop_matrix()
        rl.end_mode_3d()
        rl.rl_viewport(0, 0, w, h)
        rl.end_scissor_mode()

    panel = rl.Rectangle(w*.635, h*.18, w*.34, h*.67)
    rl.draw_rectangle_rounded(rl.Rectangle(panel.x-2, panel.y-2, panel.width+4, panel.height+4),
                              .12, 20, rl.Color(126, 140, 150, 255))
    rl.draw_rectangle_rounded(panel, .12, 20, rl.Color(15, 21, 28, 255))
    renderer._draw_text("실내 카메라", w*.805, h*.11, h*.047, (227, 234, 241), anchor="center")
    camera_rect = rl.Rectangle(panel.x+8, panel.y+8, panel.width-16, panel.height-h*.11-8)
    rl.draw_rectangle_rec(camera_rect, rl.BLACK)
    rl.begin_scissor_mode(int(camera_rect.x), int(camera_rect.y), int(camera_rect.width), int(camera_rect.height))
    try:
        if not camera_session.draw(camera_rect):
            rl.draw_rectangle_rec(camera_rect, rl.BLACK)
            renderer._draw_text("카메라 연결 대기", w*.805, h*.465, h*.046,
                                (177, 187, 199), anchor="center")
    finally:
        rl.end_scissor_mode()
    renderer._draw_text("실내 상황을 확인하세요", w*.805, h*.795, h*.035,
                        (190, 202, 214), anchor="center")
