bl_info = {
    "name": "Phone Hand Control",
    "author": "Codex",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Phone Hand",
    "description": "Low-latency hand tracking from a phone browser over LAN",
    "category": "Animation",
}


import json
import math
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup
from mathutils import Matrix, Quaternion, Vector

UDP_MAGIC = b"PHC1"
PROTOCOL_VERSION = 1
UDP_HEADER = struct.Struct("<4sBBQI d")
HAND = struct.Struct("<BBf126f")
MAX_HANDS = 2
HAND_CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
)
PALM_INDEXES = (0, 5, 9, 13, 17)
FIXED_CAMERA_NAME = "PHC_FixedCamera"
FIXED_SCENE_COLLECTION = "PHC_FixedScene"
DEFAULT_CAMERA_DISTANCE = 6.5
DEFAULT_CAMERA_LENS = 50.0
DEFAULT_CAMERA_SENSOR = 36.0
DEFAULT_STAGE_Z = 1.45
DEFAULT_FRAME_WIDTH = 4.68
DEFAULT_FRAME_HEIGHT = 2.63
NEUTRAL_HAND_2D = (
    (0.50, 0.78), (0.42, 0.68), (0.36, 0.60), (0.31, 0.53), (0.27, 0.47),
    (0.48, 0.59), (0.48, 0.48), (0.48, 0.38), (0.48, 0.29),
    (0.54, 0.57), (0.54, 0.45), (0.54, 0.34), (0.54, 0.24),
    (0.60, 0.59), (0.60, 0.48), (0.60, 0.38), (0.60, 0.30),
    (0.66, 0.63), (0.66, 0.53), (0.66, 0.44), (0.66, 0.37),
)
FINGER_CHAINS = {
    "thumb": ((1, 2), (2, 3), (3, 4)),
    "index": ((5, 6), (6, 7), (7, 8)),
    "middle": ((9, 10), (10, 11), (11, 12)),
    "ring": ((13, 14), (14, 15), (15, 16)),
    "pinky": ((17, 18), (18, 19), (19, 20)),
}
FINGER_PREFIXES = {
    "thumb": ("thumb",),
    "index": ("f_index", "index"),
    "middle": ("f_middle", "middle"),
    "ring": ("f_ring", "ring"),
    "pinky": ("f_pinky", "pinky"),
}


@dataclass(slots=True)
class HandPacket:
    hand_id: int
    flags: int
    score: float
    image: tuple[float, ...]
    world: tuple[float, ...]


@dataclass(slots=True)
class PosePacket:
    sequence: int
    receive_ns: int
    capture_ms: float
    hands: tuple[HandPacket, ...]

    def side(self, side: str) -> HandPacket | None:
        wanted = 1 if side == "LEFT" else 2
        for hand in self.hands:
            if hand.hand_id == wanted:
                return hand
        return None


class LowPass:
    def __init__(self) -> None:
        self.value: float | None = None

    def reset(self) -> None:
        self.value = None

    def __call__(self, value: float, alpha: float) -> float:
        if self.value is None:
            self.value = value
        else:
            self.value = alpha * value + (1.0 - alpha) * self.value
        return self.value


class OneEuro:
    """Adaptive jitter filter: stable at rest, low lag while moving."""

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.045, d_cutoff: float = 1.0) -> None:
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.x_filter = LowPass()
        self.dx_filter = LowPass()
        self.last_value: float | None = None
        self.last_time: float | None = None

    @staticmethod
    def alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * max(cutoff, 0.0001))
        return 1.0 / (1.0 + tau / max(dt, 0.0001))

    def reset(self) -> None:
        self.x_filter.reset()
        self.dx_filter.reset()
        self.last_value = None
        self.last_time = None

    def __call__(self, value: float, timestamp: float) -> float:
        if self.last_time is None:
            dt = 1.0 / 60.0
            derivative = 0.0
        else:
            dt = max(1.0 / 240.0, min(0.1, timestamp - self.last_time))
            derivative = (value - (self.last_value or value)) / dt
        dx_alpha = self.alpha(self.d_cutoff, dt)
        derivative_hat = self.dx_filter(derivative, dx_alpha)
        cutoff = self.min_cutoff + self.beta * abs(derivative_hat)
        x_alpha = self.alpha(cutoff, dt)
        filtered = self.x_filter(value, x_alpha)
        self.last_value = value
        self.last_time = timestamp
        return filtered


class VectorOneEuro:
    def __init__(self, **kwargs) -> None:
        self.filters = (OneEuro(**kwargs), OneEuro(**kwargs), OneEuro(**kwargs))

    def reset(self) -> None:
        for item in self.filters:
            item.reset()

    def __call__(self, value: Vector, timestamp: float) -> Vector:
        return Vector(
            (
                self.filters[0](value.x, timestamp),
                self.filters[1](value.y, timestamp),
                self.filters[2](value.z, timestamp),
            )
        )

class PoseReceiver(threading.Thread):
    def __init__(self, port: int) -> None:
        super().__init__(name="PHC-Receiver", daemon=True)
        self.port = port
        self.lock = threading.Lock()
        self.latest: PosePacket | None = None
        self.packet_count = 0
        self.byte_count = 0
        self.last_error = ""
        self._stop_event = threading.Event()
        self._socket: socket.socket | None = None

    def stop(self) -> None:
        self._stop_event.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass

    def take_latest(self) -> PosePacket | None:
        with self.lock:
            value = self.latest
            self.latest = None
            return value

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket = sock
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self.port))
            sock.settimeout(0.25)
            while not self._stop_event.is_set():
                try:
                    data, address = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError as exc:
                    if not self._stop_event.is_set():
                        self.last_error = str(exc)
                    break
                if address[0] not in {"127.0.0.1", "::1"}:
                    continue
                try:
                    packet = parse_udp_pose(data)
                except ValueError as exc:
                    self.last_error = str(exc)
                    continue
                with self.lock:
                    self.latest = packet
                    self.packet_count += 1
                    self.byte_count += len(data)
        finally:
            try:
                sock.close()
            except OSError:
                pass


def parse_udp_pose(data: bytes) -> PosePacket:
    if len(data) < UDP_HEADER.size:
        raise ValueError("short header")
    magic, version, count, receive_ns, sequence, capture_ms = UDP_HEADER.unpack_from(data)
    if magic != UDP_MAGIC:
        raise ValueError("bad magic")
    if version != PROTOCOL_VERSION:
        raise ValueError("bad protocol version")
    if count > MAX_HANDS:
        raise ValueError("too many hands")
    expected = UDP_HEADER.size + count * HAND.size
    if len(data) != expected:
        raise ValueError("bad payload size")
    offset = UDP_HEADER.size
    hands: list[HandPacket] = []
    for _ in range(count):
        values = HAND.unpack_from(data, offset)
        offset += HAND.size
        hand_id, flags, score = values[:3]
        coords = values[3:]
        hands.append(HandPacket(hand_id, flags, score, tuple(coords[:63]), tuple(coords[63:126])))
    return PosePacket(sequence, receive_ns, capture_ms, tuple(hands))


def landmark(flat: tuple[float, ...], index: int) -> Vector:
    offset = index * 3
    return Vector((flat[offset], flat[offset + 1], flat[offset + 2]))


def palm_center(image: tuple[float, ...]) -> Vector:
    value = Vector((0.0, 0.0, 0.0))
    for index in PALM_INDEXES:
        value += landmark(image, index)
    return value / len(PALM_INDEXES)


def camera_frame_dimensions(settings) -> tuple[float, float]:
    camera = bpy.data.objects.get(FIXED_CAMERA_NAME)
    if camera is None or camera.type != "CAMERA":
        return DEFAULT_FRAME_WIDTH, DEFAULT_FRAME_HEIGHT
    camera_data = camera.data
    lens = max(1.0, float(settings.camera_lens or camera_data.lens))
    sensor = max(1.0, float(camera_data.sensor_width))
    forward = camera.matrix_world.to_quaternion() @ Vector((0.0, 0.0, -1.0))
    stage_center = Vector((0.0, settings.stage_origin_y, settings.stage_origin_z))
    distance = max(0.5, (stage_center - camera.matrix_world.translation).dot(forward))
    width = distance * sensor / lens
    camera_data.lens = lens
    resolution_x = max(1, bpy.context.scene.render.resolution_x)
    resolution_y = max(1, bpy.context.scene.render.resolution_y)
    height = width * resolution_y / resolution_x
    return width, height

class RuntimeState:
    def __init__(self) -> None:
        self.receiver: PoseReceiver | None = None
        self.filters: dict[tuple[str, int], VectorOneEuro] = {}
        self.origin_filters: dict[str, VectorOneEuro] = {}
        self.calibration: dict[str, dict[str, object]] = {}
        self.last_packet_ns = 0
        self.last_sequence = -1
        self.receive_times: deque[float] = deque(maxlen=120)
        self.measured_hz = 0.0
        self.last_ui_update = 0.0
        self.last_status = "未启动"
        self.target_baselines: dict[str, tuple[Vector, Quaternion]] = {}
        self.motion_history: dict[str, tuple[tuple[float, ...], float]] = {}
        self.pinch_down: dict[str, bool] = {"LEFT": False, "RIGHT": False}
        self.tracked_sides: set[str] = set()
        self.last_interaction_time = 0.0
        self.finger_bindings: dict[str, dict[str, dict[str, object]]] = {}

STATE = RuntimeState()



def _tag_redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {"VIEW_3D", "PROPERTIES"}:
                area.tag_redraw()


class PHCSettings(PropertyGroup):
    receive_port: IntProperty(name="接收端口", default=8766, min=1024, max=65535)
    control_mode: EnumProperty(
        name="驱动方式",
        items=(
            ("DEMO", "演示手模型", "驱动插件自动创建的 21 关键点手模型"),
            ("OBJECT", "对象 / 骨骼根", "驱动指定对象位置与整体旋转"),
            ("ARMATURE", "骨骼", "驱动指定骨骼位置与旋转"),
        ),
        default="DEMO",
    )
    left_object: PointerProperty(name="左手对象", type=bpy.types.Object)
    right_object: PointerProperty(name="右手对象", type=bpy.types.Object)
    left_bone: StringProperty(name="左手骨骼", default="hand.L")
    right_bone: StringProperty(name="右手骨骼", default="hand.R")
    mirror_x: BoolProperty(name="镜像 X（自然自拍方向）", default=True)
    swap_hands: BoolProperty(name="交换左右手", default=False)
    position_scale: FloatProperty(name="画面位置倍率", default=1.0, min=0.1, max=3.0)
    depth_scale: FloatProperty(name="深度位移", default=2.4, min=0.0, max=20.0)
    hand_scale: FloatProperty(name="深度/手部尺寸", default=3.0, min=0.1, max=10.0)
    stage_origin_y: FloatProperty(name="固定场景 Y", default=0.0, min=-10.0, max=10.0)
    stage_origin_z: FloatProperty(name="固定场景 Z", default=DEFAULT_STAGE_Z, min=-10.0, max=10.0)
    camera_lens: FloatProperty(name="摄像机焦距 (mm)", default=DEFAULT_CAMERA_LENS, min=18.0, max=120.0)
    lock_camera_view: BoolProperty(name="锁定 3D 视图到固定摄像机", default=True)
    hide_untracked_hands: BoolProperty(name="隐藏未捕捉的手", default=True)
    interaction_enabled: BoolProperty(name="启用手部交互", default=True)
    pinch_threshold: FloatProperty(name="捏合阈值", default=0.060, min=0.020, max=0.150)
    min_cutoff: FloatProperty(name="静止稳定强度", default=1.15, min=0.05, max=10.0)
    speed_boost: FloatProperty(name="移动跟随强度", default=0.055, min=0.0, max=1.0)
    prediction_ms: FloatProperty(name="预测补偿 (ms)", default=0.0, min=0.0, max=30.0)
    timeout_ms: FloatProperty(name="失联超时 (ms)", default=350.0, min=80.0, max=2000.0)
    auto_keyframe: BoolProperty(name="关键帧（对象/骨骼）", default=False)
    status: StringProperty(name="状态", default="未启动")
    measured_hz: StringProperty(name="接收帧率", default="0.0 Hz")
    pose_age: StringProperty(name="帧年龄", default="-- ms")
    latency: StringProperty(name="桥接到 Blender 延迟", default="-- ms")
    hand_count: StringProperty(name="手部", default="0")
    received: StringProperty(name="累计帧", default="0")


class PHC_OT_StartReceiver(Operator):
    bl_idname = "phc.start_receiver"
    bl_label = "启动手机手部接收"
    bl_description = "监听本机 UDP 端口，接收手机桥接器发来的最新手部帧"

    def execute(self, context):
        settings = context.scene.phone_hand_control
        if STATE.receiver is not None and STATE.receiver.is_alive():
            self.report({"INFO"}, "接收器已经在运行")
            return {"FINISHED"}
        STATE.receiver = PoseReceiver(settings.receive_port)
        STATE.receiver.start()
        time.sleep(0.04)
        if STATE.receiver.last_error:
            settings.status = f"启动失败: {STATE.receiver.last_error}"
            STATE.receiver = None
            self.report({"ERROR"}, settings.status)
            return {"CANCELLED"}
        STATE.last_status = f"监听 UDP {settings.receive_port}"
        settings.status = STATE.last_status
        if not bpy.app.timers.is_registered(controller_timer):
            bpy.app.timers.register(controller_timer, first_interval=0.0, persistent=True)
        self.report({"INFO"}, f"等待 Bridge 端口 {settings.receive_port}")
        return {"FINISHED"}


class PHC_OT_StopReceiver(Operator):
    bl_idname = "phc.stop_receiver"
    bl_label = "停止接收"
    bl_description = "停止 UDP 接收和模型更新"

    def execute(self, context):
        if STATE.receiver is not None:
            STATE.receiver.stop()
            STATE.receiver.join(timeout=0.3)
        STATE.receiver = None
        if bpy.app.timers.is_registered(controller_timer):
            bpy.app.timers.unregister(controller_timer)
        context.scene.phone_hand_control.status = "已停止"
        _tag_redraw()
        return {"FINISHED"}


class PHC_OT_ResetCalibration(Operator):
    bl_idname = "phc.reset_calibration"
    bl_label = "重置标定"
    bl_description = "清除手部相对位移与旋转基准"

    def execute(self, context):
        STATE.calibration.clear()
        STATE.target_baselines.clear()
        for item in STATE.filters.values():
            item.reset()
        for item in STATE.origin_filters.values():
            item.reset()
        context.scene.phone_hand_control.status = "标定已重置"
        return {"FINISHED"}


def _make_material(name: str, color: tuple[float, float, float, float], metallic=0.15, roughness=0.38):
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    material.diffuse_color = color
    material.use_nodes = True
    principled = next((node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"), None)
    if principled:
        principled.inputs["Base Color"].default_value = color
        principled.inputs["Metallic"].default_value = metallic
        principled.inputs["Roughness"].default_value = roughness
    return material


def _link_object(name: str, data, collection, material=None):
    obj = bpy.data.objects.new(name, data)
    obj["phc_generated"] = True
    if material is not None and all(existing != material for existing in obj.data.materials):
        obj.data.materials.append(material)
    collection.objects.link(obj)
    return obj


def _move_to_collection(obj, collection) -> None:
    for old_collection in list(obj.users_collection):
        old_collection.objects.unlink(obj)
    collection.objects.link(obj)


def _link_box(name: str, location, dimensions, collection, material=None):
    mesh = bpy.data.meshes.new(name + "Mesh")
    vertices = [
        (-1.0, -1.0, -1.0), (1.0, -1.0, -1.0), (1.0, 1.0, -1.0), (-1.0, 1.0, -1.0),
        (-1.0, -1.0, 1.0), (1.0, -1.0, 1.0), (1.0, 1.0, 1.0), (-1.0, 1.0, 1.0),
    ]
    faces = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (4, 0, 3, 7)]
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = _link_object(name, mesh, collection, material)
    obj.location = location
    obj.dimensions = dimensions
    obj["phc_scene_generated"] = True
    return obj


def _link_text(name: str, body: str, location, collection, material=None):
    curve = bpy.data.curves.new(name + "Curve", type="FONT")
    curve.body = body
    curve.align_x = "CENTER"
    curve.align_y = "CENTER"
    curve.size = 0.16
    obj = _link_object(name, curve, collection, material)
    obj.location = location
    obj["phc_scene_generated"] = True
    return obj


def _look_at(obj, target) -> None:
    direction = Vector(target) - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def _set_viewport_camera(settings) -> None:
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            space.region_3d.view_perspective = "CAMERA"
            space.lock_camera = settings.lock_camera_view
            area.tag_redraw()

class PHC_OT_CreateFixedScene(Operator):
    bl_idname = "phc.create_fixed_scene"
    bl_label = "创建 / 重建固定摄像机和场景"
    bl_description = "创建与手机画面坐标系对齐的固定摄像机、背景、桌面和视锥边框"

    def execute(self, context):
        settings = context.scene.phone_hand_control
        for obj in list(bpy.data.objects):
            if obj.get("phc_scene_generated"):
                data = obj.data
                bpy.data.objects.remove(obj, do_unlink=True)
                if data is not None and data.users == 0:
                    if isinstance(data, bpy.types.Mesh):
                        bpy.data.meshes.remove(data)
                    elif isinstance(data, bpy.types.Camera):
                        bpy.data.cameras.remove(data)
                    elif isinstance(data, bpy.types.Light):
                        bpy.data.lights.remove(data)
        old_collection = bpy.data.collections.get(FIXED_SCENE_COLLECTION)
        if old_collection is not None:
            bpy.data.collections.remove(old_collection)

        collection = bpy.data.collections.new(FIXED_SCENE_COLLECTION)
        context.scene.collection.children.link(collection)
        floor_material = _make_material("PHC_Scene_Floor", (0.035, 0.045, 0.065, 1.0), metallic=0.25, roughness=0.28)
        table_material = _make_material("PHC_Scene_Table", (0.12, 0.16, 0.22, 1.0), metallic=0.45, roughness=0.22)
        backdrop_material = _make_material("PHC_Scene_Backdrop", (0.025, 0.035, 0.05, 1.0), metallic=0.1, roughness=0.7)
        frame_material = _make_material("PHC_Frame_Guide", (0.05, 0.55, 1.0, 1.0), metallic=0.0, roughness=0.2)
        frame_shader = next(node for node in frame_material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
        frame_shader.inputs["Emission Color"].default_value = (0.02, 0.35, 1.0, 1.0)
        frame_shader.inputs["Emission Strength"].default_value = 4.0

        _link_box("PHC_Scene_Floor", (0.0, 0.0, -0.06), (12.0, 12.0, 0.12), collection, floor_material)
        _link_box("PHC_Scene_Backdrop", (0.0, 1.55, 2.45), (8.0, 0.12, 5.0), collection, backdrop_material)
        _link_box("PHC_Scene_Table", (0.0, -0.05, 0.60), (5.0, 2.4, 0.20), collection, table_material)

        stage_y = settings.stage_origin_y
        stage_z = settings.stage_origin_z
        frame_width, frame_height = DEFAULT_FRAME_WIDTH, DEFAULT_FRAME_HEIGHT
        bar = 0.035
        guide_y = stage_y + 0.06
        _link_box("PHC_Frame_Top", (0.0, guide_y, stage_z + frame_height * 0.5), (frame_width, bar, bar), collection, frame_material)
        _link_box("PHC_Frame_Bottom", (0.0, guide_y, stage_z - frame_height * 0.5), (frame_width, bar, bar), collection, frame_material)
        _link_box("PHC_Frame_Left", (-frame_width * 0.5, guide_y, stage_z), (bar, bar, frame_height), collection, frame_material)
        _link_box("PHC_Frame_Right", (frame_width * 0.5, guide_y, stage_z), (bar, bar, frame_height), collection, frame_material)

        interaction_specs = (
            ("Toggle", "TOGGLE", (-1.42, -0.28, 1.05), (0.42, 0.42, 0.30), (1.0, 0.22, 0.05, 1.0)),
            ("Bounce", "BOUNCE", (0.0, -0.28, 1.05), (0.46, 0.46, 0.24), (0.10, 0.95, 0.45, 1.0)),
            ("Spin", "SPIN", (1.42, -0.28, 1.10), (0.46, 0.46, 0.46), (0.10, 0.48, 1.0, 1.0)),
        )
        for label, interaction_type, location, dimensions, color in interaction_specs:
            material = _make_material("PHC_Interactive_" + label, color, metallic=0.2, roughness=0.24)
            shader = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
            shader.inputs["Emission Color"].default_value = color
            shader.inputs["Emission Strength"].default_value = 0.35
            if interaction_type == "SPIN":
                bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2, radius=0.24, location=location)
                obj = bpy.context.active_object
                obj.name = "PHC_Interactive_" + label
                _move_to_collection(obj, collection)
                obj["phc_generated"] = True
                obj["phc_scene_generated"] = True
                obj.data.materials.append(material)
            else:
                obj = _link_box("PHC_Interactive_" + label, location, dimensions, collection, material)
            obj["phc_interactive"] = True
            obj["phc_interaction_type"] = interaction_type
            obj["phc_interaction_radius"] = 0.38
            obj["phc_base_location"] = list(location)
            obj["phc_active"] = False
            obj["phc_bounce_start"] = -10.0
            obj["phc_last_update"] = 0.0
            text_obj = _link_text("PHC_Label_" + label, label.upper(), (location[0], location[1] - 0.02, location[2] + 0.46), collection, material)
            text_obj.rotation_euler.x = math.radians(90.0)

        camera_data = bpy.data.cameras.new(FIXED_CAMERA_NAME + "Data")
        camera_data.lens = settings.camera_lens
        camera_data.sensor_width = DEFAULT_CAMERA_SENSOR
        camera_data.clip_start = 0.05
        camera_data.clip_end = 100.0
        camera = bpy.data.objects.new(FIXED_CAMERA_NAME, camera_data)
        camera["phc_generated"] = True
        camera["phc_scene_generated"] = True
        collection.objects.link(camera)
        camera.location = (0.0, -DEFAULT_CAMERA_DISTANCE, stage_z)
        _look_at(camera, (0.0, stage_y, stage_z))

        key_data = bpy.data.lights.new("PHC_KeyLightData", type="AREA")
        key_data.energy = 900.0
        key_data.shape = "DISK"
        key_data.size = 4.0
        key = _link_object("PHC_KeyLight", key_data, collection)
        key["phc_scene_generated"] = True
        key.location = (-3.5, -4.0, 5.0)
        _look_at(key, (0.0, stage_y, stage_z))

        fill_data = bpy.data.lights.new("PHC_FillLightData", type="AREA")
        fill_data.energy = 500.0
        fill_data.size = 3.0
        fill = _link_object("PHC_FillLight", fill_data, collection)
        fill["phc_scene_generated"] = True
        fill.location = (4.0, -2.0, 3.0)
        _look_at(fill, (0.0, stage_y, stage_z))

        context.scene.camera = camera
        context.scene.render.resolution_x = 1920
        context.scene.render.resolution_y = 1080
        context.scene.render.resolution_percentage = 100
        context.scene.render.image_settings.file_format = "PNG"
        context.scene.render.fps = 60
        world = context.scene.world or bpy.data.worlds.new("PHC_World")
        context.scene.world = world
        world.use_nodes = True
        background = world.node_tree.nodes.get("Background")
        if background:
            background.inputs["Color"].default_value = (0.008, 0.012, 0.02, 1.0)
            background.inputs["Strength"].default_value = 0.35

        settings.stage_origin_y = stage_y
        settings.stage_origin_z = stage_z
        settings.position_scale = 1.0
        settings.depth_scale = 2.4
        settings.hand_scale = 3.0
        STATE.calibration.clear()
        for item in STATE.filters.values():
            item.reset()
        _set_viewport_camera(settings)
        settings.status = "固定摄像机和场景已创建，3D 视图已锁定"
        self.report({"INFO"}, "固定摄像机和场景已创建")
        return {"FINISHED"}

class PHC_OT_CreateDemoRig(Operator):
    bl_idname = "phc.create_demo_rig"
    bl_label = "创建 / 重建演示手模型"
    bl_description = "创建两只由 21 个 MediaPipe 关键点驱动的精细手模型"

    def execute(self, context):
        old = [obj for obj in bpy.data.objects if obj.get("phc_generated") and not obj.get("phc_scene_generated")]
        for obj in old:
            data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if data is not None and data.users == 0 and isinstance(data, bpy.types.Mesh):
                bpy.data.meshes.remove(data)
        old_collection = bpy.data.collections.get("PHC_Hands")
        if old_collection is not None:
            bpy.data.collections.remove(old_collection)

        collection = bpy.data.collections.new("PHC_Hands")
        context.scene.collection.children.link(collection)
        skin_left = _make_material("PHC_Skin_L", (0.82, 0.29, 0.18, 1.0))
        skin_right = _make_material("PHC_Skin_R", (0.16, 0.46, 0.92, 1.0))

        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2, radius=0.034, location=(0, 0, 0))
        sphere_template = context.active_object
        sphere_template.name = "PHC_SphereTemplate"
        sphere_template["phc_generated"] = True
        sphere_template.hide_render = True
        sphere_template.hide_set(True)
        bpy.ops.mesh.primitive_cylinder_add(vertices=8, radius=0.011, depth=1.0, location=(0, 0, 0))
        cylinder_template = context.active_object
        cylinder_template.name = "PHC_CylinderTemplate"
        cylinder_template["phc_generated"] = True
        cylinder_template.hide_render = True
        cylinder_template.hide_set(True)

        for side in ("L", "R"):
            material = skin_left if side == "L" else skin_right
            for index in range(21):
                joint = _link_object(
                    f"PHC_{side}_Joint_{index:02d}",
                    sphere_template.data,
                    collection,
                    material,
                )
                joint["phc_side"] = side
                joint["phc_joint"] = index
            for start, end in HAND_CONNECTIONS:
                bone = _link_object(
                    f"PHC_{side}_Bone_{start:02d}_{end:02d}",
                    cylinder_template.data,
                    collection,
                    material,
                )
                bone["phc_side"] = side
                bone["phc_bone_a"] = start
                bone["phc_bone_b"] = end
            palm_mesh = bpy.data.meshes.new(f"PHC_{side}_PalmMesh")
            palm_mesh.from_pydata([(0.0, 0.0, 0.0)] * 5, [], [(0, 1, 2), (0, 2, 3), (0, 3, 4)])
            palm_mesh.update()
            palm = _link_object(f"PHC_{side}_Palm", palm_mesh, collection, material)
            palm["phc_side"] = side
            palm["phc_palm"] = True

        for side_name in ('LEFT', 'RIGHT'):
            _apply_demo_positions(side_name, _neutral_demo_positions(side_name, context.scene.phone_hand_control))
        if context.scene.phone_hand_control.hide_untracked_hands:
            _set_demo_hand_visible('LEFT', False)
            _set_demo_hand_visible('RIGHT', False)

        context.scene.phone_hand_control.control_mode = "DEMO"
        context.scene.phone_hand_control.status = "演示手模型已创建，点击启动接收"
        self.report({"INFO"}, "已创建精细演示手模型")
        return {"FINISHED"}


def _map_world_vector(value: Vector, mirror: bool) -> Vector:
    sign = -1.0 if mirror else 1.0
    return Vector((sign * value.x, value.z, -value.y))


def _palm_quaternion(hand: HandPacket, mirror: bool) -> Quaternion:
    wrist = _map_world_vector(landmark(hand.world, 0), mirror)
    index_mcp = _map_world_vector(landmark(hand.world, 5), mirror)
    pinky_mcp = _map_world_vector(landmark(hand.world, 17), mirror)
    middle_mcp = _map_world_vector(landmark(hand.world, 9), mirror)
    across = (index_mcp - pinky_mcp).normalized()
    along = (middle_mcp - wrist).normalized()
    normal = across.cross(along).normalized()
    across = along.cross(normal).normalized()
    matrix = Matrix(
        (
            (across.x, along.x, normal.x),
            (across.y, along.y, normal.y),
            (across.z, along.z, normal.z),
        )
    )
    return matrix.to_quaternion().normalized()


class PHC_OT_Calibrate(Operator):
    bl_idname = "phc.calibrate"
    bl_label = "记录当前姿态为中立位"
    bl_description = "以当前手部姿态和电脑模型位置建立相对控制基准，可获得最自然的操作手感"

    def execute(self, context):
        settings = context.scene.phone_hand_control
        if STATE.receiver is None:
            self.report({"ERROR"}, "请先启动接收")
            return {"CANCELLED"}
        packet = STATE.receiver.take_latest()
        if packet is None or not packet.hands:
            self.report({"ERROR"}, "没有收到当前手部数据，请确认手机桥接器已连接")
            return {"CANCELLED"}
        calibrated = 0
        for side in ("LEFT", "RIGHT"):
            hand = packet.side(side)
            if hand is None:
                continue
            if settings.swap_hands:
                hand = packet.side("RIGHT" if side == "LEFT" else "LEFT")
            if hand is None:
                continue
            target = settings.left_object if side == "LEFT" else settings.right_object
            if settings.control_mode == "DEMO":
                target_location = Vector((0.0, settings.stage_origin_y, settings.stage_origin_z))
                target_rotation = Quaternion((1.0, 0.0, 0.0, 0.0))
            elif target is not None:
                target_location = target.location.copy()
                if target.rotation_mode == "QUATERNION":
                    target_rotation = target.rotation_quaternion.copy()
                else:
                    target_rotation = target.rotation_euler.to_quaternion()
            else:
                target_location = Vector((0.0, settings.stage_origin_y, settings.stage_origin_z))
                target_rotation = Quaternion((1.0, 0.0, 0.0, 0.0))
            STATE.calibration[side] = {
                "origin": (Vector((0.5, 0.5, 0.0)) if settings.control_mode == "DEMO" else palm_center(hand.image)),
                "target_location": target_location,
                "sensor_rotation": _palm_quaternion(hand, settings.mirror_x),
                "target_rotation": target_rotation,
            }
            calibrated += 1
        if calibrated == 0:
            self.report({"ERROR"}, "当前帧没有匹配的左右手")
            return {"CANCELLED"}
        for value in STATE.origin_filters.values():
            value.reset()
        settings.status = f"已标定 {calibrated} 只手"
        self.report({"INFO"}, settings.status)
        return {"FINISHED"}


def _origin_delta(hand: HandPacket, settings, calibration) -> Vector:
    current = palm_center(hand.image)
    reference = calibration.get("origin") if calibration else Vector((0.5, 0.5, 0.0))
    delta = current - reference
    sign = -1.0 if settings.mirror_x else 1.0
    frame_width, frame_height = camera_frame_dimensions(settings)
    return Vector((
        sign * delta.x * frame_width * settings.position_scale,
        -delta.z * settings.depth_scale,
        -delta.y * frame_height * settings.position_scale,
    ))


def _predict_hand(side: str, hand: HandPacket, timestamp: float, milliseconds: float) -> HandPacket:
    previous = STATE.motion_history.get(side)
    STATE.motion_history[side] = ((hand.image), timestamp)
    if previous is None or milliseconds <= 0.0:
        return hand
    previous_image, previous_time = previous
    dt = max(1.0 / 240.0, timestamp - previous_time)
    horizon = min(0.03, milliseconds / 1000.0)
    predicted = tuple(
        current + (current - old) / dt * horizon
        for current, old in zip(hand.image, previous_image)
    )
    return HandPacket(hand.hand_id, hand.flags, hand.score, predicted, hand.world)

def _filter_vector(key: tuple[str, int], value: Vector, timestamp: float, settings) -> Vector:
    filters = STATE.filters.get(key)
    if filters is None:
        filters = VectorOneEuro(min_cutoff=settings.min_cutoff, beta=settings.speed_boost)
        STATE.filters[key] = filters
    return filters(value, timestamp)


def _neutral_demo_positions(side: str, settings):
    frame_width, frame_height = camera_frame_dimensions(settings)
    anchor = Vector((0.0, settings.stage_origin_y, settings.stage_origin_z))
    sign = -1.0 if settings.mirror_x else 1.0
    offset = -1.05 if side == "LEFT" else 1.05
    positions = []
    for image_x, image_y in NEUTRAL_HAND_2D:
        positions.append(
            anchor
            + Vector(
                (
                    offset + sign * (image_x - 0.5) * frame_width,
                    0.0,
                    -(image_y - 0.5) * frame_height,
                )
            )
        )
    return positions


def _apply_demo_positions(side: str, positions) -> None:
    tag = "L" if side == "LEFT" else "R"
    for index, position in enumerate(positions):
        joint = bpy.data.objects.get(f"PHC_{tag}_Joint_{index:02d}")
        if joint is not None:
            joint.location = position
            joint.update_tag(refresh={"OBJECT"})
    for start, end in HAND_CONNECTIONS:
        bone = bpy.data.objects.get(f"PHC_{tag}_Bone_{start:02d}_{end:02d}")
        if bone is None:
            continue
        vector = positions[end] - positions[start]
        length = vector.length
        bone.location = (positions[start] + positions[end]) * 0.5
        if length > 1.0e-6:
            bone.rotation_mode = "QUATERNION"
            bone.rotation_quaternion = vector.to_track_quat("Z", "Y")
            bone.scale = (1.0, 1.0, length)
        bone.update_tag(refresh={"OBJECT"})
    palm = bpy.data.objects.get(f"PHC_{tag}_Palm")
    if palm is not None and palm.type == "MESH" and len(palm.data.vertices) == 5:
        for vertex, index in zip(palm.data.vertices, PALM_INDEXES):
            vertex.co = positions[index]
        palm.data.update()
        palm.update_tag(refresh={"DATA"})

def _set_demo_hand_visible(side: str, visible: bool) -> None:
    tag = "L" if side == "LEFT" else "R"
    for obj in bpy.data.objects:
        if obj.get("phc_side") != tag or obj.get("phc_palm") is None and obj.get("phc_joint") is None and obj.get("phc_bone_a") is None:
            continue
        obj.hide_render = not visible
        try:
            obj.hide_set(not visible)
        except RuntimeError:
            pass


def _pinch_is_down(hand: HandPacket, threshold: float) -> bool:
    thumb_tip = landmark(hand.image, 4)
    index_tip = landmark(hand.image, 8)
    return (thumb_tip - index_tip).length <= threshold


def _trigger_interaction(obj, now: float) -> None:
    interaction = str(obj.get("phc_interaction_type", ""))
    if interaction == "TOGGLE":
        obj["phc_active"] = not bool(obj.get("phc_active", False))
    elif interaction == "BOUNCE":
        obj["phc_bounce_start"] = now
    elif interaction == "SPIN":
        obj["phc_active"] = not bool(obj.get("phc_active", False))


def _update_interactions(hands, settings, now: float) -> None:
    if not settings.interaction_enabled:
        return
    index_tips = []
    for side, hand in hands:
        joint = bpy.data.objects.get(f"PHC_{'L' if side == 'LEFT' else 'R'}_Joint_08")
        if joint is not None:
            index_tips.append(joint.location.copy())
    for obj in [item for item in bpy.data.objects if item.get("phc_interactive")]:
        base_values = obj.get("phc_base_location", [obj.location.x, obj.location.y, obj.location.z])
        base = Vector(base_values)
        last_update = float(obj.get("phc_last_update", now))
        elapsed = max(0.0, min(0.1, now - last_update))
        obj["phc_last_update"] = now
        radius = float(obj.get("phc_interaction_radius", 0.32))
        hovered = False
        for point in index_tips:
            if (point - obj.location).length <= radius:
                hovered = True
                break
        interaction = str(obj.get("phc_interaction_type", ""))
        active = bool(obj.get("phc_active", False))
        if interaction == "BOUNCE":
            start = float(obj.get("phc_bounce_start", -10.0))
            progress = (now - start) / 0.65
            if 0.0 <= progress <= 1.0:
                obj.location = base + Vector((0.0, 0.0, 0.38 * math.sin(math.pi * progress)))
            else:
                obj.location = base
        elif interaction == "TOGGLE":
            obj.location = base + Vector((0.0, 0.0, 0.16 if active else 0.0))
        elif interaction == "SPIN":
            if active:
                obj.rotation_euler.z += elapsed * 2.8
            obj.location = base + Vector((0.0, 0.0, 0.10 if hovered else 0.0))
        if obj.data is not None and obj.data.materials:
            material = obj.data.materials[0]
            shader = next((node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"), None) if material.use_nodes else None
            if shader is not None:
                shader.inputs["Emission Strength"].default_value = 7.0 if hovered else (2.5 if active else 0.35)


def _update_demo_hand(side: str, hand: HandPacket, timestamp: float, settings) -> None:
    _set_demo_hand_visible(side, True)
    tag = 'L' if side == 'LEFT' else 'R'
    calibration = STATE.calibration.get(side, {})
    stage_anchor = Vector((0.0, settings.stage_origin_y, settings.stage_origin_z))
    anchor = calibration.get("target_location", stage_anchor)
    base = anchor + _origin_delta(hand, settings, calibration)
    image_reference = palm_center(hand.image)
    world_reference = landmark(hand.world, 0)
    frame_width, frame_height = camera_frame_dimensions(settings)
    sign = -1.0 if settings.mirror_x else 1.0
    positions: list[Vector] = []
    for index in range(21):
        image_local = landmark(hand.image, index) - image_reference
        world_local = landmark(hand.world, index) - world_reference
        mapped_local = Vector((
            sign * image_local.x * frame_width * settings.position_scale,
            _map_world_vector(world_local, settings.mirror_x).y * settings.hand_scale,
            -image_local.y * frame_height * settings.position_scale,
        ))
        position = _filter_vector((side, index), base + mapped_local, timestamp, settings)
        positions.append(position)
        joint = bpy.data.objects.get(f"PHC_{tag}_Joint_{index:02d}")
        if joint is not None:
            joint.location = position
            joint.update_tag(refresh={"OBJECT"})
    for (start, end) in HAND_CONNECTIONS:
        bone = bpy.data.objects.get(f"PHC_{tag}_Bone_{start:02d}_{end:02d}")
        if bone is None:
            continue
        vector = positions[end] - positions[start]
        length = vector.length
        bone.location = (positions[start] + positions[end]) * 0.5
        if length > 1.0e-6:
            bone.rotation_mode = "QUATERNION"
            bone.rotation_quaternion = vector.to_track_quat("Z", "Y")
            bone.scale = (1.0, 1.0, length)
        bone.update_tag(refresh={"OBJECT"})
    palm = bpy.data.objects.get(f"PHC_{tag}_Palm")
    if palm is not None and palm.type == "MESH" and len(palm.data.vertices) == 5:
        for vertex, index in zip(palm.data.vertices, PALM_INDEXES):
            vertex.co = positions[index]
        palm.data.update()
        palm.update_tag(refresh={"DATA"})


def _set_target_rotation(target, sensor_rotation: Quaternion, calibration) -> None:
    reference = calibration.get("sensor_rotation", sensor_rotation)
    delta = (sensor_rotation @ reference.inverted()).normalized()
    base_rotation = calibration.get("target_rotation", Quaternion((1.0, 0.0, 0.0, 0.0)))
    result = (delta @ base_rotation).normalized()
    if target.rotation_mode == "QUATERNION":
        target.rotation_quaternion = result
    else:
        target.rotation_euler = result.to_euler(target.rotation_mode)


def _update_object_hand(side: str, hand: HandPacket, settings) -> None:
    target = settings.left_object if side == "LEFT" else settings.right_object
    if target is None:
        return
    calibration = STATE.calibration.get(side)
    sensor_rotation = _palm_quaternion(hand, settings.mirror_x)
    if calibration is None:
        calibration = {
            "origin": palm_center(hand.image),
            "target_location": target.location.copy(),
            "sensor_rotation": sensor_rotation,
            "target_rotation": (
                target.rotation_quaternion.copy()
                if target.rotation_mode == "QUATERNION"
                else target.rotation_euler.to_quaternion()
            ),
        }
        STATE.calibration[side] = calibration
    target.location = calibration["target_location"] + _origin_delta(hand, settings, calibration)
    _set_target_rotation(target, sensor_rotation, calibration)
    target.update_tag(refresh={"OBJECT"})
    if settings.auto_keyframe:
        target.keyframe_insert("location", frame=bpy.context.scene.frame_current)
        if target.rotation_mode == "QUATERNION":
            target.keyframe_insert("rotation_quaternion", frame=bpy.context.scene.frame_current)
        else:
            target.keyframe_insert("rotation_euler", frame=bpy.context.scene.frame_current)


def _update_armature_hand(side: str, hand: HandPacket, settings) -> None:
    armature = settings.left_object if side == "LEFT" else settings.right_object
    bone_name = settings.left_bone if side == "LEFT" else settings.right_bone
    if armature is None or armature.type != "ARMATURE":
        return
    pose_bone = armature.pose.bones.get(bone_name)
    if pose_bone is None:
        return
    pose_bone.rotation_mode = "QUATERNION"
    sensor_rotation = _palm_quaternion(hand, settings.mirror_x)
    calibration = STATE.calibration.get(side)
    if calibration is None:
        calibration = {
            "origin": palm_center(hand.image),
            "target_location": pose_bone.location.copy(),
            "sensor_rotation": sensor_rotation,
            "target_rotation": pose_bone.rotation_quaternion.copy(),
        }
        STATE.calibration[side] = calibration
    delta_world = _origin_delta(hand, settings, calibration)
    local_delta = pose_bone.bone.matrix_local.to_3x3().inverted() @ delta_world
    pose_bone.location = calibration["target_location"] + local_delta
    _set_target_rotation(pose_bone, sensor_rotation, calibration)
    if settings.auto_keyframe:
        pose_bone.keyframe_insert("location", frame=bpy.context.scene.frame_current)
        pose_bone.keyframe_insert("rotation_quaternion", frame=bpy.context.scene.frame_current)


def _select_hand(packet: PosePacket, side: str, swap: bool) -> HandPacket | None:
    sensor_side = side
    if swap:
        sensor_side = "RIGHT" if side == "LEFT" else "LEFT"
    return packet.side(sensor_side)


def controller_timer():
    scene = bpy.context.scene
    if scene is None:
        return 1.0 / 60.0
    settings = scene.phone_hand_control
    receiver = STATE.receiver
    if receiver is None:
        return None
    packet = receiver.take_latest()
    now = time.monotonic()
    now_ns = time.monotonic_ns()
    if packet is not None:
        STATE.last_packet_ns = packet.receive_ns
        STATE.last_sequence = packet.sequence
        STATE.receive_times.append(now)
        if len(STATE.receive_times) >= 2:
            span = STATE.receive_times[-1] - STATE.receive_times[0]
            if span > 0.0:
                STATE.measured_hz = (len(STATE.receive_times) - 1) / span
        tracked_hands = []
        for side in ("LEFT", "RIGHT"):
            hand = _select_hand(packet, side, settings.swap_hands)
            if hand is None:
                STATE.pinch_down[side] = False
                if settings.control_mode == "DEMO" and settings.hide_untracked_hands:
                    _set_demo_hand_visible(side, False)
                continue
            hand = _predict_hand(side, hand, now, settings.prediction_ms)
            tracked_hands.append((side, hand))
            if settings.control_mode == "DEMO":
                _set_demo_hand_visible(side, True)
                _update_demo_hand(side, hand, now, settings)
            elif settings.control_mode == "OBJECT":
                _update_object_hand(side, hand, settings)
            elif settings.control_mode == "ARMATURE":
                _update_armature_hand(side, hand, settings)
        STATE.tracked_sides = {side for side, _hand in tracked_hands}
        if settings.control_mode == "DEMO" and settings.interaction_enabled:
            for side, hand in tracked_hands:
                pinch = _pinch_is_down(hand, settings.pinch_threshold)
                if pinch and not STATE.pinch_down[side]:
                    tip = bpy.data.objects.get(f"PHC_{'L' if side == 'LEFT' else 'R'}_Joint_08")
                    if tip is not None:
                        for obj in (item for item in bpy.data.objects if item.get("phc_interactive")):
                            if (tip.location - obj.location).length <= float(obj.get("phc_interaction_radius", 0.32)):
                                _trigger_interaction(obj, now)
                STATE.pinch_down[side] = pinch
            _update_interactions(tracked_hands, settings, now)
        STATE.last_status = f"已接收 {len(packet.hands)} 只手"
    elif STATE.last_packet_ns and (now_ns - STATE.last_packet_ns) / 1_000_000 > settings.timeout_ms:
        STATE.last_status = "等待手机数据超过超时时间"
        if settings.control_mode == "DEMO" and settings.hide_untracked_hands:
            _set_demo_hand_visible("LEFT", False)
            _set_demo_hand_visible("RIGHT", False)
        STATE.tracked_sides.clear()
        STATE.pinch_down["LEFT"] = False
        STATE.pinch_down["RIGHT"] = False

    if now - STATE.last_ui_update >= 0.25:
        age_ms = (now_ns - STATE.last_packet_ns) / 1_000_000 if STATE.last_packet_ns else None
        settings.status = STATE.last_status
        settings.measured_hz = f"{STATE.measured_hz:.1f} Hz"
        settings.pose_age = f"{age_ms:.1f} ms" if age_ms is not None else "-- ms"
        settings.latency = f"{age_ms:.1f} ms" if age_ms is not None else "-- ms"
        settings.hand_count = str(len(packet.hands) if packet is not None else 0)
        settings.received = str(receiver.packet_count)
        STATE.last_ui_update = now
        _tag_redraw()
    return 1.0 / 60.0


class PHC_OT_ResetInteractions(Operator):
    bl_idname = "phc.reset_interactions"
    bl_label = "重置交互物体"
    bl_description = "恢复场景中交互物体的初始位置和状态"

    def execute(self, context):
        for obj in (item for item in bpy.data.objects if item.get("phc_interactive")):
            base = obj.get("phc_base_location", [obj.location.x, obj.location.y, obj.location.z])
            obj.location = Vector(base)
            obj["phc_active"] = False
            obj["phc_bounce_start"] = -10.0
        self.report({"INFO"}, "交互物体已重置")
        return {"FINISHED"}


class PHC_PT_Main(Panel):
    bl_label = "Phone Hand Control"
    bl_idname = "PHC_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Phone Hand"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.phone_hand_control
        status_box = layout.box()
        status_box.label(text=f"状态: {settings.status}", icon="INFO")
        row = status_box.row(align=True)
        row.label(text=f"接收: {settings.measured_hz}")
        row.label(text=f"帧年龄: {settings.pose_age}")
        row = status_box.row(align=True)
        row.label(text=f"手部: {settings.hand_count}")
        row.label(text=f"累计: {settings.received}")

        row = layout.row(align=True)
        if STATE.receiver is None:
            row.operator("phc.start_receiver", icon="PLAY")
        else:
            row.operator("phc.stop_receiver", icon="PAUSE")
        row.operator("phc.calibrate", text="中立位", icon="ORIENTATION_GIMBAL")

        scene_box = layout.box()
        scene_box.label(text="固定摄像机与场景")
        scene_box.operator("phc.create_fixed_scene", icon="CAMERA_DATA")
        scene_box.prop(settings, "camera_lens")
        scene_box.prop(settings, "lock_camera_view")
        scene_box.prop(settings, "hide_untracked_hands")
        scene_box.prop(settings, "interaction_enabled")
        scene_box.prop(settings, "pinch_threshold")
        scene_box.operator("phc.reset_interactions", icon="LOOP_BACK")

        box = layout.box()
        box.label(text="模型驱动")
        box.prop(settings, "control_mode", expand=True)
        if settings.control_mode == "DEMO":
            box.label(text="使用内置 21 点手模型", icon="MESH_DATA")
            box.operator("phc.create_demo_rig", icon="ADD")
        elif settings.control_mode == "OBJECT":
            box.prop(settings, "left_object")
            box.prop(settings, "right_object")
        else:
            box.prop(settings, "left_object")
            box.prop(settings, "right_object")
            box.prop(settings, "left_bone")
            box.prop(settings, "right_bone")

        box = layout.box()
        box.label(text="空间映射")
        box.prop(settings, "mirror_x")
        box.prop(settings, "swap_hands")
        box.prop(settings, "position_scale")
        box.prop(settings, "depth_scale")
        box.prop(settings, "hand_scale")

        box = layout.box()
        box.label(text="稳定性与录制")
        box.prop(settings, "min_cutoff")
        box.prop(settings, "speed_boost")
        box.prop(settings, "prediction_ms")
        box.prop(settings, "timeout_ms")
        box.prop(settings, "auto_keyframe")
        box.operator("phc.reset_calibration", icon="LOOP_BACK")

        box = layout.box()
        box.prop(settings, "receive_port")
        box.label(text="先启动接收，再由手机连接。", icon="QUESTION")
        box.label(text="端口失败时，每个 Blender 实例请使用不同端口。", icon="ERROR")


CLASSES = (
    PHCSettings,
    PHC_OT_StartReceiver,
    PHC_OT_StopReceiver,
    PHC_OT_ResetCalibration,
    PHC_OT_CreateFixedScene,
    PHC_OT_CreateDemoRig,
    PHC_OT_Calibrate,
    PHC_OT_ResetInteractions,
    PHC_PT_Main,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.phone_hand_control = PointerProperty(type=PHCSettings)


def unregister():
    if bpy.app.timers.is_registered(controller_timer):
        bpy.app.timers.unregister(controller_timer)
    if STATE.receiver is not None:
        STATE.receiver.stop()
        STATE.receiver.join(timeout=0.3)
    STATE.receiver = None
    del bpy.types.Scene.phone_hand_control
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
