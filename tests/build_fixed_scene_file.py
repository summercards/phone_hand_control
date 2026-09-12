import importlib.util
import json
import sys
from pathlib import Path

import bpy

PROJECT = Path(__file__).resolve().parents[1]
module_path = PROJECT / "blender_addon" / "phone_hand_controller" / "__init__.py"
name = "phone_hand_controller"
sys.modules.pop(name, None)
spec = importlib.util.spec_from_file_location(name, module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
module.register()

settings = bpy.context.scene.phone_hand_control
settings.control_mode = "DEMO"
settings.mirror_x = True
settings.position_scale = 1.0
settings.depth_scale = 2.4
settings.hand_scale = 3.0
settings.min_cutoff = 4.0
settings.speed_boost = 0.2

bpy.ops.phc.create_demo_rig()
bpy.ops.phc.create_fixed_scene()
settings = bpy.context.scene.phone_hand_control

base = [
    (0.50, 0.78), (0.42, 0.68), (0.36, 0.60), (0.31, 0.53), (0.27, 0.47),
    (0.48, 0.59), (0.48, 0.48), (0.48, 0.38), (0.48, 0.29),
    (0.54, 0.57), (0.54, 0.45), (0.54, 0.34), (0.54, 0.24),
    (0.60, 0.59), (0.60, 0.48), (0.60, 0.38), (0.60, 0.30),
    (0.66, 0.63), (0.66, 0.53), (0.66, 0.44), (0.66, 0.37),
]
image = []
world = []
for index, (x, y) in enumerate(base):
    image.extend((x, y, 0.0))
    world.extend(((index % 5 - 2) * 0.018, -(index // 5) * 0.035, 0.0))
hand = module.HandPacket(2, 1, 1.0, tuple(image), tuple(world))
module._update_demo_hand("RIGHT", hand, module.time.monotonic(), settings)

bpy.context.scene.render.resolution_x = 1920
bpy.context.scene.render.resolution_y = 1080
bpy.context.scene.render.resolution_percentage = 100
blend_path = PROJECT / "hand_control_fixed_scene.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))

bpy.context.scene.render.resolution_x = 960
bpy.context.scene.render.resolution_y = 540
preview_path = PROJECT / "tests" / "fixed_scene_preview.png"
bpy.context.scene.render.filepath = str(preview_path)
bpy.ops.render.render(write_still=True)
print(json.dumps({
    "blend": str(blend_path),
    "preview": str(preview_path),
    "camera": bpy.context.scene.camera.name,
    "right_wrist": list(bpy.data.objects["PHC_R_Joint_00"].location),
}, ensure_ascii=False))
