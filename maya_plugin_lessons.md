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
   要"隐藏但保留空间"，放一个同高度的可见占位控件，显示时隐藏占位；
   想隐藏文字行就把 label 设成 `" "`，不要 `visible=False`。
   占位用 `cmds.separator(style="none", height=N)` 最稳（`text` 的
   `height` 在部分版本会被忽略）。
8. Qt 的 `QSizePolicy.setRetainSizeWhenHidden(True)` 对 Maya 原生布局无效，
   不要用这个办法预留空间。
9. 窗口最终高度用 `layout.setSizeConstraint(QLayout.SetFixedSize)` 锁死为内容高度。
10. 占位控件与真控件高度不要只靠 `cmds` 的 `height` 猜：窗口显示后用 Qt
    `setFixedHeight(真控件.sizeHint().height())` 同时锁死两者，才保证像素级一致。

## Maya API

11. `M3dView` 没有 `camera()`，用 `getCamera()`；`setCamera()` 要传 MObject
    （`dagPath.node()`），不能传 DAG Path。
12. `MFnMesh.closestIntersection` 签名因版本而异，且 `faceIds` / `points`
    不能传 `None`（必须 `MIntArray()` / `MFloatPointArray()`），起点方向要用
    `MFloatPoint` / `MFloatVector`。实现里要按多种签名依次尝试并缓存可用形式。
13. `MFnCamera.getRenderingFrustum()` 返回值的单位不可靠（曾导致视锥放宽约 10 倍）。
    视锥直接从焦距 + 胶片孔径 + film fit + 渲染宽高比算，正交用 `orthographicWidth`。
14. 默认相机只排除名称完全等于 `perspShape` / `frontShape` / `sideShape` /
    `topShape` 的四个，`perspShape1` 等编号副本要参与判定。

## 性能与稳定性

15. 大网格不要一次性 `getPoints()` 进 Python；设顶点/三角预算，
    超限按"可见"保留并单独列出。
16. 三角索引用 `array('i')`，不要用 Python int 列表。
17. 遮挡求交要用空间网格或 AABB 预筛，不能每条射线遍历全场景网格。
18. 纯 Python 逐像素扫视口（ID-Buffer）在 1080p × 多帧 × 多镜头下必然卡死，
    只允许小范围验证，并设物体数量上限。
19. 长循环要写轨迹日志（处理每个物体前落盘一行），Maya 原生崩溃时
    Python 的 try/except 拦不住，只能靠日志定位。

## 交付流程

20. 每次改动：升 `VERSION` → `py_compile` → 跑 `/tmp` 下的测试 →
    重新生成 `select_invisible_copy.html`，并告知行数。
21. 脚本注释和 UI 文案用中文；窗口标题与功能保持一致。
22. 不在场景里创建插件专用节点：标色前的材质记录写在插件自己的材质
    （`invisible_check_red`）属性上，还原后立刻 `deleteAttr`，用户模型
    上不留任何自定义属性；旧版遗留的 `invisibleCheckState` 每次启动
    检测时自动删除。
23. 被隐藏（visibility 关、显示层关、primaryVisibility 关）的物体不算
    候选，任何一帧可见才参与判定；隐藏数量写进开始前的确认弹窗。
24. 遮挡射线必须 `testBothDirections=True`：视口是双面着色，法线翻转或
    单面的几何照样遮挡，单面求交会漏掉这类遮挡体（判成"可见"）。
25. `_camera_point` 已把深度取成正数（`-p.z`），屏幕坐标再取反会得到
    哨兵值 9.9，日志里的 u/v 全是假的。
