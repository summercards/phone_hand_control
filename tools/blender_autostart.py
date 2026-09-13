import importlib.util
import json
import sys
from pathlib import Path

import addon_utils
import bpy

module_name = "phone_hand_controller"
module_path = Path(PROJECT_ROOT) / "blender_addon" / module_name / "__init__.py"
if not module_path.exists():
    raise FileNotFoundError(f"Blender add-on source not found: {module_path}")

enabled = False
try:
    addon_utils.enable(module_name, default_set=True, persistent=True)
    enabled = bool(addon_utils.check(module_name)[1])
except Exception as exc:
    print(f"addon_utils.enable failed, using direct load: {exc}")

if not enabled:
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load add-on from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module.register()
else:
    module = sys.modules[module_name]

try:
    bpy.ops.wm.save_userpref()
except Exception:
    pass

settings = bpy.context.scene.phone_hand_control
settings.control_mode = "DEMO"
has_demo_rig = any(
    obj.get("phc_generated") and not obj.get("phc_scene_generated") and str(obj.name).startswith("PHC_")
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
    "addon_enabled": bool(addon_utils.check(module_name)[1]),
    "demo_rig": True,
    "camera": bpy.context.scene.camera.name if bpy.context.scene.camera else None,
    "receiver_running": module.STATE.receiver is not None and module.STATE.receiver.is_alive(),
    "udp_port": settings.receive_port,
}, ensure_ascii=False))
