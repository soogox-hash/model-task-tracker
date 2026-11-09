/**
 * 模型任务记录系统（artvom）— 桌面版主进程
 * 数据目录：C:\artvom\data（任务数据、附件等全部数据）
 * 备份目录：C:\artvom\backups（按日期命名的备份 json）
 */
const { app, BrowserWindow, Menu, ipcMain, desktopCapturer, dialog, shell, safeStorage } = require("electron");
const path = require("path");
const fs = require("fs");
const crypto = require("crypto");
const zlib = require("zlib");

/* ===================== 安全基础设施（安全审计整改） ===================== */
const APP_ALLOWED_PROTOCOLS = ["file:"];
// 系统目录黑名单：禁止把数据/任务根目录指向系统关键位置
const SYS_DIRS = [
  process.env.SystemRoot || "C:\\Windows",
  process.env.ProgramFiles || "C:\\Program Files",
  process.env["ProgramFiles(x86)"] || "C:\\Program Files (x86)",
  process.env.ProgramData || "C:\\ProgramData",
];
// 统一错误码（避免把绝对路径、用户名等堆栈细节回给渲染进程）——L-4/L-5
const ERR = {
  P_IPC: "拒绝访问：非受信调用来源",
  P_PATH: "拒绝访问：路径超出允许范围",
  P_SYS: "拒绝访问：不允许使用系统目录",
  P_TYPE: "参数非法",
  P_SIZE: "数据体积超出限制",
  P_JSON: "数据结构不合法",
  P_NOENT: "目标不存在",
  P_WRITE: "写入失败，请检查磁盘空间与权限",
  P_UNKNOWN: "操作失败，请查看本地日志",
};
// 本地安全日志（只写主进程日志文件，绝不回传前端）——L-4
function secLog(tag, detail) {
  try {
    const dir = path.join(app.getPath("userData"), "logs");
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    const line = `[${new Date().toISOString()}] ${tag} ${detail || ""}\n`;
    fs.appendFileSync(path.join(dir, "security.log"), line, "utf8");
  } catch (_e) { /* 日志失败不影响主流程 */ }
  // 同时截断保留最近 2MB，避免无限增长
  try {
    const f = path.join(app.getPath("userData"), "logs", "security.log");
    if (fs.existsSync(f) && fs.statSync(f).size > 2 * 1024 * 1024) {
      const buf = fs.readFileSync(f);
      fs.writeFileSync(f, buf.slice(buf.length - 1024 * 1024));
    }
  } catch (_e) {}
}
// 路径包含校验（防穿越）：child 必须等于 root 或位于 root 之下
function insideRoot(child, root) {
  try {
    const c = path.resolve(String(child || ""));
    const r = path.resolve(String(root || ""));
    if (!r) return false;
    if (c.toLowerCase() === r.toLowerCase()) return true;
    return c.toLowerCase().startsWith(r.toLowerCase().replace(/[\\\/]+$/, "") + path.sep);
  } catch (_e) { return false; }
}
// 系统目录判定
function inSysDir(p) {
  return SYS_DIRS.some((s) => {
    if (!s) return false;
    const r = path.resolve(s).toLowerCase();
    const c = path.resolve(String(p || "")).toLowerCase();
    return c === r || c.startsWith(r + path.sep);
  });
}
// IPC 来源校验：只接受本应用主窗口的 file:// 页面
let __mainWinId = null;
/* v0.147：写入串行化队列。所有写盘操作（db:save / backup:*）都走这个队列，
   避免快速连点保存、自动备份与手动保存并发时交错写盘导致数据不一致。 */
let __writeQueue = Promise.resolve();
function enqueueWrite(fn) {
  const p = __writeQueue.then(() => fn()).catch((e) => { secLog("WRITE_QUEUE_ERR", String(e && e.message)); throw e; });
  __writeQueue = p.then(() => {}, () => {});   // 无论成败都推进队列，避免一个失败卡死后续
  return p;
}
/* v0.147：主进程级只读锁。数据初始化失败时设为 true，所有写 IPC 直接拒绝，
   作为前端只读保护的第二道防线（前端可能有写路径绕过判断）。 */
let __readOnly = false;
function assertTrusted(e) {
  const okWin = !__mainWinId || (e && e.sender && e.sender.id === __mainWinId);
  const wc = e && e.sender;
  const url = (wc && typeof wc.getURL === "function") ? wc.getURL() : "";
  const protoOk = !!url && APP_ALLOWED_PROTOCOLS.some((p) => url.startsWith(p));
  if (!okWin || !protoOk) {
    secLog("IPC_DENIED", `win=${e && e.sender ? e.sender.id : "?"} url=${url || "?"}`);
    throw new Error(ERR.P_IPC);
  }
  return true;
}
// 入参基本清洗
function asString(v, max) {
  if (v == null) return "";
  const s = typeof v === "string" ? v : String(v);
  return max ? s.slice(0, max) : s;
}
// 加密落盘（DPAPI / safeStorage）——M-1
const ENC_MAGIC = "ARTVOM-ENC-1\n";
/* v0.140 修复：落盘改为【原子写】。
   原来直接 fs.writeFileSync(file, payload) 覆盖原文件，写到一半断电 / 崩溃 / 磁盘满时，
   文件会变成半截 JSON，整份数据直接报废（而且没有任何副本）。
   现在：写临时文件 → fsync 落盘 → 把上一版留成 .prev → rename 原子替换。
   Windows 上 rename 覆盖已存在文件是原子的，最坏情况只会丢掉最后一次保存，不会损坏文件。 */
function writeDataFile(file, plain) {
  let payload = plain;
  try {
    if (safeStorage && safeStorage.isEncryptionAvailable()) {
      const buf = safeStorage.encryptString(String(plain));
      payload = Buffer.concat([Buffer.from(ENC_MAGIC, "utf8"), buf]);
    }
  } catch (_e) { payload = plain; }

  const tmp = file + ".tmp";
  let fd = null;
  try {
    fd = fs.openSync(tmp, "w");
    fs.writeFileSync(fd, payload);
    try { fs.fsyncSync(fd); } catch (_e) {}      // 确保数据真的落盘，不是停在系统缓存
  } finally {
    if (fd !== null) { try { fs.closeSync(fd); } catch (_e) {} }
  }
  // 保留上一版（只在原文件确实有内容时），用于文件损坏后的手工找回
  try {
    if (fs.existsSync(file) && fs.statSync(file).size > 0) fs.copyFileSync(file, file + ".prev");
  } catch (_e) {}
  try {
    fs.renameSync(tmp, file);                   // 原子替换
  } catch (err) {
    // 极少数情况（文件被杀软/网盘占用）改名失败：退化为直接写，保证这次保存不丢
    try { fs.unlinkSync(tmp); } catch (_e) {}
    secLog("WRITE_RENAME_FALLBACK", String(err && err.message));
    fs.writeFileSync(file, payload);
  }
}
/* v0.140 新增：备份文件写【明文 JSON】（数据文件本身仍加密）。
   原因：备份的唯一意义是换电脑 / 重装系统 / 硬盘坏掉之后能恢复，
   而 DPAPI 加密密钥跟着 Windows 用户账户走，换台机器或重装系统后加密备份永远解不开，
   等于白备份。明文备份复制到任何地方都能被本软件导入，也能用记事本直接看。 */
function writeBackupFile(file, plain) {
  const tmp = file + ".tmp";
  try {
    fs.writeFileSync(tmp, String(plain), "utf8");
    fs.renameSync(tmp, file);                 // 原子替换，避免半截备份
  } catch (_e) {
    try { fs.unlinkSync(tmp); } catch (_e2) {}
    fs.writeFileSync(file, String(plain), "utf8");
  }
}
/* v0.140 新增：附件 / 模型 / 截图等二进制文件的原子落盘工具。
   原来这些地方都是直接 fs.writeFileSync 写目标文件，写大文件时一旦崩溃或断电，
   会留下一个半截文件，而数据库里的记录却已经指向它 —— 用户点开就是"文件损坏"。
   现在统一「写 .part → fsync → rename」，要么是完整新文件，要么保持原样。 */
function writeFileSafe(file, data) {
  const tmp = file + ".part";
  let fd = null;
  try {
    fd = fs.openSync(tmp, "w");
    fs.writeFileSync(fd, data);
    try { fs.fsyncSync(fd); } catch (_e) {}
  } finally {
    if (fd !== null) { try { fs.closeSync(fd); } catch (_e) {} }
  }
  try {
    fs.renameSync(tmp, file);
  } catch (_e) {
    try { fs.unlinkSync(tmp); } catch (_e2) {}
    fs.writeFileSync(file, data);
  }
}
function readDataFile(file) {
  const raw = fs.readFileSync(file);
  if (raw.length > ENC_MAGIC.length && raw.slice(0, ENC_MAGIC.length).toString("utf8") === ENC_MAGIC) {
    try {
      const plain = safeStorage.decryptString(raw.slice(ENC_MAGIC.length));
      return plain;
    } catch (_e) {
      secLog("DECRYPT_FAIL", path.basename(file));
      throw new Error(ERR.P_WRITE);
    }
  }
  return raw.toString("utf8");     // 兼容旧的明文数据文件（首次启动自动升级为加密）
}
// 数据库结构校验：主进程不再无条件信任渲染进程——H-3
function validateDB(obj) {
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return ERR.P_JSON;
  if (!Array.isArray(obj.tasks)) return "tasks 必须是数组";
  if (obj.tasks.length > 200000) return "任务数量超出上限";
  for (const t of obj.tasks) {
    if (!t || typeof t !== "object") return "任务项必须是对象";
    if (t.name != null && (typeof t.name !== "string" || t.name.length > 500)) return "任务名非法";
    if (t.estHours != null && t.estHours !== "") {
      const n = Number(t.estHours);
      if (!isFinite(n) || n < 0 || n > 100000) return "计划工时不合法";
    }
    if (t.attachments != null && !Array.isArray(t.attachments)) return "附件结构非法";
  }
  return null;
}
// 状态派生（主进程口径，用系统时间，避免客户端改本地时间绕过）——H-3
function deriveTaskStates(obj) {
  const now = Date.now();
  const FINAL = ["approved", "pending_delivery", "delivered", "completed", "shelved"];
  const isFinal = (s) => FINAL.includes(s);
  let changed = 0;
  for (const t of obj.tasks) {
    if (!t || typeof t !== "object" || t.deletedAt || t.archived) continue;
    if (t.endTime && !isFinal(t.status)) {
      t.status = "completed";
      if (!t.finishedAt) t.finishedAt = t.endTime;
      if (!t.updatedAt) t.updatedAt = t.endTime;
      changed++;
    } else if (t.startTime && t.status === "pending") {
      const ts = Date.parse(String(t.startTime).replace(" ", "T"));
      if (isFinite(ts) && ts <= now) { t.status = "in_progress"; changed++; }
    }
  }
  return changed;
}
// 工时账本哈希链（tamper-evident）—— H-3 延伸
function ledgerHash(rows) {
  return crypto.createHash("sha256").update(JSON.stringify(rows)).digest("hex");
}
const __pad = (n) => String(n).padStart(2, "0");
function todayDs() { const d = new Date(); return `${d.getFullYear()}-${__pad(d.getMonth() + 1)}-${__pad(d.getDate())}`; }
/* v0.144：备份文件名统一用「本地时间」。
   原来手动备份和导出备份用 toISOString()（UTC），自动备份用 todayDs()（本地）。
   中国时区差 8 小时，凌晨 0–8 点做手动备份，文件名上的日期是"昨天"；
   两种命名混在同一个 backups 目录里，按文件名排序取"最近 30 份"的淘汰逻辑也跟着错。 */
function localStamp() {
  const d = new Date();
  return `${d.getFullYear()}-${__pad(d.getMonth() + 1)}-${__pad(d.getDate())}-${__pad(d.getHours())}-${__pad(d.getMinutes())}-${__pad(d.getSeconds())}`;
}


// 根目录：默认 C:\artvom，可被用户自定义（存 userData/config.json）；开发时用项目目录下 artvom
let __customRoot = null;
function loadConfig() {
  try { return JSON.parse(fs.readFileSync(path.join(app.getPath("userData"), "artvom-config.json"), "utf8")); }
  catch (e) { return {}; }
}
/* v0.140：改为返回是否写入成功 —— 数据路径迁移要靠它判断能否安全删除旧文件，
   写失败时必须回滚，否则数据会"搬到新位置但软件还指向旧位置"。 */
function saveConfig(cfg) {
  try {
    const f = path.join(app.getPath("userData"), "artvom-config.json");
    const tmp = f + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(cfg, null, 2), "utf8");
    fs.renameSync(tmp, f);
    return true;
  } catch (e) {
    secLog("CONFIG_SAVE_FAIL", String(e && e.message));
    return false;
  }
}
function getRootDir() {
  if (__customRoot) return __customRoot;
  if (app.isPackaged) {
    const cfg = loadConfig();
    if (cfg && cfg.root && typeof cfg.root === "string") { __customRoot = cfg.root; return __customRoot; }
    return "C:\\artvom";
  }
  return path.join(__dirname, "artvom");
}
// 数据目录：C:\artvom\data — 软件其他数据全部存在这里
function getDataDir() { return path.join(getRootDir(), "data"); }
// 备份目录：C:\artvom\backups — 软件备份的数据都放在这里
function getBackupDir() { return path.join(getRootDir(), "backups"); }
// 任务文件夹：C:\artvom\task — 每个任务一个子文件夹（附件/截图/工程文件都存在任务名对应的文件夹里）
function getTaskRoot() {
  const cfg = loadConfig();
  if (cfg && cfg.taskRoot && typeof cfg.taskRoot === "string") return cfg.taskRoot;
  return path.join(getRootDir(), "task");
}
function getDbFile() { return path.join(getDataDir(), "任务数据.json"); }
function getAttDir() { return path.join(getDataDir(), "附件"); }
// 任务名 → 安全文件夹名（去掉 Windows 非法字符）
function safeTaskDirName(name) {
  return String(name || "未命名任务").replace(/[\\\/:\*\?"<>\|]/g, "_").replace(/\s+/g, " ").trim().slice(0, 80) || "未命名任务";
}
// 确保任务子文件夹存在并返回其路径
function ensureTaskDir(taskName) {
  const dir = path.join(getTaskRoot(), safeTaskDirName(taskName));
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  return dir;
}
// 旧版数据目录（exe 旁「数据」），首次升级时自动迁移
function getLegacyDir() {
  const base = app.isPackaged ? path.dirname(app.getPath("exe")) : __dirname;
  return path.join(base, "数据");
}

function ensureDirs() {
  /* v0.122：启动自动扫描 6 个文件夹，有就忽略、没有就创建（task/models/bridge 之前是按需创建，现在一并建好） */
  for (const d of [getRootDir(), getDataDir(), getBackupDir(), getAttDir(), getTaskRoot(), getModelRoot(), getBridgeRoot()])
    if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true });
  // 老版本升级：把 exe 旁「数据」里的旧数据搬到新目录（带完整性校验，失败明确留源文件）——L-5
  try {
    const legacyDb = path.join(getLegacyDir(), "任务数据.json");
    if (!fs.existsSync(getDbFile()) && fs.existsSync(legacyDb)) {
      // 先复制到临时文件，校验字节数一致后再原子改名，避免半迁移
      const tmp = getDbFile() + ".migrating";
      fs.copyFileSync(legacyDb, tmp);
      const a = fs.statSync(legacyDb).size, b = fs.statSync(tmp).size;
      if (a !== b) { try { fs.unlinkSync(tmp); } catch (_e) {} secLog("MIGRATE_SIZE_MISMATCH", `${a}!=${b}`); }
      else fs.renameSync(tmp, getDbFile());
      const legacyAtt = path.join(getLegacyDir(), "附件");
      if (fs.existsSync(legacyAtt)) {
        for (const f of fs.readdirSync(legacyAtt)) {
          const src = path.join(legacyAtt, f);
          try {
            if (!fs.statSync(src).isFile()) continue;
            const dst = path.join(getAttDir(), f);
            fs.copyFileSync(src, dst);
            if (fs.statSync(src).size !== fs.statSync(dst).size) fs.copyFileSync(src, dst);  // 重试一次
          } catch (e1) { secLog("MIGRATE_ATT_FAIL", f); }
        }
      }
      secLog("MIGRATE_OK", path.basename(legacyDb));
    }
  } catch (e) { secLog("MIGRATE_ERR", String(e && e.message)); }
  if (!fs.existsSync(getDbFile())) fs.writeFileSync(getDbFile(), '{"tasks":[]}', "utf8");
  /* v0.140：清理上次异常退出残留的临时文件（原子写中断留下的 .tmp），避免占空间 */
  try {
    for (const f of [getDbFile() + ".tmp", getDbFile() + ".migrating"]) {
      if (fs.existsSync(f)) { try { fs.unlinkSync(f); } catch (_e) {} }
    }
    try {
      const bd = getBackupDir();
      if (fs.existsSync(bd)) for (const f of fs.readdirSync(bd)) {
        if (f.endsWith(".tmp")) { try { fs.unlinkSync(path.join(bd, f)); } catch (_e) {} }
      }
    } catch (_e) {}
  } catch (_e) {}
}

app.whenReady().then(() => {
  ensureDirs();
  const win = new BrowserWindow({
    width: 1440, height: 920,
    minWidth: 1100, minHeight: 700,
    title: "影视后期 · 模型师任务记录系统",
    icon: path.join(__dirname, "app.ico"),
    backgroundColor: "#0d0f14",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,          // 安全基線：渲染进程不可直接 require（H-1）
      nodeIntegrationInWorker: false,
      nodeIntegrationInSubFrames: false,
      contextIsolation: true,          // 上下文隔离必须开启
      sandbox: true,                   // 渲染进程沙箱
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      spellcheck: false,
      experimentalFeatures: false
    }
  });
  Menu.setApplicationMenu(null);
  __mainWinId = win.webContents.id;    // 供 assertTrusted 使用
  // 禁止新开窗口 / 禁止导航到外部资源（H-1）
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-navigate", (ev, url) => {
    if (!/^file:\/\//i.test(String(url || ""))) {
      ev.preventDefault();
      secLog("NAV_BLOCKED", String(url || "").slice(0, 200));
    }
  });
  win.webContents.on("will-attach-webview", (ev) => ev.preventDefault());
  // 拒绝所有权限请求（无摄像头/麦克风/定位/通知需求）
  try {
    win.webContents.session.setPermissionRequestHandler((_wc, _perm, cb) => cb(false));
    win.webContents.session.setPermissionCheckHandler(() => false);
  } catch (_e) {}
  win.loadFile("index.html");
  // 每次启动重置缩放，避免误触 Ctrl+滚轮 后“文字变大”被记忆
  win.webContents.on("did-finish-load",()=>{try{win.webContents.setZoomFactor(1)}catch(e){}});

  // 把现有数据传给页面
  let init = '{"tasks":[]}';
  let initErr = "";
  /* v0.140 修复（严重）：原来这里用 fs.readFileSync(getDbFile(), "utf8") 直接读原始字节。
     数据文件在 Windows 上是 safeStorage（DPAPI）加密的，带 "ARTVOM-ENC-1\n" 前缀，
     直接当 utf8 读出来是乱码 → 页面 JSON.parse 失败 → 以空库启动 → 用户随便一点保存
     就把空库覆盖到真数据上，整份数据彻底丢失。
     现在统一走 readDataFile()：自动识别加密/明文并解密，读失败时把错误传给页面，
     页面据此进入「只读保护」不再覆盖文件。 */
  try {
    if (fs.existsSync(getDbFile())) init = readDataFile(getDbFile());
  } catch (e) {
    initErr = String((e && e.message) || e);
    secLog("DB_INIT_FAIL", initErr);
    __readOnly = true;   /* v0.147：初始化失败 → 主进程级只读锁开启，拒绝一切写入 */
  }
  ipcMain.on("db:init", (e) => { e.returnValue = init; });
  ipcMain.on("db:init:error", (e) => { e.returnValue = initErr; });
  // 数据文件当前的真实状态（供页面判断是否需要提示/修复）
  ipcMain.on("db:file:info", (e) => {
    try {
      const f = getDbFile();
      const st = fs.existsSync(f) ? fs.statSync(f) : null;
      e.returnValue = { path: f, exists: !!st, size: st ? st.size : 0, mtime: st ? st.mtimeMs : 0 };
    } catch (_e) { e.returnValue = { path: getDbFile(), exists: false, size: 0, mtime: 0 }; }
  });
  // 保存：结构校验 + 状态派生在主进程完成，渲染进程不再是唯一可信方——H-3
  ipcMain.handle("db:save", (e, json) => {
    assertTrusted(e);
    if (__readOnly) return { ok: false, error: "数据处于只读保护状态，无法写入" };
    if (typeof json !== "string" && typeof json !== "object") return { ok: false, error: ERR.P_TYPE };
    const raw = typeof json === "string" ? json : JSON.stringify(json);
    if (raw.length > 300 * 1024 * 1024) return { ok: false, error: ERR.P_SIZE };
    let obj;
    try { obj = JSON.parse(raw); } catch (_e) { return { ok: false, error: ERR.P_JSON }; }
    const bad = validateDB(obj);
    if (bad) { secLog("DB_SAVE_INVALID", bad); return { ok: false, error: bad }; }
    /* v0.147：实际写盘走串行队列，与备份写入互斥；deriveTaskStates 也放进队列，
       保证状态派生和落盘是一个原子操作，不会被并发的备份打断。 */
    return enqueueWrite(() => {
      deriveTaskStates(obj);
      const plain = JSON.stringify(obj);
      writeDataFile(getDbFile(), plain);
      try {
        const back = fs.statSync(getDbFile());
        if (!back.size) throw new Error("写入后文件为空");
      } catch (_e) { throw new Error(ERR.P_WRITE); }
      return { ok: true, size: plain.length, at: Date.now() };
    }).catch((err) => { secLog("DB_SAVE_ERR", String(err && err.message)); return { ok: false, error: err && err.message || ERR.P_WRITE }; });
  });

  /* ===== v0.140 数据急救：查看数据文件状态 / 回到上一版 ===== */
  ipcMain.handle("db:recover:status", (e) => {
    try {
      assertTrusted(e);
      const db = getDbFile(), prev = db + ".prev";
      const stat = (f) => { try { const s = fs.statSync(f); return { exists: true, size: s.size, mtime: s.mtimeMs }; } catch (_e) { return { exists: false, size: 0, mtime: 0 }; } };
      let enc = false;
      try { const fd = fs.openSync(db, "r"); const b = Buffer.alloc(ENC_MAGIC.length); fs.readSync(fd, b, 0, b.length, 0); fs.closeSync(fd); enc = b.toString("utf8") === ENC_MAGIC; } catch (_e) {}
      return { ok: true, db: stat(db), prev: stat(prev), encrypted: enc, dir: getRootDir() };
    } catch (err) { return { ok: false, error: ERR.P_UNKNOWN }; }
  });
  ipcMain.handle("db:recover:prev", (e) => {
    try {
      assertTrusted(e);
      const db = getDbFile(), prev = db + ".prev";
      if (!fs.existsSync(prev)) return { ok: false, error: "没有上一版数据文件（还没保存过两次以上）" };
      let plain;
      try { plain = readDataFile(prev); } catch (_e) { return { ok: false, error: "上一版文件无法解密" }; }
      let obj;
      try { obj = JSON.parse(plain); } catch (_e) { return { ok: false, error: "上一版文件已损坏" }; }
      if (!Array.isArray(obj.tasks)) return { ok: false, error: "上一版文件格式不合法" };
      // 先用当前文件再留一份 .before-recover，避免"恢复"本身把好数据覆盖掉
      try { if (fs.existsSync(db)) fs.copyFileSync(db, db + ".before-recover"); } catch (_e) {}
      writeDataFile(db, plain);
      secLog("DB_RECOVER_PREV", `tasks=${obj.tasks.length}`);
      return { ok: true, plain };
    } catch (err) { secLog("DB_RECOVER_ERR", String(err && err.message)); return { ok: false, error: ERR.P_WRITE }; }
  });
  // 桌面截屏（任务缩略图）：只允许确认后截当前应用窗口，避免静默全屏抓取——L-3
  ipcMain.handle("screen:capture", async (e) => {
    try {
      assertTrusted(e);
      const r = await dialog.showMessageBox(win, {
        type: "question", buttons: ["取消", "开始截图"], defaultId: 1, cancelId: 0,
        title: "截图确认", message: "将截取当前应用窗口作为任务缩略图，是否继续？",
      });
      if (r.response !== 1) return { error: "cancelled" };
      const sources = await desktopCapturer.getSources({
        types: ["window"],
        thumbnailSize: { width: 1600, height: 1000 },
        fetchWindowIcons: false,
      });
      if (!sources.length) return { error: ERR.P_NOENT };
      /* v0.144 修复：原来按标题子串 "模型任务" 找自家窗口，但窗口标题是
         「影视后期 · 模型师任务记录系统」（中间是「模型师任务」），永远匹配不上，
         于是走到 `|| sources[0]` —— 抓到的是 sources 里的第一个窗口，可能是任意软件，
         用户的聊天窗口 / 其他工程窗口就这么被截进任务缩略图里。
         现在优先用窗口自己的 mediaSourceId 精确匹配，匹配不到再退标题，还找不到就直接报错，
         绝不拿"随便一个窗口"凑数。 */
      let own = null;
      try {
        const mid = win && typeof win.getMediaSourceId === "function" ? win.getMediaSourceId() : "";
        if (mid) own = sources.find((s) => s.id === mid) || null;
      } catch (_e) { own = null; }
      /* v0.145 修复：标题兜底串写错了 —— 页面加载后 <title>（艺术家工作台 · 任务管理系统）
         会覆盖 BrowserWindow 构造时的初始标题，"任务记录系统" 永远匹配不上。改按真实标题匹配。 */
      if (!own) own = sources.find((s) => s.name && (s.name.indexOf("任务管理") >= 0 || s.name.indexOf("艺术家工作台") >= 0)) || null;
      if (!own) {
        secLog("CAPTURE_NO_WINDOW", "sources=" + sources.length);
        return { error: "没找到本程序的窗口，已取消截图（避免截到其他软件的画面）" };
      }
      return { dataUrl: own.thumbnail.toDataURL() };
    } catch (err) { secLog("CAPTURE_ERR", String(err && err.message)); return { error: ERR.P_UNKNOWN }; }
  });
  // 备份导出（加密落盘；不再复制明文副本到 C:\artvom\backups）——M-1
  ipcMain.handle("db:backup", (e, json) => {
    try {
      assertTrusted(e);
      const raw = typeof json === "string" ? json : JSON.stringify(json);
      if (raw.length > 300 * 1024 * 1024) return { ok: false, error: ERR.P_SIZE };
      let obj;
      try { obj = JSON.parse(raw); } catch (_e) { return { ok: false, error: ERR.P_JSON }; }
      const bad = validateDB(obj);
      if (bad) return { ok: false, error: bad };
      ensureDirs();
      const name = `备份_${localStamp()}.json`;
      const f = path.join(getBackupDir(), name);
      writeBackupFile(f, JSON.stringify(obj));
      return { ok: true, file: path.basename(f) };
    } catch (err) { secLog("BACKUP_ERR", String(err && err.message)); return { ok: false, error: ERR.P_WRITE }; }
  });

  // ===== 数据路径查看与更改 =====
  ipcMain.handle("data:path:get", (e) => {
    assertTrusted(e);
    return { root: getRootDir(), data: getDataDir(), backups: getBackupDir(), taskRoot: getTaskRoot(), models: getModelRoot(), bridge: getBridgeRoot(), db: getDbFile(), custom: !!loadConfig().root };
  });
  // 任务文件夹根目录：更改（系统目录黑名单 + 绝对路径校验）——H-2/M-4
  ipcMain.handle("taskroot:set", (e, p) => {
    try {
      assertTrusted(e);
      const dir = asString(p, 260).trim();
      if (!/^[a-zA-Z]:[\\\/]/.test(dir) && !dir.startsWith("\\\\")) return { ok: false, error: "请填写本机绝对路径，如 C:\\artvom\\task" };
      if (dir.split(/[\\\/]/).some((seg) => seg === "..")) return { ok: false, error: ERR.P_PATH };
      const abs = path.resolve(dir);
      if (inSysDir(abs)) { secLog("TASKROOT_SYS", abs); return { ok: false, error: ERR.P_SYS }; }
      fs.mkdirSync(abs, { recursive: true });
      try { fs.accessSync(abs, fs.constants.W_OK); } catch (err) { return { ok: false, error: "目标路径不可写" }; }
      const cfg = loadConfig(); cfg.taskRoot = abs;
      saveConfig(cfg);
      return { ok: true, taskRoot: abs };
    } catch (err) { secLog("TASKROOT_ERR", String(err && err.message)); return { ok: false, error: ERR.P_WRITE }; }
  });
  // 任务子文件夹：确保存在 / 打开 / 把附件写入任务文件夹（全部加来源校验 + 路径前缀校验）
  ipcMain.handle("task:dir", (e, taskName) => {
    try {
      assertTrusted(e);
      return { ok: true, dir: ensureTaskDir(asString(taskName, 200)) };
    } catch (err) { secLog("TASKDIR_ERR", String(err && err.message)); return { ok: false, error: ERR.P_UNKNOWN }; }
  });
  // 把文件（base64）保存到任务文件夹：文件名清洗 + 落点必须在任务根目录内 + 大小上限
  ipcMain.handle("task:saveFile", (e, taskName, fileName, dataBase64) => {
    try {
      assertTrusted(e);
      const dir = ensureTaskDir(asString(taskName, 200));
      const root = getTaskRoot();
      // 附件体积上限 512MB（防止渲染进程一次写爆磁盘）
      const b64 = String(dataBase64 || "");
      /* v0.144：base64 编码会让体积膨胀到原来的 4/3。
         原来阈值写 700MB(base64)、提示却说"超过 512MB"，两个数字对不上，
         用户按提示压到 520MB 仍然会被拒。这里统一按「解码后的真实大小」换算。 */
      if (b64.length > 683 * 1024 * 1024) return { ok: false, error: "附件过大（超过 512MB）" };
      let base = asString(fileName, 200).replace(/[\\\/:\*\?"<>\|]/g, "_").replace(/\.+$/, "").trim() || "文件";
      base = base.replace(/^\.+/, "");            // 去掉开头的 . / .. 前缀
      const ext = path.extname(base).slice(0, 20);
      base = (base.slice(0, base.length - ext.length) || "文件").slice(0, 120);
      let f = path.join(dir, base + ext), i = 1;
      while (fs.existsSync(f)) f = path.join(dir, `${base}(${i++})${ext}`);
      if (!insideRoot(f, root)) {                 // 纵深防御：即便清洗失效也不允许越界
        secLog("SAVEFILE_ESCAPE", f);
        return { ok: false, error: ERR.P_PATH };
      }
      writeFileSafe(f, Buffer.from(b64, "base64"));
      return { ok: true, path: f };
    } catch (err) { secLog("SAVEFILE_ERR", String(err && err.message)); return { ok: false, error: ERR.P_WRITE }; }
  });
  // 任务文件夹：选择 / 打开
  ipcMain.handle("folder:pick", async (e, title) => {
    try {
      assertTrusted(e);
      const r = await dialog.showOpenDialog(win, { title: title || "选择任务文件夹", properties: ["openDirectory", "createDirectory"] });
      return r.canceled || !r.filePaths.length ? null : r.filePaths[0];
    } catch (err) { return null; }
  });
  // 打开文件夹：只允许打开「数据根目录 / 任务根目录 / 模型库目录 / 桥接目录」之下、且必须是文件夹（禁止拉起 exe/lnk）
  ipcMain.handle("folder:open", async (e, p) => {
    try {
      assertTrusted(e);
      const target = path.resolve(asString(p, 500));
      /* v0.112：数据路径面板里 6 个路径都要能点开。
         只放行这几个固定根（data/backups/task/models/bridge 都在 root 下，
         taskRoot 可自定义到别处，所以单独列出），仍是"白名单 + 必须在根内"两道关。 */
      const allowed = [getRootDir(), getTaskRoot(), getModelRoot(), getBridgeRoot()];
      if (!allowed.some((r) => r && insideRoot(target, r))) { secLog("FOLDER_OPEN_DENY", target); return ERR.P_PATH; }
      /* v0.112：这几个是软件自己的固定目录，没建过就先建出来，点了总能进得去，
         不会出现"文件夹不存在"让人以为是软件坏了 */
      const created = [getRootDir(), getDataDir(), getBackupDir(), getTaskRoot(), getModelRoot(), getBridgeRoot(),
                       path.join(getRootDir(), "deliver")];
      if (created.some((r) => r && path.resolve(r) === target)) { try { fs.mkdirSync(target, { recursive: true }); } catch (_e) {} }
      let st; try { st = fs.statSync(target); } catch (_e) { return ERR.P_NOENT; }
      if (!st.isDirectory()) return "拒绝：只能打开文件夹";
      const r = await shell.openPath(target);
      return r ? "打开失败" : "";
    } catch (err) { secLog("FOLDER_OPEN_ERR", String(err && err.message)); return ERR.P_UNKNOWN; }
  });
  // 窗口提到最前（状态联动提示等场景，避免被其他应用窗口如资源管理器遮挡）
  ipcMain.handle("win:raise", async (e) => {
    try {
      assertTrusted(e);   /* v0.144：补来源校验，与文件头声明的"其他一律拒绝"保持一致 */
      const w = (BrowserWindow.getAllWindows() || [])[0];
      if (w && !w.isDestroyed()) { if (w.isMinimized()) w.restore(); w.show(); w.moveTop(); w.focus(); return true; }
    } catch (_e) {}
    return false;
  });
  // 打开系统"选择文件夹"对话框（配合数据路径「更改…」按钮）
  ipcMain.handle("data:path:pick", async (e) => {    try {
      assertTrusted(e);
      const r = await dialog.showOpenDialog(win, {
        title: "选择数据存放位置（将在所选文件夹下创建 artvom 文件夹）",
        properties: ["openDirectory", "createDirectory"]
      });
      return r.canceled || !r.filePaths.length ? null : r.filePaths[0];
    } catch (e) { return null; }
  });

  // 更改数据路径：两阶段提交（复制 → 逐文件字节校验 → 全部成功才删源）——M-4
  ipcMain.handle("data:path:set", (e, newParent) => {
    try {
      assertTrusted(e);
      const parent = asString(newParent, 260).trim().replace(/[\\\/]+$/, "");
      if (!/^[a-zA-Z]:[\\\/]/.test(parent) && !parent.startsWith("\\\\")) return { ok: false, error: "请填写本机绝对路径，如 D:\\我的数据" };
      if (parent.split(/[\\\/]/).some((seg) => seg === "..")) return { ok: false, error: ERR.P_PATH };
      if (inSysDir(parent)) { secLog("DATAPATH_SYS", parent); return { ok: false, error: ERR.P_SYS }; }
      const newRoot = path.join(parent, "artvom");
      const newData = path.join(newRoot, "data");
      const newBk = path.join(newRoot, "backups");
      const newTask = path.join(newRoot, "task");
      const newModel = path.join(newRoot, "models");
      const newBridge = path.join(newRoot, "bridge");
      const oldRoot = getRootDir();
      const same = path.resolve(oldRoot).toLowerCase() === path.resolve(newRoot).toLowerCase();
      if (same) return { ok: false, error: "新路径与当前路径相同" };
      ensureDirs();
      for (const d of [newRoot, newData, newBk, newTask, newModel, newBridge]) if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true });
      try { fs.accessSync(newData, fs.constants.W_OK); } catch (err) { return { ok: false, error: "目标路径不可写" }; }

      // 阶段一：只复制，不删任何源文件；返回 [{src,dst}]
      const copied = [];
      const copyDir = (src, dst) => {
        if (!fs.existsSync(src)) return;
        if (!fs.existsSync(dst)) fs.mkdirSync(dst, { recursive: true });
        for (const f of fs.readdirSync(src)) {
          const s = path.join(src, f), t = path.join(dst, f);
          let st;
          try { st = fs.statSync(s); } catch (_e) { continue; }
          if (st.isDirectory()) copyDir(s, t);
          else {
            fs.copyFileSync(s, t);
            copied.push({ src: s, dst: t });
          }
        }
      };
      /* v0.140 修复（严重）：原来只复制 data / backups 两个目录，
         task（每个任务的附件、截图、工程文件）、models（模型库文件）、bridge（Maya 桥接文件）
         全部留在旧位置没跟着走 —— 迁移完任务记录还在，但点附件就是"文件不存在"。
         现在 6 个目录一起迁；taskRoot 若被用户自定义到别处，则保持原样不去动它。 */
      copyDir(getDataDir(), newData);
      copyDir(getBackupDir(), newBk);
      const customTaskRoot = loadConfig().taskRoot;
      if (!customTaskRoot) copyDir(path.join(oldRoot, "task"), newTask);
      copyDir(path.join(oldRoot, "models"), newModel);
      copyDir(path.join(oldRoot, "bridge"), newBridge);

      // 阶段二：逐文件字节数校验，任一失败 → 全部回滚（删除新位置已复制的文件），源数据完好
      const bad = copied.filter((c) => {
        try { return fs.statSync(c.src).size !== fs.statSync(c.dst).size; } catch (_e) { return true; }
      });
      if (bad.length) {
        for (const c of copied) { try { fs.unlinkSync(c.dst); } catch (_e) {} }
        secLog("DATAPATH_VERIFY_FAIL", `${bad.length}/${copied.length}`);
        return { ok: false, error: `迁移校验失败（${bad.length} 个文件），已回滚，原数据未改动` };
      }

      /* v0.140 修复（严重）：先把新路径写进配置，再删源文件。
         原来的顺序是「删光源文件 → saveConfig」，一旦 saveConfig 失败（磁盘满、配置目录只读），
         数据已经搬到新位置、配置却还指着旧位置，而旧位置已被清空 → 下次启动读到空目录，
         用户看到的就是"数据全没了"。现在配置先行，删源失败也不影响可用性。 */
      __customRoot = newRoot;
      const cfg = loadConfig();
      cfg.root = newRoot;
      if (!saveConfig(cfg)) {
        // 配置写不进去：立刻回滚（新位置文件删掉，源文件原样保留）
        for (const c of copied) { try { fs.unlinkSync(c.dst); } catch (_e) {} }
        __customRoot = oldRoot;
        secLog("DATAPATH_CFG_FAIL", newRoot);
        return { ok: false, error: "数据路径配置写入失败，已回滚，原数据未改动" };
      }

      // 阶段三：配置已指向新位置，才删除源文件（失败也不致命，旧文件只是残留）
      let removed = 0;
      for (const c of copied) { try { fs.unlinkSync(c.src); removed++; } catch (_e) {} }
      try { if (fs.existsSync(getDataDir()) && !fs.readdirSync(getDataDir()).length) fs.rmdirSync(getDataDir()); } catch (_e) {}
      try { if (fs.existsSync(getBackupDir()) && !fs.readdirSync(getBackupDir()).length) fs.rmdirSync(getBackupDir()); } catch (_e) {}
      try { if (fs.existsSync(oldRoot) && !fs.readdirSync(oldRoot).length) fs.rmdirSync(oldRoot); } catch (_e) {}

      secLog("DATAPATH_OK", `moved=${copied.length}`);
      return { ok: true, root: newRoot, data: newData, backups: newBk, moved: removed };
    } catch (err) { secLog("DATAPATH_ERR", String(err && err.message)); return { ok: false, error: ERR.P_WRITE }; }
  });
});

app.on("window-all-closed", () => app.quit());

/* ===== 自动备份：每天一份，保留最近 30 份（存 C:\artvom\backups） ===== */
const { ipcMain: bkIpc } = require("electron");
/* __pad / todayDs 已在文件顶部安全层定义，此处不再重复声明 */
bkIpc.handle("backup:auto", (e, json) => {
  try {
    assertTrusted(e);
    if (__readOnly) return { ok: false, error: "数据处于只读保护状态，无法写入" };
    const raw = typeof json === "string" ? json : JSON.stringify(json);
    if (raw.length > 300 * 1024 * 1024) return { ok: false, error: ERR.P_SIZE };
    let obj; try { obj = JSON.parse(raw); } catch (_e) { return { ok: false, error: ERR.P_JSON }; }
    const bad = validateDB(obj); if (bad) return { ok: false, error: bad };
    const isEmpty = !(obj.tasks || []).length && !Object.keys(obj.worklogs || {}).length &&
                    !(obj.models || []).length && !(obj.samples || []).length && !(obj.trash || []).length;
    const f = path.join(getBackupDir(), `备份_${todayDs()}.json`);
    if (isEmpty) return { ok: true, skipped: true };
    /* v0.147：走写入队列，写完立刻回读 JSON.parse 校验，损坏的备份直接删除不留坑 */
    return enqueueWrite(() => {
      ensureDirs();
      const dir = getBackupDir();
      if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
      if (!fs.existsSync(f)) {
        writeBackupFile(f, JSON.stringify(obj));
        try { JSON.parse(readDataFile(f)); }       /* 回读校验：加密/明文都走 readDataFile */
        catch (_e) { try { fs.unlinkSync(f); } catch (__e) {} throw new Error("备份写入后校验失败，已删除损坏文件"); }
      }
      let files = fs.readdirSync(dir).filter((x) => x.startsWith("备份_") && x.endsWith(".json")).sort();
      while (files.length > 30) { try { fs.unlinkSync(path.join(dir, files.shift())); } catch (_e) {} }
      return { ok: true, skipped: false };
    }).catch((err) => { secLog("BK_AUTO_ERR", String(err && err.message)); return { ok: false, error: err && err.message || ERR.P_WRITE }; });
  } catch (err) { secLog("BK_AUTO_ERR", String(err && err.message)); return { ok: false, error: ERR.P_WRITE }; }
});
/* v0.125：手动「保存今日备份到本地」时同步落一条带时间戳的备份记录（不会覆盖当天自动备份） */
bkIpc.handle("backup:manual", (e, json) => {
  try {
    assertTrusted(e);
    if (__readOnly) return { ok: false, error: "数据处于只读保护状态，无法写入" };
    const raw = typeof json === "string" ? json : JSON.stringify(json);
    if (raw.length > 300 * 1024 * 1024) return { ok: false, error: ERR.P_SIZE };
    let obj; try { obj = JSON.parse(raw); } catch (_e) { return { ok: false, error: ERR.P_JSON }; }
    const bad = validateDB(obj); if (bad) return { ok: false, error: bad };
    const stamp = localStamp();
    const f = path.join(getBackupDir(), `备份_${stamp}.json`);
    return enqueueWrite(() => {
      ensureDirs();
      const dir = getBackupDir();
      if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
      writeBackupFile(f, JSON.stringify(obj));
      try { JSON.parse(readDataFile(f)); }
      catch (_e) { try { fs.unlinkSync(f); } catch (__e) {} throw new Error("备份写入后校验失败，已删除损坏文件"); }
      let files = fs.readdirSync(dir).filter((x) => x.startsWith("备份_") && x.endsWith(".json")).sort();
      while (files.length > 30) { try { fs.unlinkSync(path.join(dir, files.shift())); } catch (_e) {} }
      return { ok: true };
    }).catch((err) => { secLog("BK_MANUAL_ERR", String(err && err.message)); return { ok: false, error: err && err.message || ERR.P_WRITE }; });
  } catch (err) { secLog("BK_MANUAL_ERR", String(err && err.message)); return { ok: false, error: ERR.P_WRITE }; }
});
bkIpc.handle("backup:list", (e) => {
  try {
    assertTrusted(e);
    const dir = getBackupDir();
    if (!fs.existsSync(dir)) return [];
    return fs.readdirSync(dir).filter((x) => x.endsWith(".json")).sort().reverse()
      .map((x) => ({ file: x, date: x.replace(/^备份_/, "").replace(/\.json$/, ""), size: fs.statSync(path.join(dir, x)).size }));
  } catch (err) { return []; }
});
bkIpc.handle("backup:delete", (e, file) => {
  try {
    assertTrusted(e);
    const dir = getBackupDir();
    const f = path.join(dir, path.basename(asString(file, 200)));
    if (!insideRoot(f, dir)) return { ok: false, error: ERR.P_PATH };
    if (fs.existsSync(f)) fs.unlinkSync(f);
    return { ok: true };
  } catch (err) { secLog("BK_DEL_ERR", String(err && err.message)); return { ok: false, error: ERR.P_UNKNOWN }; }
});
bkIpc.handle("backup:read", (e, file) => {
  try {
    assertTrusted(e);
    const dir = getBackupDir();
    const f = path.join(dir, path.basename(asString(file, 200)));
    if (!insideRoot(f, dir)) return "";
    if (!fs.existsSync(f)) return "";
    return readDataFile(f);          // 自动识别加密备份 / 旧明文备份
  } catch (err) { secLog("BK_READ_ERR", String(err && err.message)); return ""; }
});

/* ===== 数据完整性自检：工时账本哈希链 + 全栈指纹 + 文件校验 —— H-3 延伸 ===== */
// 全栈指纹：对整份数据（剔除指纹字段本身）算 SHA-256，任何一项数据被外部改动都会对不上
function globalIntegrityHash(obj) {
  const copy = Object.assign({}, obj);
  delete copy.__ledger;
  delete copy.__integrity;
  return crypto.createHash("sha256").update(JSON.stringify(copy)).digest("hex");
}
bkIpc.handle("data:integrity", (e) => {
  try {
    assertTrusted(e);
    let plain;
    try { plain = readDataFile(getDbFile()); } catch (_e) { return { ok: false, error: "数据文件无法解密" }; }
    let obj; try { obj = JSON.parse(plain); } catch (_e) { return { ok: false, error: ERR.P_JSON }; }
    const stored = obj.__ledger;
    const rows = [];
    const wl = obj.worklogs || {};
    Object.keys(wl).sort().forEach((d) => {
      const w = wl[d] || {};
      rows.push([d, w.start || "", w.end || "", Array.isArray(w.ot) ? w.ot : [], w.kind || ""]);
    });
    const calc = ledgerHash(rows);
    // 全栈校验：落盘数据里的指纹 vs 按当前落盘内容重算的指纹
    const gi = obj.__integrity || null;
    const calcAll = globalIntegrityHash(obj);
    const globalOk = gi ? gi.hash === calcAll : null;
    // 各数据块计数（展示用）
    const counts = {
      tasks: (obj.tasks || []).length,
      // v0.114：只统计有实际内容的工时日（过滤渲染过程产生的空记录）
      worklogs: Object.keys(obj.worklogs || {}).filter((k) => { const w = obj.worklogs[k]; return w && typeof w === "object" && (w.start || w.end || w.leave || w.activity || w.note || w.idleUsed || (Array.isArray(w.ot) && w.ot.length)); }).length,
      models: (obj.models || []).length,
      samples: (obj.samples || []).length,
      projects: ((obj.settings && Array.isArray(obj.settings.projects)) ? obj.settings.projects.length : 0),
      settings: obj.settings ? 1 : 0,
    };
    const encrypted = fs.existsSync(getDbFile()) && (() => {
      try { const b = fs.readFileSync(getDbFile()); return b.slice(0, ENC_MAGIC.length).toString("utf8") === ENC_MAGIC; } catch (_e) { return false; }
    })();
    return {
      ok: true, encrypted, days: rows.length, counts,
      ledgerOk: stored ? stored.hash === calc : null,
      ledgerHash: calc, storedHash: stored ? stored.hash : null,
      signedAt: stored ? stored.at : null,
      globalOk, globalHash: calcAll, globalAt: gi ? gi.at : null,
    };
  } catch (err) { secLog("INTEGRITY_ERR", String(err && err.message)); return { ok: false, error: ERR.P_UNKNOWN }; }
});
// 极简 zip（仅 store 不压缩，Windows 自带解压可正常使用；速度快、无第三方依赖）
function __crc32(buf) {
  if (!__crc32._t) {
    const tb = new Int32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      tb[n] = c;
    }
    __crc32._t = tb;
  }
  const t = __crc32._t;
  let c = 0 ^ -1;
  for (let i = 0; i < buf.length; i++) c = (c >>> 8) ^ t[(c ^ buf[i]) & 0xff];
  return (c ^ -1) >>> 0;
}
function zipDir(srcDir, zipFile, baseName) {
  const files = [];
  const walk = (dir, rel) => {
    for (const name of fs.readdirSync(dir)) {
      const abs = path.join(dir, name);
      let st;
      try { st = fs.statSync(abs); } catch (e) { continue; }
      if (st.isDirectory()) walk(abs, rel ? rel + "/" + name : name);
      else if (st.isFile()) files.push({ abs, rel: rel ? rel + "/" + name : name, st });
    }
  };
  walk(srcDir, baseName);
  const chunks = [], central = [];
  let offset = 0;
  for (const f of files) {
    let data;
    try { data = fs.readFileSync(f.abs); } catch (e) { continue; }
    const nameBuf = Buffer.from(f.rel.replace(/\\/g, "/"), "utf8");
    const crc = __crc32(data);
    const d0 = new Date(f.st.mtime);
    const t = ((d0.getHours() << 11) | (d0.getMinutes() << 5) | Math.floor(d0.getSeconds() / 2)) & 0xffff;
    const dt = (((Math.max(1980, d0.getFullYear()) - 1980) << 9) | ((d0.getMonth() + 1) << 5) | d0.getDate()) & 0xffff;
    const here = offset;
    const lh = Buffer.alloc(30);
    lh.writeUInt32LE(0x04034b50, 0); lh.writeUInt16LE(20, 4); lh.writeUInt16LE(0x0800, 6);
    /* v0.144 修复：本地文件头的字段偏移整体错位。
       正确布局：0 签名 / 4 版本 / 6 标志 / 8 压缩法 / 10 修改时间 / 12 修改日期 /
                 14 CRC32 / 18 压缩后大小 / 22 原始大小 / 26 文件名长度 / 28 扩展长度。
       原来写的是：10 没写(恒 0)、12 写了「时间」、14 写了「日期」、16 写了「CRC」、
       18 又被「原始大小」覆盖。中央目录是对的，所以 Windows 自带解压不报错，
       但 unzip -t / 7-Zip 严格校验会发现本地头 CRC 不对，包内文件时间戳也是乱的。 */
    lh.writeUInt16LE(0, 8);                      /* 压缩方法：0 = 存储(不压缩) */
    lh.writeUInt16LE(t, 10);                     /* 修改时间 */
    lh.writeUInt16LE(dt, 12);                    /* 修改日期 */
    lh.writeUInt32LE(crc, 14);                   /* CRC32 */
    lh.writeUInt32LE(data.length, 18);           /* 压缩后大小 */
    lh.writeUInt32LE(data.length, 22);           /* 原始大小 */
    lh.writeUInt16LE(nameBuf.length, 26); lh.writeUInt16LE(0, 28);
    chunks.push(lh, nameBuf, data);
    const ch = Buffer.alloc(46);
    ch.writeUInt32LE(0x02014b50, 0); ch.writeUInt16LE(20, 4); ch.writeUInt16LE(20, 6); ch.writeUInt16LE(0x0800, 8);
    /* 中央目录同一套偏移：10 压缩法 / 12 时间 / 14 日期 / 16 CRC（原来时间日期也写偏了） */
    ch.writeUInt16LE(0, 10); ch.writeUInt16LE(t, 12); ch.writeUInt16LE(dt, 14);
    ch.writeUInt32LE(crc, 16); ch.writeUInt32LE(data.length, 20); ch.writeUInt32LE(data.length, 24);
    ch.writeUInt16LE(nameBuf.length, 28); ch.writeUInt16LE(0, 30); ch.writeUInt16LE(0, 32);
    ch.writeUInt16LE(0, 34); ch.writeUInt16LE(0, 36); ch.writeUInt32LE(0, 38);
    // 42：「本地头偏移」必须指向该文件本地头的起始位置
    ch.writeUInt32LE(here, 42);
    central.push(Buffer.concat([ch, nameBuf]));
    offset += 30 + nameBuf.length + data.length;
  }
  const cdir = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(0, 4); end.writeUInt16LE(0, 6);
  end.writeUInt16LE(central.length, 8); end.writeUInt16LE(central.length, 10);
  end.writeUInt32LE(cdir.length, 12); end.writeUInt32LE(offset, 16); end.writeUInt16LE(0, 20);
  fs.writeFileSync(zipFile, Buffer.concat([...chunks, cdir, end]));
  return { count: files.length, size: fs.statSync(zipFile).size };
}
// 打包入口：默认输出到 桌面，避免文件放进 artvom 根目录被自己压进去
ipcMain.handle("data:pack", async (e) => {
  try {
    assertTrusted(e);
    ensureDirs();
    const root = getRootDir();
    if (!fs.existsSync(root)) return { ok: false, error: "数据文件夹不存在：" + root };
    const stamp = `${todayDs()}_${__pad(new Date().getHours())}${__pad(new Date().getMinutes())}${__pad(new Date().getSeconds())}`;
    let destDir;
    try { destDir = app.getPath("desktop"); } catch (e) { destDir = path.dirname(root); }
    if (!destDir || !fs.existsSync(destDir)) destDir = path.dirname(root);
    const zipFile = path.join(destDir, `artvom_数据打包_${stamp}.zip`);
    const r = zipDir(root, zipFile, "artvom");
    return { ok: true, file: zipFile, dir: destDir, count: r.count, size: r.size, root };
  } catch (err) { secLog("PACK_ERR", String(err && err.message)); return { ok: false, error: ERR.P_WRITE }; }
});

/* ===== 导入打包：与 data:pack 对称的一键导入（换电脑迁移用） =====
   选 zip → 解压到临时目录 → 校验结构 → 现有数据改名留底 → 落位到数据根目录 →
   主数据文件若因换机（DPAPI 密钥跟 Windows 账户走）解不开，自动用 backups 里
   最新明文备份恢复并重新用本机密钥加密，用户全程不用手工碰文件。 */
// 在 zip 尾部找 EOCD（中央目录结束记录）
function __findEOCD(buf) {
  const min = Math.max(0, buf.length - 22 - 65536);
  for (let i = buf.length - 22; i >= min; i--) {
    if (buf.readUInt32LE(i) === 0x06054b50) return i;
  }
  return -1;
}
/* 解 zip 到 destDir。支持 store(0) 与 deflate(8)：本软件自打包是 store，
   用户用 Windows「发送到→压缩文件夹」重压的是 deflate，两种都认。 */
function unzipDir(zipFile, destDir) {
  const buf = fs.readFileSync(zipFile);
  const eocd = __findEOCD(buf);
  if (eocd < 0) throw new Error("不是有效的 zip 压缩包");
  const count = buf.readUInt16LE(eocd + 10);
  let off = buf.readUInt32LE(eocd + 16);
  const entries = [];
  for (let i = 0; i < count; i++) {
    if (off + 46 > buf.length || buf.readUInt32LE(off) !== 0x02014b50) throw new Error("zip 中央目录损坏");
    const method = buf.readUInt16LE(off + 10);
    const csize = buf.readUInt32LE(off + 20);
    const usize = buf.readUInt32LE(off + 24);
    const nameLen = buf.readUInt16LE(off + 28);
    const extraLen = buf.readUInt16LE(off + 30);
    const commentLen = buf.readUInt16LE(off + 32);
    const lho = buf.readUInt32LE(off + 42);
    /* 统一成正斜杠再判断与存储：Windows 自带压缩（PowerShell/资源管理器）生成的 zip
       目录条目以反斜杠结尾（artvom\data\），不先归一会被误判成文件写出去 */
    const name = buf.slice(off + 46, off + 46 + nameLen).toString("utf8").replace(/\\/g, "/");
    if (name && !name.endsWith("/")) entries.push({ name, method, csize, usize, lho });
    off += 46 + nameLen + extraLen + commentLen;
  }
  let n = 0;
  for (const f of entries) {
    // 防 zip-slip：逐段校验，拒绝 .. / 盘符 / 绝对路径
    const segs = f.name.replace(/\\/g, "/").split("/").filter(Boolean);
    if (!segs.length || segs.some((s) => s === "." || s === ".." || /^[A-Za-z]:$/.test(s))) continue;
    const dst = path.join(destDir, ...segs);
    if (!insideRoot(dst, destDir)) continue;
    if (f.usize > 2 * 1024 * 1024 * 1024) throw new Error("压缩包内文件过大（超过 2GB）");
    if (f.lho + 30 > buf.length || buf.readUInt32LE(f.lho) !== 0x04034b50) throw new Error("zip 本地头损坏");
    const ln = buf.readUInt16LE(f.lho + 26);
    const le = buf.readUInt16LE(f.lho + 28);
    const dataStart = f.lho + 30 + ln + le;
    const raw = buf.slice(dataStart, dataStart + f.csize);
    let data;
    if (f.method === 0) data = raw;
    else if (f.method === 8) data = zlib.inflateRawSync(raw);
    else throw new Error("压缩包里有不支持的压缩格式（method " + f.method + "），请用 Windows 自带压缩重新打包");
    ensureDir(path.dirname(dst));
    writeFileSafe(dst, data);
    n++;
  }
  return n;
}
ipcMain.handle("data:unpack", async (e) => {
  try {
    assertTrusted(e);
    const win = __mainWinId ? BrowserWindow.fromId(__mainWinId) : null;
    const opts = {
      title: "选择数据打包压缩包（artvom_数据打包_*.zip）",
      filters: [{ name: "数据打包压缩包", extensions: ["zip"] }],
      properties: ["openFile"],
    };
    const r = win ? await dialog.showOpenDialog(win, opts) : await dialog.showOpenDialog(opts);
    if (r.canceled || !r.filePaths.length) return { ok: false, cancelled: true };
    const zipFile = r.filePaths[0];

    const root = getRootDir();
    const parent = path.dirname(root);
    const stamp = `${todayDs()}_${__pad(new Date().getHours())}${__pad(new Date().getMinutes())}${__pad(new Date().getSeconds())}`;
    // 临时目录放在数据根目录的同级：同一盘符，最后一步 rename 落位是瞬时的
    const tmp = path.join(parent, `.artvom_unpack_${stamp}`);
    const cleanup = () => { try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (_e) {} };
    fs.mkdirSync(tmp, { recursive: true });
    let extracted = 0;
    try { extracted = unzipDir(zipFile, tmp); }
    catch (err) {
      cleanup();
      secLog("UNPACK_ZIP_ERR", String(err && err.message));
      return { ok: false, error: String(err && err.message) || "解压失败" };
    }
    // 结构校验：定位压缩包里的 artvom 根（正常是 tmp/artvom；也兼容直接压着 data/backups 的包）
    let srcRoot = path.join(tmp, "artvom");
    if (!fs.existsSync(srcRoot)) {
      if (fs.existsSync(path.join(tmp, "data")) || fs.existsSync(path.join(tmp, "backups"))) srcRoot = tmp;
      else { cleanup(); return { ok: false, error: "压缩包里没找到 artvom 数据文件夹，请确认是本软件「打包数据」生成的 zip" }; }
    }
    if (!fs.existsSync(path.join(srcRoot, "data")) && !fs.existsSync(path.join(srcRoot, "backups"))) {
      cleanup(); return { ok: false, error: "压缩包结构不完整（缺少 data 或 backups 文件夹）" };
    }
    // 现有数据完整留底（改名而非删除，随时可手动换回）
    let bakDir = "";
    if (fs.existsSync(root)) {
      bakDir = path.join(parent, `artvom.bak-${stamp}`);
      try { fs.renameSync(root, bakDir); }
      catch (err) {
        cleanup();
        secLog("UNPACK_BAK_ERR", String(err && err.message));
        return { ok: false, error: "现有数据文件夹被占用，无法留底（请关闭占用它的窗口后重试）" };
      }
    }
    // 落位
    try { fs.renameSync(srcRoot, root); }
    catch (err) {
      try { if (bakDir) fs.renameSync(bakDir, root); } catch (_e) {}   // 回滚留底
      cleanup();
      secLog("UNPACK_MOVE_ERR", String(err && err.message));
      return { ok: false, error: ERR.P_WRITE };
    }
    cleanup();
    // DPAPI 处理：主数据文件若本机解不开（换机场景），自动改用 backups 里最新明文备份恢复
    let restored = false, backupUsed = "", decryptFail = false;
    const dbFile = getDbFile();
    if (fs.existsSync(dbFile)) {
      try { readDataFile(dbFile); }
      catch (_e) {
        decryptFail = true;
        const bdir = getBackupDir();
        if (fs.existsSync(bdir)) {
          const cands = fs.readdirSync(bdir).filter((f) => f.endsWith(".json")).sort();
          for (let i = cands.length - 1; i >= 0; i--) {   // 从最新往前找一份可解析的明文备份
            try {
              const txt = fs.readFileSync(path.join(bdir, cands[i]), "utf8");
              if (!validateDB(JSON.parse(txt))) {
                writeDataFile(dbFile, txt);               // 用本机密钥重新加密落盘
                restored = true; backupUsed = cands[i];
                break;
              }
            } catch (_e2) {}
          }
        }
        if (!restored) {
          // 解不开又没有可用备份：改名隔离，避免启动直接进只读保护
          try { fs.renameSync(dbFile, dbFile + ".undecryptable-" + stamp); } catch (_e2) {}
        }
      }
    }
    secLog("UNPACK_OK", `files=${extracted} restored=${restored}`);
    return { ok: true, root, bakDir, extracted, restored, backupUsed, decryptFail };
  } catch (err) { secLog("UNPACK_ERR", String(err && err.message)); return { ok: false, error: ERR.P_UNKNOWN }; }
});
// 在资源管理器中定位到刚打好的压缩包（选中该文件）
ipcMain.handle("shell:showItem", async (e, p) => {
  try {
    assertTrusted(e);
    const f = path.resolve(asString(p, 500));
    // 只允许定位到「数据根目录」或「打包输出目录（桌面）」内的文件，禁止借它探测任意路径
    const desktop = (() => { try { return app.getPath("desktop"); } catch (_e) { return ""; } })();
    const allowed = [getRootDir(), getTaskRoot(), desktop].filter(Boolean);
    if (!allowed.some((r) => insideRoot(f, r))) { secLog("SHOWITEM_DENY", f); return false; }
    if (!fs.existsSync(f)) return false;
    shell.showItemInFolder(f);
    return true;
  } catch (err) { secLog("SHOWITEM_ERR", String(err && err.message)); return false; }
});


/* ============================================================================
   v0.109 模型库：主程序侧存储 + Maya 桥接
   ----------------------------------------------------------------------------
   架构说明（与 Maya 插件的关系）
     · 模型库属于本软件，模型文件与截图统一存在 <数据根>/models/<id>/
     · Maya 端只是一个"桥"：负责把库里的模型导入场景、把场景里的模型截图回传
     · 两端不直接通信，靠文件握手：<数据根>/bridge/
         import/  ← 本软件写「请导入这个模型」的请求，Maya 轮询取走
         inbox/   ← Maya 写完「我存档了这些模型」的包，本软件轮询收进库
         status/  ← 双向状态回执，用于界面显示成功/失败
   ========================================================================== */

// 模型库根目录：C:\artvom\models
function getModelRoot() { return path.join(getRootDir(), "models"); }
// 桥接目录：C:\artvom\bridge
function getBridgeRoot() { return path.join(getRootDir(), "bridge"); }
function ensureDir(p) { if (!fs.existsSync(p)) fs.mkdirSync(p, { recursive: true }); return p; }

// 资产 id 只允许安全字符，防止借 id 穿越目录
function safeAssetId(v) {
  const s = String(v || "").replace(/[^A-Za-z0-9_-]/g, "").slice(0, 40);
  return s || "";
}
// 写 JSON（原子写：先写临时文件再改名，避免 Maya 读到半个文件）
function writeJsonAtomic(file, obj) {
  ensureDir(path.dirname(file));
  const tmp = file + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(obj, null, 2), "utf8");
  if (fs.existsSync(file)) { try { fs.unlinkSync(file); } catch (_e) {} }
  fs.renameSync(tmp, file);
}
function readJsonSafe(file, dflt) {
  try { return JSON.parse(fs.readFileSync(file, "utf8")); } catch (_e) { return dflt; }
}

// ---- 保存一个模型资产（模型文件 + 截图） ----
ipcMain.handle("models:saveAsset", async (e, payload) => {
  try {
    assertTrusted(e);
    const p = payload && typeof payload === "object" ? payload : {};
    const id = safeAssetId(p.id) || ("a" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6));
    const dir = ensureDir(path.join(getModelRoot(), id));
    const out = { ok: true, id, dir, files: [], shot: "" };

    // 模型文件（可能多个）：base64 → 落盘
    const list = Array.isArray(p.files) ? p.files.slice(0, 20) : [];
    for (const f of list) {
      const name = asString(f && f.name, 200).replace(/[\\\/:*?"<>|]/g, "_").replace(/\.+$/, "").trim() || "model";
      const b64 = String((f && f.data) || "");
      /* v0.144：同上，640MB 原文对应 base64 约 854MB（原来写 900MB，实际允许到 675MB） */
      if (b64.length > 854 * 1024 * 1024) { out.ok = false; out.error = "模型文件过大（超过 640MB）"; break; }
      if (!b64) continue;
      const dst = path.join(dir, name);
      if (!insideRoot(dst, getModelRoot())) { secLog("MODEL_ESCAPE", dst); continue; }
      // 剥掉 data:...;base64, 前缀；没有前缀说明已经是裸 base64，直接用
      const cut = b64.indexOf("base64,");
      writeFileSafe(dst, Buffer.from(cut >= 0 ? b64.slice(cut + 7) : b64, "base64"));
      out.files.push(dst);
    }
    // 截图：单独存一份 shot，模型库缩略图和大图都读它
    const shotData = String(p.shot || "");
    if (shotData.startsWith("data:image")) {
      const ext = /image\/png/.test(shotData) ? ".png" : /image\/jpeg/.test(shotData) ? ".jpg" : ".png";
      const sp = path.join(dir, "shot" + ext);
      if (insideRoot(sp, getModelRoot())) {
        writeFileSafe(sp, Buffer.from(shotData.slice(shotData.indexOf("base64,") + 7), "base64"));
        out.shot = sp;
      }
    }
    return out;
  } catch (err) { secLog("MODEL_SAVE_ERR", String(err && err.message)); return { ok: false, error: ERR.P_WRITE }; }
});


// ---- 删除模型资产目录（回收站清空时调用） ----
ipcMain.handle("models:deleteAsset", async (e, id) => {
  try {
    assertTrusted(e);
    const sid = safeAssetId(id);
    if (!sid) return false;
    const dir = path.join(getModelRoot(), sid);
    if (!insideRoot(dir, getModelRoot())) return false;
    if (fs.existsSync(dir)) fs.rmSync(dir, { recursive: true, force: true });
    return true;
  } catch (err) { secLog("MODEL_DEL_ERR", String(err && err.message)); return false; }
});

// ---- 在资源管理器里定位模型 ----
ipcMain.handle("models:reveal", async (e, id) => {
  try {
    assertTrusted(e);
    const sid = safeAssetId(id);
    const dir = path.join(getModelRoot(), sid);
    if (!sid || !insideRoot(dir, getModelRoot()) || !fs.existsSync(dir)) return false;
    shell.showItemInFolder(dir);
    return true;
  } catch (_e) { return false; }
});

// ---- 写「请导入这个模型」请求，Maya 端轮询取走 ----
ipcMain.handle("bridge:requestImport", async (e, req) => {
  try {
    assertTrusted(e);
    const r = req && typeof req === "object" ? req : {};
    const rid = "imp" + Date.now().toString(36) + Math.random().toString(36).slice(2, 5);
    // 模型文件路径必须位于模型库内
    const src = path.resolve(asString(r.file, 600));
    if (src && !insideRoot(src, getModelRoot())) { secLog("BRIDGE_IMPORT_DENY", src); return { ok: false, error: ERR.P_PATH }; }
    if (src && !fs.existsSync(src)) return { ok: false, error: ERR.P_NOENT };
    const payload = {
      reqId: rid, action: "import", ts: Date.now(),
      assetId: safeAssetId(r.assetId), name: asString(r.name, 200),
      file: src,               // 绝对路径，Maya 直接读
      mode: r.mode === "reference" ? "reference" : "copy",  // copy=导入副本，reference=引用
      by: asString(r.by, 60),
    };
    writeJsonAtomic(path.join(getBridgeRoot(), "import", rid + ".json"), payload);
    return { ok: true, reqId: rid };
  } catch (err) { secLog("BRIDGE_IMPORT_ERR", String(err && err.message)); return { ok: false, error: ERR.P_WRITE }; }
});


// ---- 收 Maya 回传的存档包：把文件搬进模型库，返回规范化结果给前端入库 ----
ipcMain.handle("bridge:drainInbox", async (e) => {
  try {
    assertTrusted(e);
    const inbox = path.join(getBridgeRoot(), "inbox");
    if (!fs.existsSync(inbox)) return { ok: true, items: [] };
    const items = [];
    const files = fs.readdirSync(inbox).filter((f) => f.endsWith(".json"));
    for (const jf of files) {
      const full = path.join(inbox, jf);
      const meta = readJsonSafe(full, null);
      if (!meta) { try { fs.unlinkSync(full); } catch (_e) {} continue; }
      const id = safeAssetId(meta.assetId) || ("a" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6));
      const dir = ensureDir(path.join(getModelRoot(), id));
      const moved = [];
      // Maya 把模型文件与截图放在同目录同名文件夹里
      for (const fname of (Array.isArray(meta.files) ? meta.files : [])) {
        const src = path.join(inbox, asString(fname, 200));
        if (!fs.existsSync(src)) continue;
        const base = path.basename(src).replace(/[\\\/:*?"<>|]/g, "_");
        const dst = path.join(dir, base);
        if (!insideRoot(dst, getModelRoot())) continue;
        try { fs.renameSync(src, dst); moved.push(dst); }
        catch (_e) { try { fs.copyFileSync(src, dst); fs.unlinkSync(src); moved.push(dst); } catch (_e2) {} }
      }
      if (meta.shotFile) {
        const ssrc = path.join(inbox, asString(meta.shotFile, 200));
        if (fs.existsSync(ssrc)) {
          const sdst = path.join(dir, "shot" + path.extname(ssrc).toLowerCase());
          if (insideRoot(sdst, getModelRoot())) {
            try { fs.renameSync(ssrc, sdst); } catch (_e) { try { fs.copyFileSync(ssrc, sdst); fs.unlinkSync(ssrc); } catch (_e2) {} }
            meta.shotPath = sdst;
          }
        }
      }
      meta.id = id; meta.dir = dir; meta.savedFiles = moved;
      items.push(meta);
      try { fs.unlinkSync(full); } catch (_e) {}
      // 写回执，Maya 端可显示「已入库」
      writeJsonAtomic(path.join(getBridgeRoot(), "status", id + ".json"),
        { assetId: id, ok: true, at: Date.now(), count: moved.length });
    }
    return { ok: true, items };
  } catch (err) { secLog("BRIDGE_INBOX_ERR", String(err && err.message)); return { ok: false, error: ERR.P_UNKNOWN, items: [] }; }
});

// ---- 桥接状态：Maya 是否在线（Maya 端每 2 秒写一次心跳） ----
ipcMain.handle("bridge:status", async (e) => {
  try {
    assertTrusted(e);
    const hb = path.join(getBridgeRoot(), "status", "maya_heartbeat.json");
    const d = readJsonSafe(hb, null);
    const online = !!(d && d.ts && Date.now() - d.ts < 8000);
    return { ok: true, online, last: d ? d.ts : 0, mayaVersion: d ? d.mayaVersion : "", user: d ? d.user : "" };
  } catch (_e) { return { ok: true, online: false, last: 0 }; }
});


/* ===== v0.147：外部工具同步预留接口 =====
   为后续接入 Jira / TAPD / 飞书多维表格 / 自定义脚本等外部工具预留的通用同步入口。
   · sync:export  —— 把当前数据库（或指定片段）导出为 JSON，写入到「同步目录」下的 export.json，
                    方便外部脚本拉取。payload 可选 { scope:'all'|'tasks'|'models', ids:[...] }
   · sync:import  —— 从「同步目录」下的 import.json 读取外部推送的数据，返回给渲染层做合并。
                     主进程只负责文件读写与权限校验，合并策略由渲染层决定（避免误覆盖用户数据）。
   · sync:dir     —— 返回同步目录路径（默认 <根目录>/sync），渲染层可据此提示用户把文件放哪。 */
function getSyncRoot() { return ensureDir(path.join(getRootDir(), "sync")); }
ipcMain.handle("sync:dir", async (e) => { try { assertTrusted(e); return getSyncRoot(); } catch (_e) { return ERR.P_UNKNOWN; } });
ipcMain.handle("sync:export", async (e, payload) => {
  try {
    assertTrusted(e);
    const p = typeof payload === "string" ? JSON.parse(payload || "{}") : (payload || {});
    const dataFile = path.join(getSyncRoot(), "export.json");
    const raw = readDataFile(getDbFile());
    const db = raw ? JSON.parse(raw) : {};
    const out = { exportedAt: new Date().toISOString(), scope: p.scope || "all", data: null };
    if (p.scope === "tasks" && Array.isArray(p.ids)) {
      out.data = (db.tasks || []).filter(t => p.ids.includes(t.id));
    } else if (p.scope === "models") {
      out.data = db.models || [];
    } else {
      out.data = db;
    }
    fs.writeFileSync(dataFile, JSON.stringify(out, null, 2), "utf8");
    return { ok: true, path: dataFile, count: Array.isArray(out.data) ? out.data.length : 1 };
  } catch (err) { return { ok: false, error: err && err.message ? err.message : String(err) }; }
});
ipcMain.handle("sync:import", async (e) => {
  try {
    assertTrusted(e);
    const dataFile = path.join(getSyncRoot(), "import.json");
    if (!fs.existsSync(dataFile)) return { ok: false, error: "import.json 不存在" };
    const txt = fs.readFileSync(dataFile, "utf8");
    const obj = JSON.parse(txt);
    /* 把已读入的 import 文件改名归档，避免下次重复导入 */
    const arc = path.join(getSyncRoot(), `import.${Date.now()}.json`);
    try { fs.renameSync(dataFile, arc); } catch (_) {}
    return { ok: true, data: obj, archivedAs: path.basename(arc) };
  } catch (err) { return { ok: false, error: err && err.message ? err.message : String(err) }; }
});
