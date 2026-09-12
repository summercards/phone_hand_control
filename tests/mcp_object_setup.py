import json
import bpy

objects = []
for side, location in (("L", (-1.2, 0.0, 1.0)), ("R", (1.2, 0.0, 1.0))):
    bpy.ops.mesh.primitive_cube_add(size=0.4, location=location)
    obj = bpy.context.active_object
    obj.name = f"PHC_ObjectTest_{side}"
    obj["phc_test_object"] = True
    objects.append(obj)
settings = bpy.context.scene.phone_hand_control
settings.control_mode = "OBJECT"
settings.left_object = objects[0]
settings.right_object = objects[1]
bpy.ops.phc.reset_calibration()
print(json.dumps({"mode": settings.control_mode, "left": objects[0].name, "right": objects[1].name}))
