import importlib.util
import json
import sys
import time
from pathlib import Path

import bpy

PROJECT = Path(__file__).resolve().parents[1]
name = "phone_hand_controller"
module_path = PROJECT / "blender_addon" / name / "__init__.py"
spec = importlib.util.spec_from_file_location(name, module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
module.register()

bpy.ops.phc.create_fixed_scene()
platform = bpy.data.objects.get("PHC_Scene_Platform")
if platform is not None:
    bpy.data.objects.remove(platform, do_unlink=True)
obj = bpy.data.objects["PHC_Interactive_Toggle"]
obj.location.z = 2.0
obj["phc_velocity"] = [0.0, 0.0, 0.0]
start = time.monotonic()
for step in range(180):
    module._step_custom_physics(bpy.context.scene, start + step / 60.0)
checks = {
    "deletion_safe": obj.location.z < 0.40,
    "floor_height": round(obj.location.z, 3),
}
if not checks["deletion_safe"]:
    raise RuntimeError(json.dumps(checks))
print(json.dumps(checks))
