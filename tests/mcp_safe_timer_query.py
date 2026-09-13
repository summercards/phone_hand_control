import json, sys, bpy
module=sys.modules['phone_hand_controller']
print(json.dumps({'safe_registered': bpy.app.timers.is_registered(module.controller_timer_safe), 'old_registered': bpy.app.timers.is_registered(module.controller_timer), 'exception': module.STATE.last_exception, 'receiver': bool(module.STATE.receiver and module.STATE.receiver.is_alive()), 'last_packet_ns': module.STATE.last_packet_ns, 'tracked_hands': list(module.STATE.tracked_sides)}, ensure_ascii=False))
