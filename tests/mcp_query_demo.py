import json
import bpy

settings = bpy.context.scene.phone_hand_control
joint = bpy.data.objects.get("PHC_R_Joint_00")
bone = bpy.data.objects.get("PHC_R_Bone_00_01")
palm = bpy.data.objects.get("PHC_R_Palm")
print(json.dumps({
    "status": settings.status,
    "received": settings.received,
    "hz": settings.measured_hz,
    "age": settings.pose_age,
    "joint_location": list(joint.location) if joint else None,
    "joint_scale": list(joint.scale) if joint else None,
    "bone_location": list(bone.location) if bone else None,
    "palm_vertex_0": list(palm.data.vertices[0].co) if palm else None,
}, ensure_ascii=False))
