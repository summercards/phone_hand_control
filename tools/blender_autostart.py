import json
import importlib.util
import sys
from pathlib import Path

import addon_utils
import bpy

module_name = "phone_hand_controller"
module_path = Path(PROJECT_ROOT) / "blender_addon" / "phone_hand_controller" / "__init__.py"
if not module_path.exists():
    raise FileNotFoundError(f"Blender add-on source not found: {module_path}")

try:
    addon_utils.disable(module_name, default_set=True)
except Exception:
    pass

old_module = sys.modules.pop(module_name, None)
if old_module is not None and hasattr(old_module, "unregister"):
    try:
        old_module.unregister()
    except Exception:
        pass

spec = importlib.util.spec_from_file_location(module_name, module_path)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Cannot load add-on from {module_path}")
module = importlib.util.module_from_spec(spec)
sys.modules[module_name] = module
spec.loader.exec_module(module)
module.register()

settings = bpy.context.scene.phone_hand_control
settings.control_mode = "DEMO"
has_demo_rig = any(
    obj.get("phc_generated") and str(obj.name).startswith("PHC_")
    for obj in bpy.data.objects
)
if not has_demo_rig:
    bpy.ops.phc.create_demo_rig()

bpy.ops.phc.create_fixed_scene()
settings = bpy.context.scene.phone_hand_control

if module.STATE.receiver is None or not module.STATE.receiver.is_alive():
    bpy.ops.phc.start_receiver()

print(json.dumps({
    "blender": bpy.app.version_string,
    "addon_source": str(module_path),
    "demo_rig": True,
    "camera": bpy.context.scene.camera.name if bpy.context.scene.camera else None,
    "receiver_running": module.STATE.receiver is not None and module.STATE.receiver.is_alive(),
    "udp_port": settings.receive_port,
}, ensure_ascii=False))
