import json
import addon_utils
import bpy

try:
    addon_utils.disable("phone_hand_controller", default_set=True)
except Exception:
    pass
addon_utils.enable("phone_hand_controller", default_set=True, persistent=True)
settings = bpy.context.scene.phone_hand_control
settings.control_mode = "DEMO"
settings.mirror_x = True
settings.position_scale = 3.0
settings.hand_scale = 3.0
settings.min_cutoff = 5.0
settings.speed_boost = 0.3
bpy.ops.phc.create_demo_rig()
bpy.ops.phc.start_receiver()
print(json.dumps({"reloaded": True, "mode": settings.control_mode}))

