import json
import bpy
settings = bpy.context.scene.phone_hand_control
right = settings.right_object
print(json.dumps({"mode": settings.control_mode, "right": right.name, "location": list(right.location), "rotation": list(right.rotation_euler), "received": settings.received}, ensure_ascii=False))
