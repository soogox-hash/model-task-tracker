# -*- coding: utf-8 -*-
"""
选出在指定镜头和帧范围内始终不可见的网格物体（Maya 2020+，Python 3）。

用法
----
运行脚本打开检测面板，点击「开始」后才统计场景中的镜头、模型、帧数，
并执行判定。也可以直接调用 select_invisible() 走脚本编辑器流程。

选择规则
--------
* 选中镜头：只检查这些镜头；未选中镜头：检查场景镜头。
* 选中网格/组：只检查其下的网格；只选镜头时仍检查全部网格。
* 可同时选中镜头和目标物体，两种选择互不冲突。

帧规则
------
* FRAMES 非空时优先使用显式帧列表，支持子帧。
* 否则检查 FRAME_START 至 FRAME_END（包含首尾），步长为 FRAME_STEP。
* FRAME_START/END 为 None 时使用当前帧；默认逐整帧检查，不再只查三帧。

判定规则
--------
* 每一帧重新计算镜头矩阵、视锥、物体几何和可见遮挡体。
* 包围盒仅用于保守剔除，不作为“可见”的证据。
* 顶点采样走快路径；采样未命中时，对表面三角形做完整视锥裁剪。
* 遮挡射线只射向位于当前视锥内的真实表面采样点。
* 任何几何/API 错误都会把物体列为“无法判定”，不会当作不可见。
* 检测结果按原因标色，不选中物体：画面外标红
  （invisible_check_red），画面内但被完全遮挡标紫
  （invisible_check_purple）；重复检测复用同一材质，并先还原上次标色。

PIXEL_EXACT 保留原参数名，但实际是“视口 ID-Buffer 模式”：逐像素检查
视口结果，并不等同于最终渲染器的严格可见性。无法关闭抗锯齿/色彩管理、
目标过多或需要独立排除透明遮挡体时，会安全退回采样模式。
"""
import json
import math
import os
import tempfile
import time
from array import array

import maya.cmds as cmds
import maya.api.OpenMaya as om

# ---------------- 可调参数 ----------------
VERSION = "1.1.6"           # 用于确认当前加载的是哪份脚本
MARGIN = 1.0                # 视锥放宽系数；1.0 = 严格按画面边界
FRAMES = []                 # 显式帧列表，优先级最高；支持子帧
FRAME_START = None          # 帧范围起点；None = 当前帧
FRAME_END = None            # 帧范围终点；None = 当前帧
FRAME_STEP = 1.0            # 帧步长；检查子帧可设为 0.5、0.25 等
ONLY_RENDERABLE = False     # 只统计可渲染镜头
FRUSTUM_SAMPLES = 96        # 视锥快路径的顶点采样上限
SAMPLES_PER_OBJ = 64        # 每镜头遮挡判定的表面采样上限
EXACT_FRUSTUM = True        # 快路径未命中时执行三角形/视锥精确裁剪
OCCLUSION = True            # 是否做遮挡判定
PIXEL_EXACT = False         # 视口 ID-Buffer；需要 Maya GUI
EXCLUDE_OCCLUDERS = ()      # 采样模式下不算遮挡体的物体名前缀
DEBUG_VISIBLE = False       # True = 打印“为什么算可见”的屏幕坐标证据
RAY_EPSILON = 0.01          # 遮挡射线端点容差（场景单位）
PIXEL_TOLERANCE = 1         # ID 缓冲每通道允许的 8-bit 误差
EXACT_TRIANGLE_BUDGET = 200000  # 单物体精确视锥裁剪的三角形上限
PROGRESS_EVERY = 50         # 每处理多少个物体打印一次进度
MAX_MESH_VERTICES = 300000  # 单个网格顶点上限，超过则跳过精确采样
MAX_MESH_TRIANGLES = 300000  # 单个网格三角上限
MAX_GEOMETRY_VERTICES = 800000  # 单个物体的几何顶点总预算
MAX_GEOMETRY_TRIANGLES = 800000  # 单个物体的三角总预算
TRACE_LOG = True            # 写入逐物体轨迹日志，崩溃后可定位到具体物体

_ID_LEVELS = 43             # 43^3 个颜色；0 和末号保留，其余分配给目标
_MATERIALS = {
    "red": ("invisible_check_red", (1.0, 0.0, 0.0)),
    "purple": ("invisible_check_purple", (0.62, 0.13, 0.87)),
}
_RED_STATE = "invisibleCheckState"           # 记录原材质，供下次检测恢复
_DEFAULT_CAMERAS = ("perspShape", "frontShape", "sideShape", "topShape")
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


def _is_default_camera(shape):
    """只排除 Maya 自带的那四个相机（persp/front/side/top）。

    带编号的相机（persp1、persp2、perspShape1 等）都是用户新建的，
    必须参与判定，不能一并排除。
    """
    return _short(shape) in _DEFAULT_CAMERAS


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
        if _is_default_camera(shape):
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


def _sample_frames(start=None, end=None, step=None, frames=None):
    if frames is None:
        frames = FRAMES
    if frames:
        values = sorted(set(float(f) for f in frames))
        if not values:
            raise ValueError("FRAMES 不能为空列表以外的空帧集合")
        return values

    start = FRAME_START if start is None else start
    end = FRAME_END if end is None else end
    if start is None or end is None:
        current = float(cmds.currentTime(query=True))
        start = current if start is None else start
        end = current if end is None else end
    step = float(FRAME_STEP if step is None else step)
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
def _perspective_extents(fn, aspect):
    """由焦距、胶片孔径和 film fit 计算 (tanH, tanV)。

    直接算比 getRenderingFrustum 更可控：后者返回值的单位随版本变化，
    曾导致视锥放宽数倍、画面外物体被误判为画面内。
    """
    focal = float(fn.focalLength)
    if focal <= 0:
        raise RuntimeError("焦距无效")
    hfa = float(fn.horizontalFilmAperture) * 25.4
    vfa = float(fn.verticalFilmAperture) * 25.4
    if hfa <= 0 or vfa <= 0:
        raise RuntimeError("胶片孔径无效")
    tan_h = (hfa * 0.5) / focal
    tan_v = (vfa * 0.5) / focal
    film_aspect = hfa / vfa
    fit = int(fn.filmFit)
    horizontal = int(getattr(om.MFnCamera, "kHorizontalFilmFit", 0))
    vertical = int(getattr(om.MFnCamera, "kVerticalFilmFit", 1))
    fill = int(getattr(om.MFnCamera, "kFillFilmFit", 2))
    if fit == vertical:
        tan_h = tan_v * aspect
    elif fit == fill:
        # 画面覆盖孔径：宽画面裁掉上下，窄画面裁掉左右。
        if aspect >= film_aspect:
            tan_v = tan_h / aspect
        else:
            tan_h = tan_v * aspect
    elif fit != horizontal:
        # overscan：完整露出孔径，画面多出的部分留边。
        if aspect >= film_aspect:
            tan_h = tan_v * aspect
        else:
            tan_v = tan_h / aspect
    else:
        tan_v = tan_h / aspect
    return tan_h, tan_v


def _cam_frustum(cam_shape, margin=None):
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
    aspect = width * pixel_aspect / height
    if near <= 0 or far < near:
        raise RuntimeError("镜头裁剪面无效：" + cam_shape)

    ortho = bool(cmds.camera(cam_shape, query=True, orthographic=True))
    if ortho:
        # 正交镜头：orthographicWidth 是世界单位宽度。
        half_w = float(fn.orthographicWidth) * 0.5
        half_h = half_w / aspect
    else:
        half_w, half_h = _perspective_extents(fn, aspect)
    scale = (MARGIN if margin is None else margin) * float(fn.overscan)
    half_w *= scale
    half_h *= scale
    if half_w <= 0 or half_h <= 0:
        raise RuntimeError("镜头视锥无效：" + cam_shape)
    left, right = -half_w, half_w
    bottom, top = -half_h, half_h

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


def _bounds(node):
    """世界空间包围盒；对象无效或无包围盒时返回 None，不抛错。"""
    if not node or not cmds.objExists(node):
        return None
    try:
        bb = cmds.exactWorldBoundingBox(node)
    except Exception:
        return None
    if not bb or len(bb) != 6:
        return None
    return tuple(float(v) for v in bb)


def _bbox_points(obj):
    bb = _bounds(obj)
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
_TRACE_HANDLE = None


def _trace_start():
    global _TRACE_HANDLE
    if not TRACE_LOG:
        return
    try:
        path = os.path.join(tempfile.gettempdir(),
                            "invisible_scan_trace.log")
        _TRACE_HANDLE = open(path, "w", buffering=1)
        print("逐物体轨迹日志：%s" % path)
    except Exception:
        _TRACE_HANDLE = None


def _trace(message):
    if _TRACE_HANDLE is None:
        return
    try:
        _TRACE_HANDLE.write(message + "\n")
    except Exception:
        pass


def _trace_stop():
    global _TRACE_HANDLE
    if _TRACE_HANDLE is not None:
        try:
            _TRACE_HANDLE.close()
        except Exception:
            pass
        _TRACE_HANDLE = None


def _target_geometry(target):
    """读取当前帧几何；由调用方用完即释放，避免缓存全部物体撑爆内存。

    超大网格直接返回 None：继续读取可能瞬间耗尽内存并让 Maya 崩溃，
    调用方会把这类物体按“无法确认、保留”处理。
    """
    geometry = []
    total_vertices = 0
    total_triangles = 0
    for shape in _mesh_shapes(target):
        fn = om.MFnMesh(_to_dag_path(shape))
        vertex_count = fn.numVertices
        if vertex_count == 0 or fn.numPolygons == 0:
            continue
        if (vertex_count > MAX_MESH_VERTICES
                or fn.numPolygons > MAX_MESH_TRIANGLES):
            return None
        total_vertices += vertex_count
        if total_vertices > MAX_GEOMETRY_VERTICES:
            return None
        vertices = list(fn.getPoints(om.MSpace.kWorld))
        _counts, indices = fn.getTriangles()
        total_triangles += len(indices) // 3
        if total_triangles > MAX_GEOMETRY_TRIANGLES:
            return None
        indices = array("i", indices)  # 每索引 4 字节，避免 Python int 列表爆内存
        if len(indices) % 3:
            raise RuntimeError("三角索引损坏：" + shape)
        geometry.append((vertices, indices))
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
    total = sum(len(vertices) for vertices, _indices in geometry)
    result = []
    for index in _uniform_indices(total, limit):
        for vertices, _indices in geometry:
            if index < len(vertices):
                result.append(vertices[index])
                break
            index -= len(vertices)
    return result


def _centroid_samples(geometry, limit):
    total = sum(len(indices) // 3 for _vertices, indices in geometry)
    result = []
    for index in _uniform_indices(total, limit):
        for vertices, indices in geometry:
            count = len(indices) // 3
            if index < count:
                base = index * 3
                a, b, c = (vertices[indices[base]],
                           vertices[indices[base + 1]],
                           vertices[indices[base + 2]])
                result.append(om.MPoint(
                    (a.x + b.x + c.x) / 3.0,
                    (a.y + b.y + c.y) / 3.0,
                    (a.z + b.z + c.z) / 3.0))
                break
            index -= count
    return result


def _frustum_evidence(bbox, geometry, frustum, limit, strict=False):
    """返回 (视锥内真实表面点, 是否因预算不足无法确认)。

    strict=True 时不做三角形预算限制，逐三角形完整裁剪，用于“检查
    选中物体是否在画面内”这种要求绝对准确的场景。
    """
    if not any(indices for _vertices, indices in geometry):
        return [], False
    if _bbox_definitely_outside(bbox, frustum):
        return [], False

    evidence = [p for p in _vertex_samples(geometry, FRUSTUM_SAMPLES)
                if _point_in_frustum(p, frustum)]
    if evidence or not EXACT_FRUSTUM:
        return evidence[:limit], False

    # 顶点都在外部不代表表面在外部：逐个三角形裁剪，覆盖大面片、
    # 镜头位于物体内部、视锥仅切到一条边等情况。超出预算时按“可能
    # 可见”处理，宁可保留物体，也不误选可见物体。
    budget = None if strict else EXACT_TRIANGLE_BUDGET
    for vertices, indices in geometry:
        camera_vertices = [_camera_point(p, frustum) for p in vertices]
        for base in range(0, len(indices) - 2, 3):
            if budget is not None:
                if budget <= 0:
                    return [], True
                budget -= 1
            clipped = _clip_triangle(
                [camera_vertices[indices[base + i]] for i in range(3)],
                frustum)
            for point in clipped:
                evidence.append(_world_point(point, frustum))
                if len(evidence) >= limit:
                    return evidence, False
    return evidence, False


# ---------------- 遮挡 ----------------
def _world_bounds(node):
    return _bounds(node)


def _segment_hits_bounds(source, destination, bounds, pad):
    """世界空间线段与 AABB 快速相交测试，用来剔除无关遮挡体。"""
    if bounds is None:
        return True
    src = (source.x, source.y, source.z)
    vector = (destination.x - src[0], destination.y - src[1],
              destination.z - src[2])
    low = (bounds[0] - pad, bounds[1] - pad, bounds[2] - pad)
    high = (bounds[3] + pad, bounds[4] + pad, bounds[5] + pad)
    enter, leave = 0.0, 1.0
    for axis in range(3):
        if abs(vector[axis]) < 1e-12:
            if src[axis] < low[axis] or src[axis] > high[axis]:
                return False
            continue
        first = (low[axis] - src[axis]) / vector[axis]
        second = (high[axis] - src[axis]) / vector[axis]
        if first > second:
            first, second = second, first
        enter = max(enter, first)
        leave = min(leave, second)
        if enter > leave:
            return False
    return True


def _under_transform(node, root):
    """node 是否就是 root 或位于 root 之下（DAG 长名）。"""
    if not root:
        return False
    return node == root or node.startswith(root + "|")


def _build_occluders():
    result = []
    for shape in _all_shapes("mesh"):
        try:
            transform = _parent_transform(shape)
            if any(_short(transform).startswith(prefix)
                   for prefix in EXCLUDE_OCCLUDERS):
                continue
            if not _visible_to_camera(shape):
                continue
            result.append((om.MFnMesh(_to_dag_path(shape)), shape, transform,
                           _world_bounds(shape)))
        except Exception:
            continue  # 单个异常网格不影响整体判定
    return result


def _occluder_report():
    """打印可遮挡几何的类型统计：只有 mesh 参与射线遮挡。"""
    counts = {}
    for kind in ("mesh", "nurbsSurface", "subdiv", "gpuCache"):
        usable = 0
        try:
            shapes = _all_shapes(kind)
        except Exception:
            shapes = []
        for shape in shapes:
            try:
                if _visible_to_camera(shape):
                    usable += 1
            except Exception:
                pass
        counts[kind] = usable
    print("遮挡体统计：mesh %d，nurbsSurface %d，subdiv %d，gpuCache %d"
          % (counts["mesh"], counts["nurbsSurface"], counts["subdiv"],
             counts["gpuCache"]))
    others = (counts["nurbsSurface"] + counts["subdiv"]
              + counts["gpuCache"])
    if others:
        print("注意：当前只有 mesh 能挡住射线，另有 %d 个非 mesh 几何"
              "（NURBS / 细分 / Alembic 缓存）不参与遮挡；被它们挡住的"
              "物体会被判为可见。" % others)
    return counts


class _OccluderIndex(object):
    """均匀网格：沿射线只取穿过格子里的遮挡体，避免遍历全场景。"""

    def __init__(self, occluders, resolution=48):
        self.items = [o for o in occluders if o[3] is not None]
        self.cells = {}
        self.origin = (0.0, 0.0, 0.0)
        self.cell = 1.0
        self.low = (0.0, 0.0, 0.0)
        self.high = (0.0, 0.0, 0.0)
        self.max_steps = resolution * 4
        if not self.items:
            return

        xs = [o[3][0] for o in self.items] + [o[3][3] for o in self.items]
        ys = [o[3][1] for o in self.items] + [o[3][4] for o in self.items]
        zs = [o[3][2] for o in self.items] + [o[3][5] for o in self.items]
        low = (min(xs), min(ys), min(zs))
        high = (max(xs), max(ys), max(zs))
        diagonal = math.sqrt(sum((high[i] - low[i]) ** 2 for i in range(3)))
        self.origin = low
        self.low = low
        self.high = high
        self.cell = max(diagonal / float(resolution), 1e-4)

        for item_index, item in enumerate(self.items):
            bb = item[3]
            start = [int(math.floor((bb[i] - low[i]) / self.cell))
                     for i in range(3)]
            end = [int(math.floor((bb[i + 3] - low[i]) / self.cell))
                   for i in range(3)]
            for x in range(start[0], end[0] + 1):
                for y in range(start[1], end[1] + 1):
                    for z in range(start[2], end[2] + 1):
                        self.cells.setdefault((x, y, z), []).append(
                            item_index)

    def _clip_to_scene(self, source, destination):
        """把线段裁剪到遮挡体总包围盒内，返回 (t0, t1)。"""
        origin = (source.x, source.y, source.z)
        vector = (destination.x - origin[0], destination.y - origin[1],
                  destination.z - origin[2])
        t0, t1 = 0.0, 1.0
        for axis in range(3):
            if abs(vector[axis]) < 1e-12:
                if (origin[axis] < self.low[axis]
                        or origin[axis] > self.high[axis]):
                    return None
                continue
            first = (self.low[axis] - origin[axis]) / vector[axis]
            second = (self.high[axis] - origin[axis]) / vector[axis]
            if first > second:
                first, second = second, first
            t0 = max(t0, first)
            t1 = min(t1, second)
            if t0 > t1:
                return None
        return t0, t1

    def _cells_on_segment(self, source, destination):
        clipped = self._clip_to_scene(source, destination)
        if clipped is None:
            return
        t0, t1 = clipped
        ox, oy, oz = self.origin
        cell = self.cell
        sx = (source.x + (destination.x - source.x) * t0 - ox) / cell
        sy = (source.y + (destination.y - source.y) * t0 - oy) / cell
        sz = (source.z + (destination.z - source.z) * t0 - oz) / cell
        ex = (source.x + (destination.x - source.x) * t1 - ox) / cell
        ey = (source.y + (destination.y - source.y) * t1 - oy) / cell
        ez = (source.z + (destination.z - source.z) * t1 - oz) / cell
        dx, dy, dz = ex - sx, ey - sy, ez - sz

        ix, iy, iz = int(math.floor(sx)), int(math.floor(sy)), int(
            math.floor(sz))
        step_x = 1 if dx > 0 else -1
        step_y = 1 if dy > 0 else -1
        step_z = 1 if dz > 0 else -1
        infinity = float("inf")
        delta_x = abs(1.0 / dx) if dx else infinity
        delta_y = abs(1.0 / dy) if dy else infinity
        delta_z = abs(1.0 / dz) if dz else infinity
        max_x = ((ix + (1 if dx > 0 else 0)) - sx) / dx if dx else infinity
        max_y = ((iy + (1 if dy > 0 else 0)) - sy) / dy if dy else infinity
        max_z = ((iz + (1 if dz > 0 else 0)) - sz) / dz if dz else infinity

        for _step in range(self.max_steps):
            yield ix, iy, iz
            if max_x >= 1.0 and max_y >= 1.0 and max_z >= 1.0:
                return
            if max_x <= max_y and max_x <= max_z:
                ix += step_x
                max_x += delta_x
            elif max_y <= max_z:
                iy += step_y
                max_y += delta_y
            else:
                iz += step_z
                max_z += delta_z

    def candidates(self, source, destination):
        if not self.cells:
            return ()
        seen = set()
        result = []
        for key in self._cells_on_segment(source, destination):
            for item_index in self.cells.get(key, ()):
                if item_index in seen:
                    continue
                seen.add(item_index)
                result.append(self.items[item_index])
        return result


def _hit_distance(hit, source):
    """兼容不同返回顺序，取命中点到射线起点的距离。"""
    if not hit:
        return None
    values = hit if isinstance(hit, (tuple, list)) else (hit,)
    if values and isinstance(values[0], bool):
        if not values[0]:
            return None
        values = values[1:]
    for value in values:
        if isinstance(value, (om.MPoint, om.MFloatPoint)):
            return om.MVector(value.x - source.x, value.y - source.y,
                              value.z - source.z).length()
    for value in values:
        if isinstance(value, float) and math.isfinite(value):
            return value
    return None


_HIT_MODE = None
_EMPTY_INTS = None
_EMPTY_POINTS = None


def _hit_arrays():
    """faceIds / points 所在版本要求真实数组，空数组表示全部面。"""
    global _EMPTY_INTS, _EMPTY_POINTS
    if _EMPTY_INTS is None:
        _EMPTY_INTS = om.MIntArray()
        _EMPTY_POINTS = om.MFloatPointArray()
    return _EMPTY_INTS, _EMPTY_POINTS


def _closest_hit(fn, origin, direction, distance):
    """兼容不同 Maya 版本的 closestIntersection 参数形式。"""
    global _HIT_MODE
    ids, points = _hit_arrays()
    calls = (
        # 你这版：第 3 个参数是整型空间枚举，至少 5 个位置参数
        lambda: fn.closestIntersection(origin, direction, om.MSpace.kWorld,
                                       distance, False),
        lambda: fn.closestIntersection(origin, direction, om.MSpace.kWorld,
                                       distance),
        # 标准 API 2.0：faceIds / points 必须是数组
        lambda: fn.closestIntersection(origin, direction, ids, points,
                                       om.MSpace.kWorld, distance, False),
        lambda: fn.closestIntersection(origin, direction, ids, points,
                                       om.MSpace.kWorld, distance),
        # 关键字形式
        lambda: fn.closestIntersection(origin, direction,
                                       space=om.MSpace.kWorld,
                                       maxParam=distance,
                                       testBothDirections=False),
        lambda: fn.closestIntersection(origin, direction),
    )
    if _HIT_MODE is not None:
        return calls[_HIT_MODE]()
    errors = []
    for mode, call in enumerate(calls):
        try:
            result = call()
        except TypeError as exc:
            errors.append(str(exc))
            continue
        _HIT_MODE = mode
        return result
    raise RuntimeError(
        "closestIntersection 参数不兼容：%s\n函数签名：%s"
        % ("; ".join(errors),
           (getattr(fn.closestIntersection, "__doc__", "") or "").strip()))


def _ray_blocked(source, destination, index, skip_transform):
    # MFnMesh.closestIntersection 在 API 2.0 只接受 MFloatPoint/MFloatVector。
    sx, sy, sz = source.x, source.y, source.z
    dx = destination.x - sx
    dy = destination.y - sy
    dz = destination.z - sz
    distance = math.sqrt(dx * dx + dy * dy + dz * dz)
    if distance <= RAY_EPSILON:
        return False
    origin = om.MFloatPoint(sx, sy, sz)
    direction = om.MFloatVector(dx / distance, dy / distance,
                                dz / distance)
    for fn, shape, transform, bounds in index.candidates(source, destination):
        if _under_transform(transform, skip_transform):
            continue
        # 网格只是缩小候选集，仍需包围盒确认线段确实穿过它。
        if not _segment_hits_bounds(source, destination, bounds, RAY_EPSILON):
            continue
        try:
            hit = _closest_hit(fn, origin, direction, distance)
        except Exception as exc:
            raise RuntimeError("遮挡射线失败：%s：%s" % (shape, exc))
        hit_distance = _hit_distance(hit, source)
        if (hit_distance is not None and RAY_EPSILON < hit_distance
                < distance - RAY_EPSILON):
            return True
    return False


def _ray_source(point, frustum):
    if not frustum["ortho"]:
        return frustum["eye"]
    depth = _camera_point(point, frustum)[2]
    return point - frustum["direction"] * max(
        depth - frustum["near"], 0.0)


def _screen_ratio(point, frustum):
    """采样点的归一化画面坐标；|x|、|y| <= 1 表示在画面内。"""
    c = _camera_point(point, frustum)
    right = max(abs(frustum["right"]), 1e-9)
    top = max(abs(frustum["top"]), 1e-9)
    if frustum["ortho"]:
        return c[0] / right, c[1] / top
    depth = -c[2]
    if depth <= 1e-9:
        return 9.9, 9.9
    return c[0] / depth / right, c[1] / depth / top


def _debug_visible(target, frame, frustum, point):
    sx, sy = _screen_ratio(point, frustum)
    print("[可见证据] %s | 镜头 %s | 帧 %g | 屏幕坐标 (%.2f, %.2f)"
          "  1.0 = 画面边缘" % (target, frustum["name"].split("|")[-1],
                               frame, sx, sy))


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


def _sampling_invisible(cam_shapes, frames, targets, progress=None):
    """带轨迹日志的采样判定入口，崩溃后可查看最后一个物体。"""
    _trace_start()
    try:
        return _sample_targets(cam_shapes, frames, targets, progress)
    finally:
        _trace_stop()


def _sample_targets(cam_shapes, frames, targets, progress=None):
    remaining = set(targets)
    in_frame = set()
    unresolved = []
    skipped = []
    unconfirmed = []
    total_units = max(1, len(frames) * len(targets))
    started = time.time()
    for frame_index, frame in enumerate(frames):
        _trace("帧 %s：设置时间" % frame)
        cmds.currentTime(frame)
        # 镜头位置和朝向必须在此帧重新求值，不能复用其他帧的矩阵。
        _trace("帧 %s：计算镜头矩阵" % frame)
        frustums = [(cam, _cam_frustum(cam)) for cam in cam_shapes]
        _trace("帧 %s：构建遮挡体" % frame)
        occluders = _build_occluders() if OCCLUSION else []
        _trace("帧 %s：遮挡体 %d 个" % (frame, len(occluders)))
        occluder_index = _OccluderIndex(occluders) if OCCLUSION else None
        _trace("帧 %s：遮挡索引完成" % frame)
        pending = [t for t in targets if t in remaining]
        base = frame_index * len(targets)
        _trace("帧 %s 开始，待判定 %d" % (frame, len(pending)))

        for count, target in enumerate(pending, 1):
            geometry = None
            _trace("帧 %s 物体 %s" % (frame, target))
            try:
                if not _target_visible(target):
                    continue  # 当前帧隐藏；仍可能在其他帧可见
                bbox = _bbox_points(target)  # 每帧每物体只取一次
                geometry = _target_geometry(target)
                if geometry is None:
                    # 超大网格不读取，按“无法确认、保留”处理，避免内存崩溃。
                    remaining.discard(target)
                    skipped.append(target)
                    _trace("跳过超大网格：%s" % target)
                    continue
                visible = False
                uncertain_target = False
                for _camera, frustum in frustums:
                    evidence, uncertain = _frustum_evidence(
                        bbox, geometry, frustum,
                        SAMPLES_PER_OBJ if OCCLUSION else 1)
                    if uncertain:
                        # 三角面预算不足：无法确认，保留物体，避免误选。
                        uncertain_target = True
                        break
                    if not evidence:
                        continue
                    # 进入过画面：即使被完全遮挡，也属于“被遮挡”而不是“画面外”。
                    in_frame.add(target)
                    if not OCCLUSION:
                        visible = True
                        if DEBUG_VISIBLE:
                            _debug_visible(target, frame, frustum,
                                           evidence[0])
                        break
                    points = _occlusion_points(geometry, frustum, evidence)
                    hit = next(
                        (p for p in points
                         if not _ray_blocked(_ray_source(p, frustum), p,
                                             occluder_index, target)), None)
                    if hit is not None:
                        visible = True
                        if DEBUG_VISIBLE:
                            _debug_visible(target, frame, frustum, hit)
                        break
                if uncertain_target:
                    remaining.discard(target)
                    unconfirmed.append(target)
                    _trace("三角预算不足，按可见保留：%s" % target)
                elif visible:
                    # 任一镜头、任一帧可见，就不再属于“始终不可见”。
                    remaining.discard(target)
            except Exception as exc:
                remaining.discard(target)  # 出错不进入待删除选择
                unresolved.append((target, str(exc)))
            finally:
                geometry = None  # 立即释放当前物体的几何数据
                if progress is not None:
                    done = base + count
                    progress(int(done * 100.0 / total_units), done,
                             total_units, time.time() - started)

        print("帧 %g：待判定 %d，无法判定 %d" %
              (frame, len(remaining), len(unresolved)))
        if not remaining:
            break

    if progress is not None:
        progress(100, total_units, total_units, time.time() - started)
    if skipped:
        print("网格过大、无法精确判定（已按可见保留）%d 个：" % len(skipped))
        for target in skipped[:50]:
            print("   ", target)
        _trace("超大网格跳过 %d 个" % len(skipped))
    if unconfirmed:
        print("三角形超过预算、无法确认（已按可见保留）%d 个：" %
              len(unconfirmed))
        for target in unconfirmed[:50]:
            print("   ", target)
        _trace("三角预算不足保留 %d 个" % len(unconfirmed))
    invisible = [t for t in targets if t in remaining]
    occluded = [t for t in invisible if t in in_frame]
    return invisible, unresolved, occluded


def _frustum_only_inside(cam_shapes, frames, targets):
    """只判是否进入过画面（不管遮挡），用于 ID-Buffer 结果的分类。"""
    inside = set()
    if not targets:
        return inside
    original_time = cmds.currentTime(query=True)
    try:
        for frame in frames:
            cmds.currentTime(frame)
            frustums = [(cam, _cam_frustum(cam)) for cam in cam_shapes]
            for target in targets:
                if target in inside or not _target_visible(target):
                    continue
                bbox = _bbox_points(target)
                geometry = _target_geometry(target)
                if geometry is None:
                    continue
                for _cam, frustum in frustums:
                    evidence, _uncertain = _frustum_evidence(
                        bbox, geometry, frustum, SAMPLES_PER_OBJ)
                    if evidence:
                        inside.add(target)
                        break
            if len(inside) == len(targets):
                break
    finally:
        try:
            cmds.currentTime(original_time)
        except Exception:
            pass
    return inside


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


def _view_camera(view):
    """兼容 API 2.0 的 getCamera() 与旧版 camera()。"""
    for name in ("getCamera", "camera"):
        getter = getattr(view, name, None)
        if getter is None:
            continue
        try:
            return getter()
        except Exception:
            continue
    return None


def _camera_object(cam_shape):
    return _to_dag_path(cam_shape).node()


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
            original_camera = _view_camera(view)
            if original_camera is None:
                raise RuntimeError("无法读取视口当前镜头")
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
                    view.setCamera(_camera_object(camera))
                    view.refresh()
                    image = om.MImage()
                    try:
                        view.readColorBuffer(image, False)
                    except TypeError:
                        view.readColorBuffer(image)
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


def _shape_shading_groups(shape):
    try:
        return cmds.listSets(object=shape, type=1) or []
    except Exception:
        return []


def _shading_group(key):
    """返回共享标记材质的 shadingGroup；已存在则复用，绝不重复创建。"""
    material, rgb = _MATERIALS[key]
    group = material + "SG"
    if cmds.objExists(material) and cmds.nodeType(material) != "shadingEngine":
        if not cmds.objExists(group):
            linked = cmds.listConnections(material, type="shadingEngine") or []
            if linked:
                group = linked[0]
            else:
                group = cmds.sets(name=group, renderable=True,
                                  noSurfaceShader=True, empty=True)
            if not cmds.isConnected(material + ".outColor",
                                    group + ".surfaceShader"):
                cmds.connectAttr(material + ".outColor",
                                 group + ".surfaceShader", force=True)
    else:
        material = cmds.shadingNode("lambert", asShader=True,
                                    name=material)
        group = cmds.sets(name=group, renderable=True,
                          noSurfaceShader=True, empty=True)
        cmds.connectAttr(material + ".outColor", group + ".surfaceShader",
                         force=True)
    cmds.setAttr(material + ".color", rgb[0], rgb[1], rgb[2],
                 type="double3")
    cmds.setAttr(material + ".ambientColor", rgb[0] * 0.2, rgb[1] * 0.2,
                 rgb[2] * 0.2, type="double3")
    cmds.setAttr(material + ".diffuse", 0.85)
    return group


def _paint_state():
    if not cmds.objExists(_RED_STATE):
        return {}
    try:
        raw = cmds.getAttr(_RED_STATE + ".restore")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _store_paint_state(mapping):
    if not cmds.objExists(_RED_STATE):
        cmds.createNode("transform", name=_RED_STATE)
        cmds.setAttr(_RED_STATE + ".visibility", False)
        cmds.addAttr(_RED_STATE, longName="restore", dataType="string")
    cmds.setAttr(_RED_STATE + ".restore", json.dumps(mapping), type="string")


def _restore_paint():
    """还原上次标色前的材质指定，保证每次检测只反映当前结果。"""
    mapping = _paint_state()
    if not mapping:
        return
    for shape, groups in mapping.items():
        if not cmds.objExists(shape):
            continue
        for group in groups:
            if not cmds.objExists(group):
                continue
            try:
                cmds.sets(shape, edit=True, forceElement=group)
            except Exception:
                pass
    _store_paint_state({})


def _paint(batches):
    """按类别把物体指定到各自共享材质；返回 {类别: shadingGroup}。"""
    shapes_by_key = {}
    for key, targets in batches.items():
        shapes = []
        for transform in targets:
            relatives = cmds.listRelatives(transform, shapes=True,
                                           fullPath=True) or []
            for shape in relatives:
                if cmds.nodeType(shape) == "mesh":
                    shapes.append(shape)
        if shapes:
            shapes_by_key[key] = shapes
    if not shapes_by_key:
        return {}
    _restore_paint()
    mapping = {}
    groups = {}
    for key, shapes in shapes_by_key.items():
        group = _shading_group(key)
        groups[key] = group
        for shape in shapes:
            mapping[shape] = _shape_shading_groups(shape)
            try:
                cmds.sets(shape, edit=True, forceElement=group)
            except Exception:
                mapping.pop(shape, None)
    _store_paint_state(mapping)
    return groups


def _report(invisible, unresolved, occluded=()):
    for target, reason in unresolved:
        print("无法判定（未标色）：%s\n    %s" % (target, reason))
    if invisible:
        blocked_set = set(occluded or ())
        blocked = [t for t in invisible if t in blocked_set]
        outside = [t for t in invisible if t not in blocked_set]
        batches = {}
        if outside:
            batches["red"] = outside
        if blocked:
            batches["purple"] = blocked
        groups = _paint(batches)
        print("在指定镜头和帧范围内始终不可见的物体 %d 个：" % len(invisible))
        if outside:
            print("  画面外 %d 个（红色 %s）：" %
                  (len(outside), groups.get("red", "")))
            for target in outside:
                print("     ", target)
        if blocked:
            print("  画面内但完全被遮挡 %d 个（紫色 %s）：" %
                  (len(blocked), groups.get("purple", "")))
            for target in blocked:
                print("     ", target)
        try:
            cmds.inViewMessage(
                amg="<hl>%d</hl> 个物体不可见：画面外 %d（红），"
                    "被遮挡 %d（紫）"
                    % (len(invisible), len(outside), len(blocked)),
                pos="midCenter", fade=True)
        except Exception:
            pass
    else:
        _restore_paint()
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


def _frame_text(frames, limit=6):
    """帧列表的紧凑写法：单帧、连续区间、或列举。"""
    values = sorted(set(frames))
    if not values:
        return ""
    if len(values) == 1:
        return "%g" % values[0]
    step = values[1] - values[0]
    contiguous = all(abs((b - a) - step) < 1e-6
                     for a, b in zip(values, values[1:]))
    if contiguous:
        return "%g 至 %g" % (values[0], values[-1])
    head = values[:limit]
    text = "、".join("%g" % v for v in head)
    if len(values) > limit:
        text += " 等"
    return text


def _check_selected(*_args):
    """检查当前选中的物体：在每个所选镜头的画面内 / 画面外 / 被遮挡。"""
    selection = cmds.ls(selection=True, long=True) or []
    targets = _target_transforms(selection)
    if not targets:
        _set_info("检查失败：请先选中要检查的物体")
        cmds.warning("请先选中要检查的物体，再点「检查选中物体」")
        return []
    cam_shapes = _chosen_cameras()
    if cam_shapes is None:
        _set_info("检查失败：请至少选择一个镜头")
        cmds.warning("请至少选择一个镜头（或点击「全选」）")
        return []
    try:
        frames = _sample_frames()
    except Exception as exc:
        _set_info("帧范围无效：%s" % exc)
        return []

    labels = {cam: _camera_label(cam) for cam in cam_shapes}
    stats = {t: dict((cam, {"inside": [], "clear": [], "blocked": []})
                     for cam in cam_shapes) for t in targets}
    unknown = {}
    original_time = cmds.currentTime(query=True)
    try:
        for frame in frames:
            cmds.currentTime(frame)
            # 检查模式严格按真实画面判定：不放宽视锥、不设三角形预算。
            frustums = [(cam, _cam_frustum(cam, margin=1.0))
                        for cam in cam_shapes]
            occluders = _build_occluders() if OCCLUSION else []
            index = _OccluderIndex(occluders) if OCCLUSION else None
            for target in targets:
                if not _target_visible(target):
                    continue
                bbox = _bbox_points(target)
                geometry = _target_geometry(target)
                if geometry is None:
                    unknown[target] = "网格过大，无法精确判定"
                    continue
                for cam, frustum in frustums:
                    evidence, uncertain = _frustum_evidence(
                        bbox, geometry, frustum,
                        SAMPLES_PER_OBJ if OCCLUSION else 1, strict=True)
                    if uncertain:
                        unknown[target] = "三角形超过预算，无法精确判定"
                        continue
                    if not evidence:
                        continue
                    stats[target][cam]["inside"].append(frame)
                    u, v = max((_screen_ratio(p, frustum) for p in evidence),
                               key=lambda s: max(abs(s[0]), abs(s[1])))
                    prev = stats[target][cam].get("edge")
                    if prev is None or max(abs(u), abs(v)) > max(
                            abs(prev[0]), abs(prev[1])):
                        stats[target][cam]["edge"] = (u, v)
                    if not OCCLUSION:
                        stats[target][cam]["clear"].append(frame)
                        continue
                    points = _occlusion_points(geometry, frustum, evidence)
                    if any(not _ray_blocked(_ray_source(p, frustum), p,
                                            index, target) for p in points):
                        stats[target][cam]["clear"].append(frame)
                    else:
                        stats[target][cam]["blocked"].append(frame)
    finally:
        try:
            cmds.currentTime(original_time)
        except Exception:
            pass

    lines = []
    inside_any = set()
    clear_any = set()
    total = len(frames)
    for target in targets:
        lines.append("物体 %s" % target)
        for cam in cam_shapes:
            data = stats[target][cam]
            name = labels[cam]
            edge = data.get("edge")
            edge_text = ("，采样点 u=%.2f, v=%.2f" % edge) if edge else ""
            if data["clear"]:
                inside_any.add(target)
                clear_any.add(target)
                lines.append(
                    "  %s：画面内且未被遮挡 %d 帧（共采样 %d 帧，帧 %s）%s" %
                    (name, len(data["clear"]), total,
                     _frame_text(data["clear"]), edge_text))
            elif data["inside"]:
                inside_any.add(target)
                lines.append(
                    "  %s：画面内但全部被遮挡（共采样 %d 帧，帧 %s）%s" %
                    (name, total, _frame_text(data["inside"]), edge_text))
            else:
                lines.append("  %s：不在画面内（共采样 %d 帧）" %
                             (name, total))
        if target in unknown:
            lines.append("  注意：%s" % unknown[target])
    report = "\n".join(lines)
    print(report)

    # 标色：完全在画面外 → 红；进入画面但所有帧都被挡 → 紫。
    outside = [t for t in targets if t not in inside_any and t not in unknown]
    blocked = [t for t in targets
               if t in inside_any and t not in clear_any and t not in unknown]
    batches = {}
    if outside:
        batches["red"] = outside
    if blocked:
        batches["purple"] = blocked
    if batches:
        _paint(batches)
    else:
        _restore_paint()

    all_inside = len(inside_any) == len(targets)
    message = ("检查完成：%d/%d 个物体在画面内，画面外 %d 个（红），"
               "被遮挡 %d 个（紫）"
               % (len(inside_any), len(targets), len(outside), len(blocked)))
    if unknown:
        message += "，无法判定 %d 个" % len(unknown)
    _set_info(message, color="#1a7f37" if all_inside else "#d1242f")
    return report


def _run_detection(cam_shapes, targets, frames, progress=None):
    """按已统计好的镜头、物体、帧执行判定，并恢复原始时间。"""
    _validate_options(targets)
    original_time = cmds.currentTime(query=True)
    try:
        invisible = None
        unresolved = []
        occluded = []
        if PIXEL_EXACT:
            invisible = _pixel_exact_invisible(cam_shapes, frames, targets)
            if invisible is not None:
                print("视口 ID-Buffer 判定完成。")
                inside = _frustum_only_inside(cam_shapes, frames, invisible)
                occluded = [t for t in invisible if t in inside]

        if invisible is None:
            if OCCLUSION:
                _occluder_report()
            invisible, unresolved, occluded = _sampling_invisible(
                cam_shapes, frames, targets, progress)
        elif progress is not None:
            progress(100, 1, 1, 0.0)
        _report(invisible, unresolved, occluded)
        return invisible, occluded
    finally:
        try:
            cmds.currentTime(original_time)
        except Exception:
            pass


def select_invisible(*_args):
    """脚本编辑器入口：按当前选择和作用域直接判定。"""
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
        return _run_detection(cam_shapes, targets, frames)[0]
    except Exception as exc:
        cmds.warning("不可见物体判定已中止：%s" % exc)
        return []


# ---------------- 界面 ----------------
_WINDOW = "invisibleObjectFinderWindow"
_FIELD_START = "iofStartField"
_FIELD_END = "iofEndField"
_FIELD_STEP = "iofStepField"
_CHECK_OCCLUSION = "iofOcclusionCheck"
_CHECK_EXACT = "iofExactCheck"
_CHECK_PIXEL = "iofPixelCheck"
_INFO = "iofInfoText"
_PROGRESS_BAR = "iofProgressBar"
_PROGRESS_TEXT = "iofProgressText"
_PROGRESS_BOX = "iofProgressBox"
_PROGRESS_FORM = "iofProgressForm"
_BUTTON_START = "iofStartButton"
_BUTTON_CLOSE = "iofCloseButton"
_BUTTON_CHECK = "iofCheckButton"
_CAMERA_LIST = "iofCameraList"
_CAMERAS_CACHE = []
_FRAME_FOLLOW_JOB = None
_FRAME_FOLLOW = True
_FRAME_FOLLOW_GUARD = False


def _format_seconds(seconds):
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return "%d 小时 %d 分" % (hours, minutes)
    if minutes:
        return "%d 分 %d 秒" % (minutes, secs)
    return "%d 秒" % secs


def _set_progress(percent, elapsed):
    percent = max(0, min(100, int(percent)))
    if percent >= 100:
        eta = "0 秒"
    elif percent > 0 and elapsed > 0.2:
        eta = _format_seconds(elapsed * (100.0 - percent) / percent)
    else:
        eta = "计算中…"
    label = "进度 %d%%    已用 %s    预计剩余 %s" % (
        percent, _format_seconds(elapsed), eta)
    try:
        cmds.progressBar(_PROGRESS_BAR, edit=True, progress=percent,
                         visible=True)
        cmds.text(_PROGRESS_TEXT, edit=True, label=label, visible=True)
        _trace("界面刷新 %d%%" % percent)
        cmds.refresh(force=True)
    except Exception:
        pass  # 窗口被关闭时静默继续


def _set_info(label, color=None):
    """更新按钮下方的信息文字；color 为 None 时恢复默认颜色。"""
    try:
        cmds.text(_INFO, edit=True, label=label, visible=True)
    except Exception:
        return
    _set_label_color(_INFO, color)
    try:
        cmds.refresh(force=True)
    except Exception:
        pass


def _set_label_color(name, color):
    """用 Qt 样式表给 Maya 文本控件设置文字颜色；失败时静默跳过。"""
    try:
        from maya import OpenMayaUI as omui
    except Exception:
        return
    try:
        from shiboken2 import wrapInstance
        from PySide2 import QtWidgets
    except Exception:
        try:
            from shiboken6 import wrapInstance
            from PySide6 import QtWidgets
        except Exception:
            return
    try:
        pointer = omui.MQtUtil.findControl(name)
        if not pointer:
            return
        widget = wrapInstance(int(pointer), QtWidgets.QWidget)
        widget.setStyleSheet("color: %s;" % color if color else "")
    except Exception:
        pass


def _make_progress():
    """返回带节流的进度回调，避免每个物体都刷新界面。"""
    state = {"percent": -1, "time": 0.0}

    def update(percent, done, total, elapsed):
        percent = max(0, min(100, int(percent)))
        now = time.time()
        if percent == state["percent"] and now - state["time"] < 0.4:
            return
        state["percent"] = percent
        state["time"] = now
        _set_progress(percent, elapsed)

    return update


def _frame_field(field):
    text = (cmds.textFieldGrp(field, query=True, text=True) or "").strip()
    return float(text) if text else None


def _current_frame_text():
    return "%g" % float(cmds.currentTime(query=True))


def _set_frame_fields(start, end):
    global _FRAME_FOLLOW_GUARD
    _FRAME_FOLLOW_GUARD = True
    try:
        cmds.textFieldGrp(_FIELD_START, edit=True, text=start)
        cmds.textFieldGrp(_FIELD_END, edit=True, text=end)
    except Exception:
        pass
    finally:
        _FRAME_FOLLOW_GUARD = False


def _frame_fields_follow_current(*_args):
    """时间轴变化时，未手动改过的起止帧继续等于当前帧。"""
    if not _FRAME_FOLLOW:
        return
    value = _current_frame_text()
    _set_frame_fields(value, value)


def _frame_manual_edit(*_args):
    """用户改过帧范围后停止跟随，避免把自定义范围冲掉。"""
    global _FRAME_FOLLOW
    if not _FRAME_FOLLOW_GUARD:
        _FRAME_FOLLOW = False


def _stop_frame_follow(*_args):
    global _FRAME_FOLLOW_JOB, _FRAME_FOLLOW
    _FRAME_FOLLOW = False
    if _FRAME_FOLLOW_JOB is not None:
        try:
            if cmds.scriptJob(exists=_FRAME_FOLLOW_JOB):
                cmds.scriptJob(kill=_FRAME_FOLLOW_JOB, force=True)
        except Exception:
            pass
        _FRAME_FOLLOW_JOB = None


def _start_frame_follow():
    global _FRAME_FOLLOW_JOB, _FRAME_FOLLOW
    _stop_frame_follow()
    _FRAME_FOLLOW = True
    try:
        _FRAME_FOLLOW_JOB = cmds.scriptJob(
            event=["timeChanged", _frame_fields_follow_current],
            killWithScene=False)
    except Exception:
        _FRAME_FOLLOW_JOB = None


def _close_ui(*_args):
    _stop_frame_follow()
    if cmds.window(_WINDOW, exists=True):
        cmds.deleteUI(_WINDOW)


def _camera_label(shape):
    """列表显示大纲里的节点名（相机 transform），不带 Shape 后缀。"""
    parents = cmds.listRelatives(shape, parent=True) or []
    return parents[0] if parents else _short(shape)


def _refresh_cameras(*_args):
    """重新枚举场景镜头并默认全选。"""
    global _CAMERAS_CACHE
    _CAMERAS_CACHE = _cameras([])
    labels = [_camera_label(cam) for cam in _CAMERAS_CACHE]
    cmds.textScrollList(_CAMERA_LIST, edit=True, removeAll=True)
    if labels:
        cmds.textScrollList(_CAMERA_LIST, edit=True, append=labels)
        cmds.textScrollList(
            _CAMERA_LIST, edit=True,
            selectIndexedItem=list(range(1, len(labels) + 1)))
    return _CAMERAS_CACHE


def _select_all_cameras(*_args):
    indices = list(range(1, len(_CAMERAS_CACHE) + 1))
    if indices:
        cmds.textScrollList(_CAMERA_LIST, edit=True,
                            selectIndexedItem=indices)


def _select_no_cameras(*_args):
    cmds.textScrollList(_CAMERA_LIST, edit=True, deselectAll=True)


def _chosen_cameras():
    """面板里勾选的镜头；列表为空时退回视口选择/全部镜头。"""
    listed = _CAMERAS_CACHE
    if not listed:
        return _cameras(cmds.ls(selection=True, long=True) or [])
    indices = cmds.textScrollList(_CAMERA_LIST, query=True,
                                  selectIndexedItem=True) or []
    if not indices:
        return None
    return [listed[i - 1] for i in indices if 0 < i <= len(listed)]


def _main_window_minimized():
    """检测 Maya 主窗口是否最小化；取不到 Qt 时视为正常。"""
    try:
        from maya import OpenMayaUI as legacy_ui
    except Exception:
        return False
    try:
        from shiboken2 import wrapInstance
        from PySide2 import QtWidgets
    except Exception:
        try:
            from shiboken6 import wrapInstance
            from PySide6 import QtWidgets
        except Exception:
            return False
    try:
        pointer = legacy_ui.MQtUtil.mainWindow()
        if not pointer:
            return False
        window = wrapInstance(int(pointer), QtWidgets.QWidget)
        return bool(window and window.isMinimized())
    except Exception:
        return False


def _pixel_preflight(targets):
    """返回无法使用视口 ID-Buffer 的原因与解决方法列表。"""
    problems = []
    try:
        if cmds.about(batch=True):
            problems.append((
                "当前是批处理/无界面会话（mayapy 或 -batch），没有 3D 视口。",
                "用 Maya 图形界面正常启动后再运行脚本，不要用 mayapy。"))
    except Exception:
        pass

    try:
        import maya.api.OpenMayaUI as omui
    except Exception:
        problems.append((
            "当前环境无法加载 maya.api.OpenMayaUI，说明不是 GUI 会话。",
            "改用 Maya 图形界面启动，并确认安装完整。"))
        return problems

    view = None
    try:
        view = omui.M3dView.active3dView()
    except Exception as exc:
        problems.append((
            "读取活动视口失败：%s" % exc,
            "重新打开一个 3D 视图面板后再试。"))
    if view is None:
        problems.append((
            "没有活动的 3D 视口（可能所有视图都被关闭或替换）。",
            "打开一个 3D 视图面板并点击使其激活，不要把脚本编辑器最大化"
            "覆盖整个视图区域。"))
        return problems

    try:
        view.getCamera()
    except Exception as exc:
        problems.append((
            "活动视口不可用：%s" % exc,
            "恢复或新建一个 3D 视图面板后重试。"))

    try:
        width, height = view.portWidth(), view.portHeight()
    except Exception:
        width, height = None, None
    if width is not None and (width <= 0 or height <= 0):
        problems.append((
            "视口尺寸为 0，画面没有实际绘制。",
            "取消最小化 Maya 主窗口，恢复视图面板大小后再运行。"))

    if _main_window_minimized():
        problems.append((
            "Maya 主窗口处于最小化状态，视口不会刷新。",
            "还原 Maya 主窗口并保持可见后重新运行。"))

    capacity = _ID_LEVELS ** 3
    if len(targets) > capacity:
        problems.append((
            "候选物体 %d 个，超过 ID 色容量 %d 个，无法分配唯一颜色。"
            % (len(targets), capacity),
            "先在视口里选中部分物体缩小范围，或分批检测。"))

    return problems


def _pixel_problem_message(problems):
    lines = ["无法使用「视口 ID-Buffer」模式："]
    for number, (reason, solution) in enumerate(problems, 1):
        lines.append("%d. %s" % (number, reason))
        lines.append("   解决方法：%s" % solution)
    lines.append("")
    lines.append("可以取消勾选后改用采样模式，精度略低但不受视口限制。")
    return "\n".join(lines)


def _ui_start(*_args):
    """点击开始后才扫描场景并执行判定。"""
    global FRAME_START, FRAME_END, FRAME_STEP
    global OCCLUSION, EXACT_FRUSTUM, PIXEL_EXACT
    try:
        FRAME_START = _frame_field(_FIELD_START)
        FRAME_END = _frame_field(_FIELD_END)
        FRAME_STEP = _frame_field(_FIELD_STEP)
        if FRAME_STEP is None:
            FRAME_STEP = 1.0
    except ValueError:
        cmds.warning("帧范围必须填写数字，留空表示使用当前帧")
        return

    OCCLUSION = cmds.checkBox(_CHECK_OCCLUSION, query=True, value=True)
    EXACT_FRUSTUM = cmds.checkBox(_CHECK_EXACT, query=True, value=True)
    PIXEL_EXACT = cmds.checkBox(_CHECK_PIXEL, query=True, value=True)

    selection = cmds.ls(selection=True, long=True) or []
    cam_shapes = _chosen_cameras()
    if cam_shapes is None:
        cmds.warning("请至少选择一个镜头（或点击「全选」）")
        return
    targets = _target_transforms(selection)
    try:
        frames = _sample_frames()
    except Exception as exc:
        _set_info("帧范围无效：%s" % exc)
        cmds.warning("帧范围无效：%s" % exc)
        return

    info = ("镜头：%d 个\n模型：%d 个\n帧：%d 个（%g 至 %g）" %
            (len(cam_shapes), len(targets), len(frames),
             frames[0], frames[-1]))
    _set_info(info)
    print(info.replace("\n", "，"))

    if not cam_shapes:
        cmds.warning("场景里没有可用镜头")
        return
    if not targets:
        cmds.warning("场景里没有可判定的网格模型")
        return

    if PIXEL_EXACT:
        problems = _pixel_preflight(targets)
        if problems:
            message = _pixel_problem_message(problems)
            print(message)
            try:
                choice = cmds.confirmDialog(
                    title="无法运行视口 ID-Buffer",
                    message=message,
                    button=["改用采样判定", "取消"],
                    defaultButton="改用采样判定",
                    cancelButton="取消",
                    dismissString="取消")
            except Exception:
                choice = "改用采样判定"
            if choice != "改用采样判定":
                return
            PIXEL_EXACT = False
            try:
                cmds.checkBox(_CHECK_PIXEL, edit=True, value=False)
            except Exception:
                pass

    update = _make_progress()
    try:
        # 开始后隐藏统计信息，让位给进度条与进度文字。
        cmds.text(_INFO, edit=True, visible=False)
    except Exception:
        pass
    _set_progress(0, 0.0)
    try:
        cmds.button(_BUTTON_START, edit=True, enable=False)
    except Exception:
        pass
    try:
        invisible, occluded = _run_detection(cam_shapes, targets, frames,
                                             update)
    except Exception as exc:
        cmds.warning("不可见物体判定已中止：%s" % exc)
        _set_info("判定中止：%s" % exc)
        return []
    finally:
        try:
            cmds.button(_BUTTON_START, edit=True, enable=True)
        except Exception:
            pass

    # 结束后收起进度条与进度文字，在按钮下方显示结果。
    try:
        cmds.progressBar(_PROGRESS_BAR, edit=True, visible=False)
        cmds.text(_PROGRESS_TEXT, edit=True, visible=False)
    except Exception:
        pass
    if invisible:
        blocked = len([t for t in invisible if t in set(occluded or ())])
        outside = len(invisible) - blocked
        _set_info("检测完成：画面外 %d 个（红），被遮挡 %d 个（紫）"
                  % (outside, blocked))
    else:
        _set_info("检测完成：没有发现始终不可见的物体")
    return invisible


def show_ui(*_args):
    """打开检测面板；点击「开始」后才统计场景并运行。"""
    if cmds.window(_WINDOW, exists=True):
        _stop_frame_follow()
        cmds.deleteUI(_WINDOW)

    cmds.window(_WINDOW, title="不可见物体检测", width=380,
                resizeToFitChildren=True, sizeable=False,
                closeCommand=_stop_frame_follow)
    cmds.columnLayout(adjustableColumn=True, rowSpacing=8,
                      columnAttach=("both", 10))
    cmds.text(label="选择镜头后可只跑其中几个；帧范围对已选镜头生效。",
              align="left")
    cmds.frameLayout(label="镜头（可多选）", collapsable=False)
    cmds.columnLayout(adjustableColumn=True, rowSpacing=4)
    cmds.textScrollList(_CAMERA_LIST, numberOfRows=6, height=96,
                        allowMultiSelection=True)
    cmds.rowColumnLayout(numberOfColumns=7,
                         columnWidth=[(1, 10), (2, 100), (3, 12),
                                      (4, 100), (5, 12), (6, 100),
                                      (7, 10)])
    cmds.text(label="", width=10)
    cmds.button(label="全选", height=24, width=100,
                command=_select_all_cameras)
    cmds.text(label="", width=12)
    cmds.button(label="全不选", height=24, width=100,
                command=_select_no_cameras)
    cmds.text(label="", width=12)
    cmds.button(label="刷新", height=24, width=100, command=_refresh_cameras)
    cmds.text(label="", width=10)
    cmds.setParent("..")
    cmds.setParent("..")
    cmds.setParent("..")
    cmds.frameLayout(label="帧范围与判定选项（帧范围默认跟随当前帧）",
                     collapsable=False)
    cmds.rowColumnLayout(numberOfColumns=2,
                         columnWidth=[(1, 172), (2, 172)],
                         columnSpacing=[(1, 8)],
                         rowSpacing=[(1, 6), (2, 6)],
                         columnAlign=[(1, "left"), (2, "left")])
    current_frame = _current_frame_text()
    cmds.textFieldGrp(_FIELD_START, label="起始帧", text=current_frame,
                      height=24, width=172, adjustableColumn=0,
                      columnWidth=[(1, 52), (2, 110)],
                      changeCommand=_frame_manual_edit)
    cmds.checkBox(_CHECK_OCCLUSION, label="遮挡判定", value=OCCLUSION,
                  height=24, width=172)
    cmds.textFieldGrp(_FIELD_END, label="结束帧", text=current_frame,
                      height=24, width=172, adjustableColumn=0,
                      columnWidth=[(1, 52), (2, 110)],
                      changeCommand=_frame_manual_edit)
    cmds.checkBox(_CHECK_EXACT, label="精确视锥裁剪", value=EXACT_FRUSTUM,
                  height=24, width=172)
    cmds.textFieldGrp(_FIELD_STEP, label="帧步长", text="1",
                      height=24, width=172, adjustableColumn=0,
                      columnWidth=[(1, 52), (2, 110)])
    cmds.checkBox(_CHECK_PIXEL, label="视口 ID-Buffer", value=PIXEL_EXACT,
                  height=24, width=172,
                  annotation="逐像素精确判定，需要 Maya GUI 会话")
    cmds.setParent("..")
    cmds.setParent("..")
    # 进度条默认隐藏，但保留固定高度的容器，开始后出现时界面不跳动。
    cmds.frameLayout(_PROGRESS_BOX, height=24, borderVisible=False,
                     labelVisible=False, collapsable=False)
    cmds.formLayout(_PROGRESS_FORM, height=20)
    _bar = cmds.progressBar(_PROGRESS_BAR, minValue=0, maxValue=100,
                            height=14, visible=False)
    cmds.formLayout(_PROGRESS_FORM, edit=True,
                    attachForm=[(_bar, "left", 24), (_bar, "right", 24),
                                (_bar, "top", 3)])
    cmds.setParent("..")
    cmds.setParent("..")
    cmds.rowColumnLayout(numberOfColumns=7,
                         columnWidth=[(1, 10), (2, 104), (3, 16),
                                      (4, 104), (5, 16), (6, 104), (7, 10)])
    cmds.text(label="", width=10)
    cmds.button(_BUTTON_START, label="开始", height=32, width=104,
                command=_ui_start)
    cmds.text(label="", width=16)
    cmds.button(_BUTTON_CHECK, label="检测画面内", height=32, width=104,
                command=_check_selected)
    cmds.text(label="", width=16)
    cmds.button(_BUTTON_CLOSE, label="关闭", height=32, width=104,
                command=_close_ui)
    cmds.text(label="", width=10)
    cmds.setParent("..")
    cmds.text(_INFO, label="点击「开始」后统计场景数据", align="center")
    cmds.text(_PROGRESS_TEXT, label="进度 0%    已用 0 秒    预计剩余 --",
              align="center", visible=False)
    _refresh_cameras()
    _start_frame_follow()
    cmds.showWindow(_WINDOW)


if __name__ == "__main__":
    print("不可见物体检测 v%s" % VERSION)
    show_ui()
