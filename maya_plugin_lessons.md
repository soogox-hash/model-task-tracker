# Maya 脚本踩坑记录（每次改脚本前先过一遍）

## Maya UI（cmds）

1. `cmds.window` 的尺寸会被 `windowPref` 记住，旧高度会盖过布局计算结果。
   创建窗口前必须 `cmds.windowPref(name, remove=True)`。
2. `frameLayout` 放进窗口里会抢占剩余垂直空间，`resizeToFitChildren` 算不准高度。
   面板里用普通 `text` 当分组标题，不要用 `frameLayout` 包内容。
3. `formLayout` 默认高度 100，会撑高窗口；能不用就不用，居中改用左右占位列。
4. `rowColumnLayout` 的 `rowSpacing` / `columnSpacing` 必须传 `[(索引, 值)]`，
   传单个整数会报「参数无效」。
5. `rowAttach` 没有 `"center"` 这个值，去掉它，靠控件等高 + 同行对齐。
6. `textFieldGrp` 没有 `editChangedCommand`，用 `changeCommand`。
7. `visible=False` 的控件在 `columnLayout` 里会被完全收起，不占位。
   要"隐藏但保留空间"，必须放一个同高度的可见占位控件（如
   `cmds.text(label=" ", height=14)`），显示时隐藏占位、换成真控件；
   想隐藏文字行就把 label 设成 `" "`，不要 `visible=False`。
8. Qt 的 `QSizePolicy.setRetainSizeWhenHidden(True)` 对 Maya 原生布局无效，
   不要用这个办法预留空间。
9. 窗口最终高度用 `layout.setSizeConstraint(QLayout.SetFixedSize)` 锁死为内容高度。

## Maya API

10. `M3dView` 没有 `camera()`，用 `getCamera()`；`setCamera()` 要传 MObject
    （`dagPath.node()`），不能传 DAG Path。
11. `MFnMesh.closestIntersection` 签名因版本而异，且 `faceIds` / `points`
    不能传 `None`（必须 `MIntArray()` / `MFloatPointArray()`），起点方向要用
    `MFloatPoint` / `MFloatVector`。实现里要按多种签名依次尝试并缓存可用形式。
12. `MFnCamera.getRenderingFrustum()` 返回值的单位不可靠（曾导致视锥放宽约 10 倍）。
    视锥直接从焦距 + 胶片孔径 + film fit + 渲染宽高比算，正交用 `orthographicWidth`。
13. 默认相机只排除名称完全等于 `perspShape` / `frontShape` / `sideShape` /
    `topShape` 的四个，`perspShape1` 等编号副本要参与判定。

## 性能与稳定性

14. 大网格不要一次性 `getPoints()` 进 Python；设顶点/三角预算，
    超限按"可见"保留并单独列出。
15. 三角索引用 `array('i')`，不要用 Python int 列表。
16. 遮挡求交要用空间网格或 AABB 预筛，不能每条射线遍历全场景网格。
17. 纯 Python 逐像素扫视口（ID-Buffer）在 1080p × 多帧 × 多镜头下必然卡死，
    只允许小范围验证，并设物体数量上限。
18. 长循环要写轨迹日志（处理每个物体前落盘一行），Maya 原生崩溃时
    Python 的 try/except 拦不住，只能靠日志定位。

## 交付流程

19. 每次改动：升 `VERSION` → `py_compile` → 跑 `/tmp` 下的测试 →
    重新生成 `select_invisible_copy.html`，并告知行数。
20. 脚本注释和 UI 文案用中文；窗口标题与功能保持一致。
