/**
 * preload.js — 只向渲染进程暴露「白名单式」的最小 API 面（contextIsolation + sandbox）
 * 安全要点：
 *  1. 所有入参在这里先做类型与长度收敛，避免把任意对象透传给主进程
 *  2. 不暴露 ipcRenderer 本体，渲染进程无法自造 IPC 通道
 *  3. 主进程侧仍有 assertTrusted / 路径校验，形成纵深防御
 */
const { contextBridge, ipcRenderer } = require("electron");

const str = (v, max = 500) => (v == null ? "" : String(v).slice(0, max));
const json = (v) => (typeof v === "string" ? v : JSON.stringify(v));

contextBridge.exposeInMainWorld("eapi", {
  /* 数据读写 */
  save: (d) => ipcRenderer.invoke("db:save", json(d)),
  backup: (d) => ipcRenderer.invoke("db:backup", json(d)),
  backupAuto: (d) => ipcRenderer.invoke("backup:auto", json(d)),
  backupManual: (d) => ipcRenderer.invoke("backup:manual", json(d)),
  backupList: () => ipcRenderer.invoke("backup:list"),
  backupRead: (file) => ipcRenderer.invoke("backup:read", str(file, 200)),
  backupDelete: (file) => ipcRenderer.invoke("backup:delete", str(file, 200)),
  integrity: () => ipcRenderer.invoke("data:integrity"),
  /* v0.140 数据急救：查看数据文件状态 / 回到上一版 */
  recoverStatus: () => ipcRenderer.invoke("db:recover:status"),
  recoverPrev: () => ipcRenderer.invoke("db:recover:prev"),

  /* 截图（主进程会先弹确认框，只截当前应用窗口） */
  capture: () => ipcRenderer.invoke("screen:capture"),

  /* 数据路径 */
  dataPathGet: () => ipcRenderer.invoke("data:path:get"),
  dataPathSet: (p) => ipcRenderer.invoke("data:path:set", str(p, 260)),
  dataPathPick: () => ipcRenderer.invoke("data:path:pick"),
  folderPick: (title) => ipcRenderer.invoke("folder:pick", str(title, 100)),
  raise: () => ipcRenderer.invoke("win:raise"),
  folderOpen: (p) => ipcRenderer.invoke("folder:open", str(p, 500)),

  /* 任务文件夹体系：每个任务一个子文件夹（附件/截图/工程文件落盘保存） */
  taskRootSet: (p) => ipcRenderer.invoke("taskroot:set", str(p, 260)),
  taskDir: (name) => ipcRenderer.invoke("task:dir", str(name, 200)),
  taskSaveFile: (name, fileName, dataBase64) =>
    ipcRenderer.invoke("task:saveFile", str(name, 200), str(fileName, 200), str(dataBase64, 700 * 1024 * 1024)),

  /* 打包整份数据（artvom 文件夹 → zip）并定位压缩包 */
  packData: () => ipcRenderer.invoke("data:pack"),
  /* 导入打包（与 packData 对称）：选 zip 一键恢复整份数据，换电脑迁移用 */
  unpackData: () => ipcRenderer.invoke("data:unpack"),
  showItem: (p) => ipcRenderer.invoke("shell:showItem", str(p, 500)),

  /* ===== v0.109 模型库（属于本软件，模型文件存 <数据根>/models/<id>/） ===== */
  modelSave: (payload) => ipcRenderer.invoke("models:saveAsset", payload && typeof payload === "object" ? payload : {}),
  modelDelete: (id) => ipcRenderer.invoke("models:deleteAsset", str(id, 40)),
  modelReveal: (id) => ipcRenderer.invoke("models:reveal", str(id, 40)),

  /* ===== Maya 桥接：文件握手，Maya 端只做"导入/回传"两件事 ===== */
  bridgeRequestImport: (req) => ipcRenderer.invoke("bridge:requestImport", req && typeof req === "object" ? req : {}),
  bridgeDrainInbox: () => ipcRenderer.invoke("bridge:drainInbox"),
  bridgeStatus: () => ipcRenderer.invoke("bridge:status"),

  /* ===== v0.147：外部工具同步预留接口 ===== */
  syncDir: () => ipcRenderer.invoke("sync:dir"),
  syncExport: (payload) => ipcRenderer.invoke("sync:export", payload),
  syncImport: () => ipcRenderer.invoke("sync:import"),
});

/* ===== 初始数据注入（v0.144 修复）=====
   【原实现的问题】这里原本是 `window.__DB_INIT__ = ipcRenderer.sendSync("db:init")`。
   本窗口 contextIsolation:true，preload 运行在「隔离世界」里，往 window 上挂属性对页面
   主世界完全不可见 —— 页面永远读不到 __DB_INIT__，只能退回 localStorage。
   打包版（文件存储、localStorage 是空的）于是以空库启动，既没有数据也不进只读保护，
   第一次 save() 就把磁盘上的真任务数据覆盖成空库。__DB_INIT_ERROR__ / __DB_FILE_INFO__
   同样失效，v0.140 的只读保护在 exe 里等于没装。
   【修法】必须经过 contextBridge.exposeInMainWorld 才能跨世界传值。
   这里暴露一个函数，页面调用一次即可拿到三份初始数据（只走一次同步 IPC 往返）。 */
contextBridge.exposeInMainWorld("__dbBoot", () => {
  let init = "", error = "", file = null;
  try { init = ipcRenderer.sendSync("db:init"); } catch (_e) { init = ""; }
  try { error = ipcRenderer.sendSync("db:init:error") || ""; } catch (_e) { error = ""; }
  try { file = ipcRenderer.sendSync("db:file:info") || null; } catch (_e) { file = null; }
  return { init: typeof init === "string" ? init : "", error: String(error || ""), file };
});
