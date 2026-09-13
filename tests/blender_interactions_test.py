import importlib.util
import json
import sys
import time
from pathlib import Path

import bpy
from mathutils import Vector

PROJECT = Path(__file__).resolve().parents[1]
name = "phone_hand_controller"
module_path = PROJECT / "blender_addon" / name / "__init__.py"
spec = importlib.util.spec_from_file_location(name, module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
module.register()

settings = bpy.context.scene.phone_hand_control
settings.control_mode = "DEMO"
settings.hide_untracked_hands = True
settings.interaction_enabled = True
settings.pickup_enabled = True
settings.pinch_threshold = 0.35
bpy.ops.phc.create_demo_rig()
bpy.ops.phc.create_fixed_scene()
settings = bpy.context.scene.phone_hand_control

image = tuple(value for x, y in module.NEUTRAL_HAND_2D for value in (x, y, 0.0))
world = tuple(value for index in range(21) for value in (0.0, -index * 0.01, 0.0))
hand = module.HandPacket(2, 1, 1.0, image, world)
module._update_demo_hand("RIGHT", hand, time.monotonic(), settings)

left_joint = bpy.data.objects["PHC_L_Joint_08"]
right_joint = bpy.data.objects["PHC_R_Joint_08"]


def strike(object_name, now):
    obj = bpy.data.objects[object_name]
    current = [obj.location.copy() for _ in range(21)]
    previous = [obj.location + Vector((0.0, -0.20, 0.0)) for _ in range(21)]
    module.STATE.hand_positions["RIGHT"] = current
    module.STATE.hand_previous_positions["RIGHT"] = previous
    module._update_interactions([("RIGHT", hand)], settings, now)
    return obj


toggle = strike("PHC_Interactive_Toggle", time.monotonic())
bounce = strike("PHC_Interactive_Bounce", time.monotonic() + 0.1)
ball = strike("PHC_PunchBall", time.monotonic() + 0.2)

start_time = time.monotonic() + 0.5
for step in range(180):
    module._step_custom_physics(bpy.context.scene, start_time + step / 60.0)
rest_target = Vector(ball.get("phc_base_location", (0.0, -0.16, 1.72)))
punch_error = (ball.location - rest_target).length

grab_points = [Vector((10.0, 10.0, 10.0)) for _ in range(21)]
grab_points[4] = toggle.location + Vector((-0.02, 0.0, 0.0))
grab_points[8] = toggle.location + Vector((0.02, 0.0, 0.0))
module.STATE.hand_positions["RIGHT"] = [point.copy() for point in grab_points]
module.STATE.hand_previous_positions["RIGHT"] = [point.copy() for point in grab_points]
for joint_index, point in enumerate(grab_points):
    bpy.data.objects[f"PHC_R_Joint_{joint_index:02d}"].location = point
pinch_image = list(image)
pinch_image[12:15] = [0.50, 0.50, 0.0]
pinch_image[24:27] = [0.52, 0.50, 0.0]
pinch_hand = module.HandPacket(2, 1, 1.0, tuple(pinch_image), world)
module.STATE.previous_pinch["RIGHT"] = False
module._update_grab(settings, [("RIGHT", pinch_hand)], time.monotonic())
grab_acquired = module.STATE.grabbed_object_name == toggle.name and bool(toggle.get("phc_held"))
moved_points = [point.copy() for point in grab_points]
moved_points[4] += Vector((0.25, 0.0, 0.20))
moved_points[8] += Vector((0.25, 0.0, 0.20))
module.STATE.hand_previous_positions["RIGHT"] = [point.copy() for point in grab_points]
module.STATE.hand_positions["RIGHT"] = [point.copy() for point in moved_points]
for joint_index, point in enumerate(moved_points):
    bpy.data.objects[f"PHC_R_Joint_{joint_index:02d}"].location = point
module._update_grab(settings, [("RIGHT", pinch_hand)], time.monotonic() + 0.1)
grab_followed = (toggle.location - (moved_points[4] + moved_points[8]) * 0.5).length < 0.15
release_image = list(pinch_image)
release_image[24] = 0.80
release_hand = module.HandPacket(2, 1, 1.0, tuple(release_image), world)
module._update_grab(settings, [("RIGHT", release_hand)], time.monotonic() + 0.2)
grab_released = not bool(toggle.get("phc_held"))

checks = {
    "left_hidden": left_joint.hide_get() and left_joint.hide_render,
    "right_visible": not right_joint.hide_get() and not right_joint.hide_render,
    "hands_no_rigidbody": left_joint.rigid_body is None and right_joint.rigid_body is None,
    "toggle_physics": Vector(toggle.get("phc_velocity", (0.0, 0.0, 0.0))).length > 0.0,
    "bounce_triggered": bool(bounce.get("phc_active")),
    "punch_hit": bool(ball.get("phc_active")),
    "punch_recovered": punch_error < 0.10,
    "grab_acquired": grab_acquired,
    "grab_followed": grab_followed,
    "grab_released": grab_released,
}
if not all(checks.values()):
    raise RuntimeError(json.dumps({"checks": checks, "punch_error": punch_error, "grab_name": module.STATE.grabbed_object_name, "grabable": bool(toggle.get("phc_grabable")), "pinch": module._pinch_distance(pinch_hand), "point": list((grab_points[4] + grab_points[8]) * 0.5), "object": list(toggle.location), "pickup_enabled": settings.pickup_enabled, "pickup_threshold": settings.pickup_threshold, "pickup_radius": settings.pickup_radius, "previous_pinch": module.STATE.previous_pinch}, ensure_ascii=False))
print(json.dumps({"checks": checks, "punch_error": punch_error}, ensure_ascii=False))
