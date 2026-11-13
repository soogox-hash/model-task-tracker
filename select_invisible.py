# -*- coding: utf-8 -*-
"""
选出在指定镜头和帧范围内始终不可见的网格物体（Maya 2020+，Python 3）。

选择规则
--------
* 选中镜头：只检查这些镜头；未选中镜头：检查场景镜头。
* 选中网格/组：只检查其下的网格；只选镜头时仍检查全部网格。
* 可同时选中镜头和目标物体，两种选择互不冲突。

帧规则
------
* FRAMES 非空时优先使用显式帧列表，支持子帧。
* 否则检查 FRAME_START 至 FRAME_END（包含首尾），步长为 FRAME_STEP。
* FRAME_START/END 为 None 时使用播放范围；默认逐整帧检查，不再只查三帧。

判定规则
--------
* 每一帧重新计算镜头矩阵、视锥、物体几何和可见遮挡体。
* 包围盒仅用于保守剔除，不作为“可见”的证据。
* 顶点采样走快路径；采样未命中时，对表面三角形做完整视锥裁剪。
* 遮挡射线只射向位于当前视锥内的真实表面采样点。
* 任何几何/API 错误都会把物体列为“无法判定”，不会当作不可见选中。

PIXEL_EXACT 保留原参数名，但实际是“视口 ID-Buffer 模式”：逐像素检查
视口结果，并不等同于最终渲染器的严格可见性。无法关闭抗锯齿/色彩管理、
目标过多或需要独立排除透明遮挡体时，会安全退回采样模式。
"""
import math

import maya.cmds as cmds
import maya.api.OpenMaya as om

# ---------------- 可调参数 ----------------
MARGIN = 1.15               # 视锥放宽系数；>=1 更保守
FRAMES = []                 # 显式帧列表，优先级最高；支持子帧
FRAME_START = None          # 帧范围起点；None = 播放范围起点
FRAME_END = None            # 帧范围终点；None = 播放范围终点
FRAME_STEP = 1.0            # 帧步长；检查子帧可设为 0.5、0.25 等
ONLY_RENDERABLE = False     # 只统计可渲染镜头
FRUSTUM_SAMPLES = 96        # 视锥快路径的顶点采样上限
SAMPLES_PER_OBJ = 64        # 每镜头遮挡判定的表面采样上限
EXACT_FRUSTUM = True        # 快路径未命中时执行三角形/视锥精确裁剪
OCCLUSION = True            # 是否做遮挡判定
PIXEL_EXACT = False         # 视口 ID-Buffer；需要 Maya GUI
EXCLUDE_OCCLUDERS = ()      # 采样模式下不算遮挡体的物体名前缀
RAY_EPSILON = 0.01          # 遮挡射线端点容差（场景单位）
PIXEL_TOLERANCE = 1         # ID 缓冲每通道允许的 8-bit 误差

_ID_LEVELS = 43             # 43^3 个颜色；0 和末号保留，其余分配给目标
_DEFAULT_CAMERAS = ("frontShape", "sideShape", "topShape")
_DRAW_TYPES = ("mesh", "nurbsSurface", "subdiv", "gpuCache")
_DRAW_ATTRS = (
    "overrideEnabled",
    "overrideRGBColors",
    "overrideColorRGB",
    "overrideShading",
    "overrideTexturing",
    "overrideVisibility",
)


class _PixelUnavailable(Exception):
    pass


def _short(node):
    return node.rsplit("|", 1)[-1]


def _to_dag_path(obj):
    sel = om.MSelectionList()
    sel.add(obj)
    return sel.getDagPath(0)


def _parent_transform(shape):
    parents = cmds.listRelatives(shape, parent=True, fullPath=True) or []
    if not parents:
        raise RuntimeError("形状节点没有父变换：" + shape)
    return parents[0]


def _mesh_shapes(transform):
    shapes = cmds.listRelatives(
        transform, shapes=True, fullPath=True, type="mesh") or []
    return [s for s in shapes if not cmds.getAttr(s + ".intermediateObject")]


def _all_shapes(node_type):
    shapes = cmds.ls(type=node_type, long=True, allPaths=True) or []
    if node_type == "mesh":
        shapes = [s for s in shapes
                  if not cmds.getAttr(s + ".intermediateObject")]
    return shapes


def _is_camera_node(node):
    if cmds.nodeType(node) == "camera":
        return True
    return bool(cmds.listRelatives(
        node, shapes=True, fullPath=True, type="camera") or [])


def _cameras(selection):
    selected = []
    for node in selection:
        if cmds.nodeType(node) == "camera":
            selected.append(node)
        else:
            selected.extend(cmds.listRelatives(
                node, shapes=True, fullPath=True, type="camera") or [])

    candidates = selected or _all_shapes("camera")
    result = []
    for shape in dict.fromkeys(candidates):
        if _short(shape) in _DEFAULT_CAMERAS:
            continue
        if ONLY_RENDERABLE and not cmds.camera(
                shape, query=True, renderable=True):
            continue
        result.append(shape)
    return result


def _target_transforms(selection):
    roots = []
    for node in selection:
        if _is_camera_node(node):
            continue
        if cmds.nodeType(node) == "mesh":
            if not cmds.getAttr(node + ".intermediateObject"):
                roots.append(node)
        elif cmds.objectType(node, isAType="transform"):
            roots.append(node)

    targets = set()
    if roots:
        for root in roots:
            if cmds.nodeType(root) == "mesh":
                shapes = [root]
            else:
                shapes = cmds.listRelatives(
                    root, allDescendents=True, fullPath=True,
                    type="mesh") or []
            for shape in shapes:
                if not cmds.getAttr(shape + ".intermediateObject"):
                    targets.add(_parent_transform(shape))
    else:
        for shape in _all_shapes("mesh"):
            targets.add(_parent_transform(shape))
    return sorted(targets)


def _sample_frames():
    if FRAMES:
        frames = sorted(set(float(f) for f in FRAMES))
        if not frames:
            raise ValueError("FRAMES 不能为空列表以外的空帧集合")
        return frames

    start = (cmds.playbackOptions(query=True, minTime=True)
             if FRAME_START is None else float(FRAME_START))
    end = (cmds.playbackOptions(query=True, maxTime=True)
           if FRAME_END is None else float(FRAME_END))
    step = float(FRAME_STEP)
    start, end = float(start), float(end)
    if step <= 0:
        raise ValueError("FRAME_STEP 必须大于 0")
    if end < start:
        raise ValueError("FRAME_END 不能小于 FRAME_START")

    count = int(math.floor((end - start) / step + 1e-9))
    frames = [start + i * step for i in range(count + 1)]
    if not frames or abs(frames[-1] - end) > 1e-9:
        frames.append(end)  # 范围内始终包含结束帧
    return frames


def _validate_options(targets):
    if MARGIN <= 0:
        raise ValueError("MARGIN 必须大于 0")
    if FRUSTUM_SAMPLES < 1 or SAMPLES_PER_OBJ < 1:
        raise ValueError("采样数必须大于等于 1")
    if PIXEL_EXACT and len(targets) > _ID_LEVELS ** 3 - 2:
        raise ValueError("目标过多，ID-Buffer 无法分配唯一颜色")


def _display_layers(shape, transform):
    layers = []
    for node in (shape, transform):
        layers.extend(cmds.listConnections(
            node, source=True, destination=False,
            type="displayLayer") or [])
    return set(layers)


def _visible_to_camera(shape):
    """当前帧的基础相机可见性；不判断视锥和遮挡。"""
    if cmds.objExists(shape + ".intermediateObject") and cmds.getAttr(
            shape + ".intermediateObject"):
        return False
    transform = _parent_transform(shape)
    hierarchy = [shape, transform]
    hierarchy.extend(cmds.listRelatives(
        transform, allParents=True, fullPath=True) or [])
    for node in hierarchy:
        if cmds.objExists(node + ".visibility") and not cmds.getAttr(
                node + ".visibility"):
            return False
        if (cmds.objExists(node + ".overrideEnabled")
                and cmds.getAttr(node + ".overrideEnabled")
                and cmds.objExists(node + ".overrideVisibility")
                and not cmds.getAttr(node + ".overrideVisibility")):
            return False
    if cmds.objExists(shape + ".primaryVisibility") and not cmds.getAttr(
            shape + ".primaryVisibility"):
        return False
    for layer in _display_layers(shape, transform):
        if cmds.objExists(layer + ".visibility") and not cmds.getAttr(
                layer + ".visibility"):
            return False
    return True


def _target_visible(target):
    return any(_visible_to_camera(s) for s in _mesh_shapes(target))


# ---------------- 每帧镜头视锥 ----------------
def _cam_frustum(cam_shape):
    dag = _to_dag_path(cam_shape)
    fn = om.MFnCamera(dag)
    matrix = dag.inclusiveMatrix()
    near = float(fn.nearClippingPlane)
    far = float(fn.farClippingPlane)
    width = float(cmds.getAttr("defaultResolution.width"))
    height = float(cmds.getAttr("defaultResolution.height"))
    pixel_aspect = 1.0
    if cmds.objExists("defaultResolution.pixelAspect"):
        pixel_aspect = float(cmds.getAttr("defaultResolution.pixelAspect"))
    if width <= 0 or height <= 0 or pixel_aspect <= 0:
        raise RuntimeError("渲染分辨率或像素宽高比无效")
    # getFilmFrustum 返回尺寸/偏移，并非左右上下边界；渲染视锥才直接
    # 返回 (left, right, bottom, top)，且会处理 film fit 和画面宽高比。
    left, right, bottom, top = fn.getRenderingFrustum(
        width * pixel_aspect / height)
    left, right = float(left), float(right)
    bottom, top = float(bottom), float(top)
    if near <= 0 or far < near:
        raise RuntimeError("镜头裁剪面无效：" + cam_shape)

    cx, cy = (left + right) * 0.5, (bottom + top) * 0.5
    hx = (right - left) * 0.5 * MARGIN
    hy = (top - bottom) * 0.5 * MARGIN
    left, right = cx - hx, cx + hx
    bottom, top = cy - hy, cy + hy
    ortho = bool(cmds.camera(cam_shape, query=True, orthographic=True))
    if not ortho:
        left, right = left / near, right / near
        bottom, top = bottom / near, top / near

    translation = om.MTransformationMatrix(matrix).translation(om.MSpace.kWorld)
    direction = om.MVector(0, 0, -1) * matrix
    direction.normalize()
    return {
        "matrix": matrix,
        "inverse": matrix.inverse(),
        "left": left,
        "right": right,
        "bottom": bottom,
        "top": top,
        "near": near,
        "far": far,
        "ortho": ortho,
        "eye": om.MPoint(translation.x, translation.y, translation.z),
        "direction": direction,
        "name": cam_shape,
    }


def _camera_point(point, frustum):
    p = point * frustum["inverse"]
    return p.x, p.y, -p.z


def _world_point(camera_point, frustum):
    p = om.MPoint(camera_point[0], camera_point[1], -camera_point[2])
    return p * frustum["matrix"]


def _plane_distance(point, plane, frustum):
    x, y, z = point
    if plane == 0:
        return z - frustum["near"]
    if plane == 1:
        return frustum["far"] - z
    if plane == 2:
        return x - (frustum["left"] if frustum["ortho"]
                    else frustum["left"] * z)
    if plane == 3:
        return ((frustum["right"] if frustum["ortho"]
                 else frustum["right"] * z) - x)
    if plane == 4:
        return y - (frustum["bottom"] if frustum["ortho"]
                    else frustum["bottom"] * z)
    return ((frustum["top"] if frustum["ortho"]
             else frustum["top"] * z) - y)


def _camera_point_inside(point, frustum, epsilon=1e-7):
    return all(_plane_distance(point, plane, frustum) >= -epsilon
               for plane in range(6))


def _point_in_frustum(point, frustum):
    return _camera_point_inside(_camera_point(point, frustum), frustum)


def _clip_triangle(triangle, frustum):
    """Sutherland-Hodgman：返回三角形位于视锥内的表面片顶点。"""
    polygon = list(triangle)
    epsilon = 1e-9
    for plane in range(6):
        if not polygon:
            return []
        clipped = []
        previous = polygon[-1]
        d_previous = _plane_distance(previous, plane, frustum)
        previous_inside = d_previous >= -epsilon
        for current in polygon:
            d_current = _plane_distance(current, plane, frustum)
            current_inside = d_current >= -epsilon
            if current_inside != previous_inside:
                denominator = d_previous - d_current
                if abs(denominator) > 1e-15:
                    t = d_previous / denominator
                    intersection = tuple(
                        previous[i] + (current[i] - previous[i]) * t
                        for i in range(3))
                    clipped.append(intersection)
            if current_inside:
                clipped.append(current)
            previous = current
            d_previous = d_current
            previous_inside = current_inside
        polygon = clipped
    return polygon


def _bbox_points(obj):
    bb = cmds.xform(obj, query=True, boundingBox=True, worldSpace=True)
    if bb is None:
        return []
    x0, y0, z0, x1, y1, z1 = bb
    return [om.MPoint(x, y, z)
            for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]


def _bbox_definitely_outside(points, frustum):
    """只有所有角点位于同一视锥平面外侧时才剔除，不做可见判定。"""
    if not points:
        return False
    camera_points = [_camera_point(p, frustum) for p in points]
    for plane in range(6):
        if all(_plane_distance(p, plane, frustum) < -1e-7
               for p in camera_points):
            return True
    return False


# ---------------- 当前帧表面几何 ----------------
def _target_geometry(target, cache):
    if target in cache:
        return cache[target]
    geometry = []
    for shape in _mesh_shapes(target):
        fn = om.MFnMesh(_to_dag_path(shape))
        vertices = list(fn.getPoints(om.MSpace.kWorld))
        _counts, indices = fn.getTriangles()
        indices = list(indices)
        if len(indices) % 3:
            raise RuntimeError("三角索引损坏：" + shape)
        triangles = [tuple(indices[i:i + 3])
                     for i in range(0, len(indices), 3)]
        geometry.append((vertices, triangles))
    cache[target] = geometry
    return geometry


def _uniform_indices(count, limit):
    if count <= 0 or limit <= 0:
        return []
    if count <= limit:
        return list(range(count))
    if limit == 1:
        return [count // 2]
    # 包含首尾，避免原切片方式遗漏末尾及产生前段偏置。
    return [(i * (count - 1)) // (limit - 1) for i in range(limit)]


def _vertex_samples(geometry, limit):
    total = sum(len(vertices) for vertices, _triangles in geometry)
    result = []
    for index in _uniform_indices(total, limit):
        for vertices, _triangles in geometry:
            if index < len(vertices):
                result.append(vertices[index])
                break
            index -= len(vertices)
    return result


def _centroid_samples(geometry, limit):
    total = sum(len(triangles) for _vertices, triangles in geometry)
    result = []
    for index in _uniform_indices(total, limit):
        for vertices, triangles in geometry:
            if index < len(triangles):
                a, b, c = (vertices[i] for i in triangles[index])
                result.append(om.MPoint(
                    (a.x + b.x + c.x) / 3.0,
                    (a.y + b.y + c.y) / 3.0,
                    (a.z + b.z + c.z) / 3.0))
                break
            index -= len(triangles)
    return result


def _frustum_evidence(target, geometry, frustum, limit):
    """返回视锥内真实表面点；空列表表示此镜头视野外。"""
    if not any(triangles for _vertices, triangles in geometry):
        return []
    if _bbox_definitely_outside(_bbox_points(target), frustum):
        return []

    evidence = [p for p in _vertex_samples(geometry, FRUSTUM_SAMPLES)
                if _point_in_frustum(p, frustum)]
    if evidence or not EXACT_FRUSTUM:
        return evidence[:limit]

    # 顶点都在外部不代表表面在外部：逐个三角形裁剪，覆盖大面片、
    # 镜头位于物体内部、视锥仅切到一条边等情况。
    for vertices, triangles in geometry:
        camera_vertices = [_camera_point(p, frustum) for p in vertices]
        for triangle in triangles:
            clipped = _clip_triangle(
                [camera_vertices[i] for i in triangle], frustum)
            for point in clipped:
                evidence.append(_world_point(point, frustum))
                if len(evidence) >= limit:
                    return evidence
    return evidence


# ---------------- 遮挡 ----------------
def _build_occluders():
    result = []
    for shape in _all_shapes("mesh"):
        transform = _parent_transform(shape)
        if any(_short(transform).startswith(prefix)
               for prefix in EXCLUDE_OCCLUDERS):
            continue
        if not _visible_to_camera(shape):
            continue
        result.append((om.MFnMesh(_to_dag_path(shape)), shape, transform))
    return result


def _ray_blocked(source, destination, occluders):
    direction = om.MVector(destination - source)
    distance = direction.length()
    if distance <= RAY_EPSILON:
        return False
    direction.normalize()
    for fn, shape, _transform in occluders:
        try:
            hit = fn.closestIntersection(
                source, direction, om.MSpace.kWorld, distance, False)
        except Exception as exc:
            raise RuntimeError("遮挡射线失败：%s：%s" % (shape, exc))
        if not hit:
            continue
        ray_parameter = float(hit[1])
        if RAY_EPSILON < ray_parameter < distance - RAY_EPSILON:
            return True
    return False


def _ray_source(point, frustum):
    if not frustum["ortho"]:
        return frustum["eye"]
    depth = _camera_point(point, frustum)[2]
    return point - frustum["direction"] * max(
        depth - frustum["near"], 0.0)


def _occlusion_points(geometry, frustum, evidence):
    points = list(evidence[:SAMPLES_PER_OBJ])
    remaining = SAMPLES_PER_OBJ - len(points)
    if remaining <= 0:
        return points
    vertex_budget = max(1, remaining // 2)
    centroid_budget = remaining - vertex_budget
    candidates = _vertex_samples(geometry, vertex_budget)
    if centroid_budget:
        candidates.extend(_centroid_samples(geometry, centroid_budget))
    points.extend(p for p in candidates if _point_in_frustum(p, frustum))
    return points


def _sampling_invisible(cam_shapes, frames, targets):
    remaining = set(targets)
    unresolved = []
    for frame in frames:
        cmds.currentTime(frame)
        # 镜头位置和朝向必须在此帧重新求值，不能复用其他帧的矩阵。
        frustums = [(cam, _cam_frustum(cam)) for cam in cam_shapes]
        occluders = _build_occluders() if OCCLUSION else []
        geometry_cache = {}

        for target in targets:
            if target not in remaining:
                continue
            try:
                if not _target_visible(target):
                    continue  # 当前帧隐藏；仍可能在其他帧可见
                geometry = _target_geometry(target, geometry_cache)
                target_occluders = [o for o in occluders if o[2] != target]
                visible = False
                for _camera, frustum in frustums:
                    evidence = _frustum_evidence(
                        target, geometry, frustum,
                        SAMPLES_PER_OBJ if OCCLUSION else 1)
                    if not evidence:
                        continue
                    if not OCCLUSION:
                        visible = True
                        break
                    points = _occlusion_points(geometry, frustum, evidence)
                    if any(not _ray_blocked(_ray_source(p, frustum), p,
                                            target_occluders)
                           for p in points):
                        visible = True
                        break
                if visible:
                    # 任一镜头、任一帧可见，就不再属于“始终不可见”。
                    remaining.discard(target)
            except Exception as exc:
                remaining.discard(target)  # 出错不进入待删除选择
                unresolved.append((target, str(exc)))

        print("帧 %g：待判定 %d，无法判定 %d" %
              (frame, len(remaining), len(unresolved)))
        if not remaining:
            break
    return [t for t in targets if t in remaining], unresolved


# ---------------- 视口 ID-Buffer ----------------
def _id_rgb(index):
    levels = (
        (index // (_ID_LEVELS * _ID_LEVELS)) % _ID_LEVELS,
        (index // _ID_LEVELS) % _ID_LEVELS,
        index % _ID_LEVELS,
    )
    return tuple(level / float(_ID_LEVELS - 1) for level in levels)


def _decode_id(r, g, b):
    levels = []
    for value in (r, g, b):
        level = int(round(value * (_ID_LEVELS - 1) / 255.0))
        expected = int(round(level * 255.0 / (_ID_LEVELS - 1)))
        if abs(value - expected) > PIXEL_TOLERANCE:
            return None
        levels.append(level)
    return (levels[0] * _ID_LEVELS * _ID_LEVELS
            + levels[1] * _ID_LEVELS + levels[2])


def _snapshot_draw_attrs(shape):
    snapshot = {}
    for attr in _DRAW_ATTRS:
        plug = shape + "." + attr
        if not cmds.objExists(plug):
            continue
        value = cmds.getAttr(plug)
        if attr == "overrideColorRGB":
            value = tuple(value[0])
        snapshot[attr] = value
    return snapshot


def _restore_draw_attrs(shape, snapshot):
    for attr, value in snapshot.items():
        plug = shape + "." + attr
        try:
            if attr == "overrideColorRGB":
                cmds.setAttr(plug, *value, type="double3")
            else:
                cmds.setAttr(plug, value)
        except Exception:
            pass


def _paint_shape(shape, index):
    r, g, b = _id_rgb(index)  # 归一化 0..1，不是 0..255
    cmds.setAttr(shape + ".overrideEnabled", True)
    cmds.setAttr(shape + ".overrideRGBColors", True)
    cmds.setAttr(shape + ".overrideColorRGB", r, g, b, type="double3")
    cmds.setAttr(shape + ".overrideShading", False)
    if cmds.objExists(shape + ".overrideTexturing"):
        cmds.setAttr(shape + ".overrideTexturing", False)


def _editor_flags():
    return {
        "displayAppearance": "flatShaded",
        "displayLights": "flat",
        "textures": False,
        "useDefaultMaterial": False,
        "wireframeOnShaded": False,
        "shadows": False,
        "cmEnabled": False,
        "grid": False,
        "headsUpDisplay": False,
        "selectionHiliteDisplay": False,
    }


def _pixel_exact_invisible(cam_shapes, frames, targets):
    """成功时返回不可见列表；视口条件不满足时返回 None。"""
    snapshots = {}
    editor_old = {}
    hardware_old = {}
    original_selection = cmds.ls(selection=True, long=True) or []
    original_camera = None
    view = None
    panel = None
    try:
        if EXCLUDE_OCCLUDERS:
            raise _PixelUnavailable(
                "ID-Buffer 不能在同一 pass 中独立排除透明遮挡体")
        try:
            import maya.api.OpenMayaUI as omui
            view = omui.M3dView.active3dView()
            panel = cmds.playblast(activeEditor=True)
            original_camera = view.camera()
        except Exception as exc:
            raise _PixelUnavailable("需要活动 Maya 3D 视口：%s" % exc)

        try:
            for flag, new_value in _editor_flags().items():
                editor_old[flag] = cmds.modelEditor(
                    panel, query=True, **{flag: True})
                cmds.modelEditor(panel, edit=True, **{flag: new_value})
        except Exception as exc:
            raise _PixelUnavailable(
                "无法安全设置视口显示或色彩管理：%s" % exc)

        try:
            for attr in ("multiSampleEnable", "ssaoEnable"):
                plug = "hardwareRenderingGlobals." + attr
                if not cmds.objExists(plug):
                    raise RuntimeError("缺少属性：" + plug)
                hardware_old[attr] = cmds.getAttr(plug)
                cmds.setAttr(plug, False)
        except Exception as exc:
            raise _PixelUnavailable(
                "无法关闭多重采样或 SSAO：%s" % exc)

        draw_shapes = []
        for node_type in _DRAW_TYPES:
            draw_shapes.extend(_all_shapes(node_type))
        draw_shapes = list(dict.fromkeys(draw_shapes))
        target_ids = {target: i + 1 for i, target in enumerate(targets)}
        non_target_id = _ID_LEVELS ** 3 - 1
        cmds.select(clear=True)  # 防止选择高亮覆盖 ID 色

        for shape in draw_shapes:
            snapshots[shape] = _snapshot_draw_attrs(shape)
            transform = _parent_transform(shape)
            _paint_shape(shape, target_ids.get(transform, non_target_id))

        visible_ids = set()
        image = om.MImage()
        for frame in frames:
            cmds.currentTime(frame)
            # primaryVisibility 不影响普通视口显示，需显式同步到 ID pass。
            for shape in draw_shapes:
                old = snapshots[shape]
                originally_hidden = (
                    old.get("overrideEnabled", False)
                    and not old.get("overrideVisibility", True))
                primary_visible = (
                    not cmds.objExists(shape + ".primaryVisibility")
                    or bool(cmds.getAttr(shape + ".primaryVisibility")))
                cmds.setAttr(shape + ".overrideVisibility",
                             primary_visible and not originally_hidden)

            for camera in cam_shapes:
                try:
                    view.setCamera(_to_dag_path(camera))
                    view.refresh()
                    image = om.MImage()
                    view.readColorBuffer(image, False)
                    width, height = image.getSize()
                    pixels = image.pixels()
                    if width <= 0 or height <= 0 or len(pixels) < width * height * 4:
                        raise RuntimeError("颜色缓冲为空或尺寸无效")
                except Exception as exc:
                    raise _PixelUnavailable("读取视口失败：%s" % exc)

                for y in range(height):
                    row = y * width * 4
                    for x in range(width):
                        offset = row + x * 4
                        index = _decode_id(
                            pixels[offset], pixels[offset + 1], pixels[offset + 2])
                        if index is not None and 1 <= index <= len(targets):
                            visible_ids.add(index)
                    if len(visible_ids) == len(targets):
                        break
                if len(visible_ids) == len(targets):
                    break
            if len(visible_ids) == len(targets):
                break

        return [target for i, target in enumerate(targets)
                if i + 1 not in visible_ids]
    except _PixelUnavailable as exc:
        print("视口 ID-Buffer 不可用：%s；退回采样判定。" % exc)
        return None
    except Exception as exc:
        print("视口 ID-Buffer 失败：%s；退回采样判定。" % exc)
        return None
    finally:
        for shape, old in snapshots.items():
            _restore_draw_attrs(shape, old)
        for attr, value in hardware_old.items():
            try:
                cmds.setAttr("hardwareRenderingGlobals." + attr, value)
            except Exception:
                pass
        if panel:
            for flag, value in editor_old.items():
                try:
                    cmds.modelEditor(panel, edit=True, **{flag: value})
                except Exception:
                    pass
        try:
            if view is not None and original_camera is not None:
                view.setCamera(original_camera)
                view.refresh()
        except Exception:
            pass
        try:
            if original_selection:
                cmds.select(original_selection, replace=True)
            else:
                cmds.select(clear=True)
        except Exception:
            pass


def _report(invisible, unresolved):
    for target, reason in unresolved:
        print("无法判定（未选中）：%s\n    %s" % (target, reason))
    if invisible:
        cmds.select(invisible, replace=True)
        print("在指定镜头和帧范围内始终不可见的物体 %d 个（已选中）：" %
              len(invisible))
        for target in invisible:
            print("   ", target)
        try:
            cmds.inViewMessage(
                amg="<hl>%d</hl> 个物体始终不可见，已选中" % len(invisible),
                pos="midCenter", fade=True)
        except Exception:
            pass
    else:
        cmds.select(clear=True)
        print("没有发现可确认的始终不可见物体。")
    if unresolved:
        print("另有 %d 个物体无法判定，请查看上方原因。" % len(unresolved))


def _notify(title, message):
    """用消息框显示信息；无 GUI 时退回脚本编辑器输出。"""
    print(message)
    try:
        cmds.confirmDialog(title=title, message=message, button=["确定"],
                           defaultButton="确定", dismissString="确定",
                           icon="information")
    except Exception:
        pass


def select_invisible(*_args):
    """主入口：选中在指定镜头、指定帧范围内始终不可见的物体。"""
    original_time = cmds.currentTime(query=True)
    try:
        selection = cmds.ls(selection=True, long=True) or []
        cam_shapes = _cameras(selection)
        if not cam_shapes:
            cmds.warning("没有可用镜头")
            return []
        targets = _target_transforms(selection)
        if not targets:
            cmds.warning("没有参与判定的网格物体")
            return []
        frames = _sample_frames()
        _validate_options(targets)
        _notify("不可见物体判定",
                "镜头 %d 个\n帧 %d 个（%g 至 %g）\n候选物体 %d 个" %
                (len(cam_shapes), len(frames), frames[0], frames[-1],
                 len(targets)))

        invisible = None
        unresolved = []
        if PIXEL_EXACT:
            invisible = _pixel_exact_invisible(cam_shapes, frames, targets)
            if invisible is not None:
                print("视口 ID-Buffer 判定完成。")

        if invisible is None:
            invisible, unresolved = _sampling_invisible(
                cam_shapes, frames, targets)
        _report(invisible, unresolved)
        return invisible
    except Exception as exc:
        cmds.warning("不可见物体判定已中止：%s" % exc)
        return []
    finally:
        # 无论成功、失败、GUI 退回或用户异常，都恢复原始时间。
        try:
            cmds.currentTime(original_time)
        except Exception:
            pass


if __name__ == "__main__":
    select_invisible()
