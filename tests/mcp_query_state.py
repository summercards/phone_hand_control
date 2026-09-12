import json
import bpy
import sys
mod = sys.modules.get("phone_hand_controller")
print(json.dumps({
  "timer_registered": bpy.app.timers.is_registered(mod.controller_timer) if mod and hasattr(mod, "controller_timer") else "no-module",
  "receiver": bool(mod.STATE.receiver) if mod and hasattr(mod, "STATE") else None,
  "calibration": list(mod.STATE.calibration.keys()) if mod and hasattr(mod, "STATE") else None,
  "last_sequence": mod.STATE.last_sequence if mod and hasattr(mod, "STATE") else None,
  "filters": len(mod.STATE.filters) if mod and hasattr(mod, "STATE") else None,
  "joint_hide_render": bpy.data.objects["PHC_R_Joint_00"].hide_render,
  "joint_matrix": list(bpy.data.objects["PHC_R_Joint_00"].matrix_world.translation),
}, ensure_ascii=False))
