import bpy
settings = bpy.context.scene.phone_hand_control
settings.control_mode = "DEMO"
settings.left_object = None
settings.right_object = None
for obj in list(bpy.data.objects):
    if obj.get("phc_test_object"):
        bpy.data.objects.remove(obj, do_unlink=True)
bpy.ops.phc.reset_calibration()
print("restored demo mode")
