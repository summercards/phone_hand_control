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
checks = {
    "left_hidden": left_joint.hide_get() and left_joint.hide_render,
    "right_visible": not right_joint.hide_get() and not right_joint.hide_render,
    "hands_no_rigidbody": left_joint.rigid_body is None and right_joint.rigid_body is None,
}

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
spin = strike("PHC_Interactive_Spin", time.monotonic() + 0.2)
ball = strike("PHC_PunchBall", time.monotonic() + 0.3)

start_time = time.monotonic() + 0.5
for step in range(180):
    module._step_custom_physics(bpy.context.scene, start_time + step / 60.0)
rest_target = Vector(ball.get("phc_base_location", (1.55, -0.20, 1.69)))
punch_error = (ball.location - rest_target).length
punch_recovered = punch_error < 0.10
print("punch_error", punch_error)

checks.update({
    "punch_recovered": punch_recovered,
    "toggle_physics": Vector(toggle.get("phc_velocity", (0.0, 0.0, 0.0))).length > 0.0,
    "bounce_triggered": bool(bounce.get("phc_active")),
    "spin_triggered": bool(spin.get("phc_active")),
    "punch_hit": bool(ball.get("phc_active")),
    "punch_velocity": Vector(ball.get("phc_velocity", (0.0, 0.0, 0.0))).length > 0.0,
    "collision_volumes": all(bpy.data.objects[name].get("phc_collision_shape") for name in ("PHC_Interactive_Toggle", "PHC_Interactive_Bounce", "PHC_Interactive_Spin", "PHC_PunchBall")),
})
if not all(checks.values()):
    raise RuntimeError(json.dumps(checks, ensure_ascii=False))
print(json.dumps(checks, ensure_ascii=False))
