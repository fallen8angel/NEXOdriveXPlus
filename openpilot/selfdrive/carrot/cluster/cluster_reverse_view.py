"""Dedicated reverse layout, independent of normal HUD layout and CAN decoding."""
from dataclasses import replace

import pyray as rl

from cluster_reverse import sensor_style
from cluster_scene import vehicle_box


def _parking_sensor_sectors(sensors, width, height, front: bool):
    """Screen-space fan sectors for five independent SPAS positions per bumper."""
    sectors = []
    # Full-screen reverse parking view is centered on the ego vehicle.
    center_x, center_y = width * .50, height * .50
    base_angle = 270 if front else 90
    for sensor in sensors:
        style = sensor_style(sensor)
        if style is None:
            continue
        color, count = style
        # Spread the five positions across the bumper while pointing away from the car.
        # Raylib screen-space angles run opposite to the vehicle's lateral sign.
        # Mirror only the reverse-HUD fan geometry so left/right matches the
        # NEXO factory cluster: vehicle-left indications draw on screen-left.
        angle = base_angle - sensor.lateral * 58
        for band in range(count):
            inner = height * 1.36 * (.19 + band * .055)
            outer = inner + height * 1.36 * .043
            sectors.append((center_x, center_y, inner, outer, angle - 10, angle + 10, color))
    return tuple(sectors)


def rear_sensor_sectors(sensors, width, height):
    return _parking_sensor_sectors(sensors, width, height, False)


def front_sensor_sectors(sensors, width, height):
    return _parking_sensor_sectors(sensors, width, height, True)


def _inactive_sensor_guides(width, height, front: bool):
    """Subtle factory-style five-position guides; active SPAS bands draw on top."""
    sectors = []
    center_x, center_y = width * .50, height * .50
    base_angle = 270 if front else 90
    for lateral in (-.90, -.45, 0.0, .45, .90):
        angle = base_angle - lateral * 58
        inner = height * 1.36 * .19
        outer = inner + height * 1.36 * .043
        sectors.append((center_x, center_y, inner, outer, angle - 9, angle + 9))
    return tuple(sectors)


def draw_reverse_hud(renderer, state, camera_session):
    """Full-screen NEXO parking display for R: ego + all front/rear SPAS stages."""
    w, h = renderer.width, renderer.height
    # The reverse view is parking-first. Driver camera is deliberately not shown.
    # Release it if a previous build/session had it open.
    try:
        camera_session.close()
    except Exception:
        pass

    rl.clear_background(rl.Color(8, 13, 18, 255))
    rl.draw_rectangle_rounded(
        rl.Rectangle(w*.008, h*.018, w*.984, h*.964), .16, 24, rl.Color(64, 76, 86, 255)
    )
    rl.draw_rectangle_rounded(
        rl.Rectangle(w*.011, h*.026, w*.978, h*.948), .16, 24, rl.Color(3, 8, 12, 255)
    )

    # Inactive guides make front/rear sensor coverage readable even before a
    # detected stage is active, like the factory cluster. They are display-only.
    for front in (True, False):
        for sx, sy, inner, outer, start, end in _inactive_sensor_guides(w, h, front):
            rl.draw_ring(
                rl.Vector2(sx, sy), inner, outer, start, end, 24,
                rl.Color(104, 112, 120, 105),
            )

    # Draw every proven SPAS12 point independently: five front + five rear.
    # Stage 1/2/3 colors come from sensor_style: green/yellow/red.
    parking_sectors = (
        *front_sensor_sectors(state.rear_parking.front_sensors, w, h),
        *rear_sensor_sectors(state.rear_parking.sensors, w, h),
    )
    for sx, sy, inner, outer, start, end, color in parking_sectors:
        rl.draw_ring(rl.Vector2(sx, sy), inner, outer, start, end, 24, rl.Color(*color))

    vehicle = replace(
        vehicle_box(0, 0, 0, 3.6, (240, 240, 240), False),
        brake_lights=state.brake_lights,
    )
    # Large centered top-down vehicle, with room for front and rear fan sectors.
    viewport = (int(w*.31), int(h*.035), int(w*.38), int(h*.90))
    vx, vy, vw, vh = viewport
    rl.begin_scissor_mode(*viewport)
    rl.rl_viewport(vx, h-vy-vh, vw, vh)
    camera = rl.Camera3D(
        rl.Vector3(0, -10.6, 4.7),
        rl.Vector3(0, 0, .45),
        rl.Vector3(0, 0, 1),
        2.65,
        rl.CameraProjection.CAMERA_ORTHOGRAPHIC,
    )
    rl.begin_mode_3d(camera)
    rl.rl_push_matrix()
    rl.rl_scalef((w / vw) * (vh / h), 1.0, 1.0)
    try:
        renderer._draw_nexo_ego(vehicle)
    finally:
        rl.rl_pop_matrix()
        rl.end_mode_3d()
        rl.rl_viewport(0, 0, w, h)
        rl.end_scissor_mode()

    # Keep reverse status readable without taking space away from the parking map.
    renderer._draw_text("R", w*.055, h*.14, h*.16, (255, 55, 48), anchor="center")
    renderer._draw_text("후진", w*.055, h*.26, h*.040, (220, 228, 236), anchor="center")
    renderer._draw_text("주변을 확인하세요", w*.50, h*.925, h*.038, (188, 200, 211), anchor="center")
