import importlib.util
import json
import sys
import time
from pathlib import Path

import bpy

PROJECT = Path(__file__).resolve().parents[1]
name = "phone_hand_controller"
path = PROJECT / "blender_addon" / name / "__init__.py"
spec = importlib.util.spec_from_file_location(name, path)
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
module.register()

settings = bpy.context.scene.phone_hand_control
settings.control_mode = "DEMO"
settings.hide_untracked_hands = True
settings.interaction_enabled = True
bpy.ops.phc.create_demo_rig()
bpy.ops.phc.create_fixed_scene()
settings = bpy.context.scene.phone_hand_control

image = tuple(value for x, y in module.NEUTRAL_HAND_2D for value in (x, y, 0.0))
world = tuple(value for index in range(21) for value in (0.0, -index * 0.01, 0.0))
hand = module.HandPacket(2, 1, 1.0, image, world)
module._update_demo_hand("RIGHT", hand, time.monotonic(), settings)
left_joint = bpy.data.objects["PHC_L_Joint_08"]
right_joint = bpy.data.objects["PHC_R_Joint_08"]

toggle = bpy.data.objects["PHC_Interactive_Toggle"]
bounce = bpy.data.objects["PHC_Interactive_Bounce"]
spin = bpy.data.objects["PHC_Interactive_Spin"]
module._trigger_interaction(toggle, time.monotonic())
module._trigger_interaction(spin, time.monotonic())
bounce["phc_bounce_start"] = time.monotonic()
module._update_interactions([("RIGHT", hand)], settings, time.monotonic() + 0.12)

checks = {
    "left_hidden": left_joint.hide_get() and left_joint.hide_render,
    "right_visible": not right_joint.hide_get() and not right_joint.hide_render,
    "toggle_active": bool(toggle.get("phc_active")),
    "toggle_lifted": toggle.location.z > toggle.get("phc_base_location")[2],
    "bounce_lifted": bounce.location.z > bounce.get("phc_base_location")[2],
    "spin_active": bool(spin.get("phc_active")),
}
if not all(checks.values()):
    raise RuntimeError(json.dumps(checks))
print(json.dumps(checks, ensure_ascii=False))
