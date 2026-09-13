import importlib.util
import json
import sys
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
settings.depth_scale = 3.0
bpy.ops.phc.create_demo_rig()
bpy.ops.phc.create_fixed_scene()
settings = bpy.context.scene.phone_hand_control


def scaled_hand(scale, shift_x=0.0):
    image = []
    world = []
    for index, (x, y) in enumerate(module.NEUTRAL_HAND_2D):
        image.extend((0.5 + (x - 0.5) * scale + shift_x, 0.5 + (y - 0.5) * scale, 0.0))
        world.extend(((index % 5) * 0.01, -(index // 5) * 0.02, 0.0))
    return module.HandPacket(2, 1, 1.0, tuple(image), tuple(world))

reference = scaled_hand(1.0, 0.08)
reference_spread = module.palm_scale(reference.image, reference.world)
calibration = {"origin": Vector((0.5, 0.5, 0.0)), "palm_spread": reference_spread}
far = module._origin_delta("RIGHT", scaled_hand(0.80, 0.08), settings, dict(calibration))
near = module._origin_delta("RIGHT", scaled_hand(1.25, 0.08), settings, dict(calibration))
checks = {
    "far_moves_away": far.y > 0.02,
    "near_moves_toward_camera": near.y < -0.02,
}
if not all(checks.values()):
    raise RuntimeError(json.dumps({"checks": checks, "near": list(near), "far": list(far)}, ensure_ascii=False))
print(json.dumps({"checks": checks, "near": list(near), "far": list(far)}, ensure_ascii=False))
