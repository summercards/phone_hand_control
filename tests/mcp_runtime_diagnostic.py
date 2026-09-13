import json
import sys
import bpy

module = sys.modules.get("phone_hand_controller")
settings = bpy.context.scene.phone_hand_control if hasattr(bpy.types.Scene, "phone_hand_control") else None
print(json.dumps({
    "module": bool(module),
    "scene_property": settings is not None,
    "timer_registered": bool(module and bpy.app.timers.is_registered(module.controller_timer_safe)),
    "legacy_timer_registered": bool(module and bpy.app.timers.is_registered(module.controller_timer)),
    "receiver_alive": bool(module and module.STATE.receiver and module.STATE.receiver.is_alive()),
    "receiver_error": getattr(module.STATE.receiver, "last_error", None) if module and module.STATE.receiver else None,
    "last_sequence": getattr(module.STATE, "last_sequence", None) if module else None,
    "hand_count": getattr(settings, "hand_count", None) if settings else None,
    "received": getattr(settings, "received", None) if settings else None,
    "status": getattr(settings, "status", None) if settings else None,
    "last_exception": getattr(module.STATE, "last_exception", None) if module else None,
    "scene": bpy.context.scene.name,
    "frame": bpy.context.scene.frame_current,
    "rigid_world": bool(bpy.context.scene.rigidbody_world),
}, ensure_ascii=False))
