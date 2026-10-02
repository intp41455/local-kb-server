# CLAUDE.md

## 项目目的

「天禧随行知识库」本地服务：把电脑上某个桌面应用的知识库通过**局域网直连 + 云盘中转**两条路径暴露给手机，手机端是一个可离线、可安装到主屏的 PWA。

核心设计约束（`README.md` 一/三节）：

- **对上游数据只读**：手机端的新建/修改写到独立的 `overlay/` 目录，**不碰原生目录一个字节**；读取时两路合并（原生→`local`，overlay→`app`，云端条目→`cloud`），按标题归一化（`norm_title`）去重。可靠性优先于功能完整性——最坏情况是手机端笔记丢了，原数据始终完好。
- **云盘侧是单向备份**（rclone 上传），不是双向同步，刻意避开冲突合并问题。
- 已知未做项：无鉴权（仅限局域网自用）、单用户、无三方合并、`http.server` 单线程高并发会阻塞（见 `README.md` 八节）。

## 技术栈

- **服务端** `kb-server/server.py`（约 1329 行）：Python 3.8+，仅标准库（`http.server` / `ssl` / `gzip` / `json`），**零第三方依赖**。可选：Pillow（图片压缩，没有就回退原图）、openssl（自动生成自签证书，Windows 下找 Git 自带的 `openssl.exe`）。
- **前端** `kb-server/web/`：原生 JS + Service Worker + Web App Manifest，**无构建步骤**。数据存 IndexedDB（库名 `txkb`，5 个 store：`items`/`texts`/`blobs`/`ops`/`meta`）；SW 只缓存应用壳，`/api/` 请求绝不缓存。
- **云盘备份** `gdrive-sync/`：rclone（外部二进制，需自备 `rclone.exe`）+ 两个 `.bat` 脚本（GBK 编码，终端显示乱码属正常）。

## 目录结构

```
kb-server/
├── server.py            # 服务端全部逻辑：HTTP/HTTPS 双端口、API、证书生成、overlay 合并
└── web/                 # 手机端 PWA
    ├── index.html       # 应用主体（约 1878 行，CSS/JS 全内联）
    ├── sw.js            # Service Worker
    ├── manifest.webmanifest
    └── icons/
gdrive-sync/
├── 1-连接Google账号.bat     # 一次性 rclone OAuth 授权
├── 2-同步到Google网盘.bat   # 分 5 步备份（cusnote/cloudthumb/notethumb/db/手机端笔记）
├── rclone.conf.example      # 配置模板（真实凭据不入库）
└── 使用说明_Google网盘同步.txt
docs/
└── architecture.{png,html,json}   # 架构图三件套
```

运行数据全部落在 `kb-server/data/`（**已 gitignore**）：`state.json`、`journal.jsonl`、`certs/`（CA 与服务器证书）、`backup/`、`trash/`、`overlay/`（手机端可写笔记）、`imgcache/`、`access.log`、`server.log`。

## 安装 / 运行

无包管理器、无构建、无测试。

```bash
# 1. 指定上游知识库目录（路径因应用与账号而异，源码不写死）
set KB_BASE_DIR=C:\ProgramData\<应用名>\user\<你的用户ID>   # Windows cmd
# 默认值：~/kb-data（未设置时）

# 2. 启动（首次自动用 openssl 生成自签 CA + 服务器证书）
python kb-server/server.py

# 3. 手机访问 http://<电脑IP>:8787/setup 安装 CA 证书（一次性）
#    之后用 https://<电脑IP>:8788 访问，可"添加到主屏"当 App

# 4. 可选云盘备份：先跑 1-连接Google账号.bat，之后每次跑 2-同步到Google网盘.bat
#    （需要 VPN 能连通 Google；rclone.exe 需自行放到 gdrive-sync/）
```

- 端口：**HTTP 8787 只做证书引导**（其余请求 302 跳 HTTPS）；**HTTPS 8788 承载完整应用**。顺序不能反过来——证书没装前 HTTPS 会被浏览器拦截。
- 无 openssl 时降级为纯 HTTP 8788（PWA/SW 不可用）。
- 无测试套件；验证方式为在 `data/state.json` 的 `validation` 字段记录的"写入链路验证"（在 cusnote 创建测试笔记观察官方客户端是否上云）。

## 关键约定与坑点

1. **KB_BASE_DIR 是运行前提**。`KB_BASE` 决定 `CUSNOTE_DIR`（`$KB_BASE/cusnote`）与 `CLOUDTHUMB_DIR`（`$KB_BASE/cloudthumb`）。`gdrive-sync/2-同步到Google网盘.bat` 里的 `SRC` 路径（当前为 `C:\ProgramData\Lenovo\AIAgent\kd\user\10338710475`）是**硬编码的本机路径**，换环境要改。

2. **上游笔记目录约定**：每个笔记是一个目录，必须含 `meta.json` 才计入索引；正文优先读 `note.md`，否则回退解析 `note.h5`（自定义 JSON 树：`text`/`linebreak`/`paragraph` 节点，见 `h5_to_text`）。创建笔记时 `meta.json` 会写入固定的 `deviceInfo` 与 `source: "PKB"`。

3. **写回双模式**：`state.json` 的 `writeback.{create,update,delete}` 可为 `"kb"`（写原生 cusnote，ID 为 UUIDv4）或 `"app"`（写 overlay，ID 为 `app-<16位hex>`）。`find_note_dir` 先查 cusnote 再查 overlay。云端条目（无本地目录）"删除"只会加入 `hidden` 列表，不产生文件操作。

4. **幂等操作队列**：`/api/ops` 批量写入靠 `state.json` 的 `processedOps`（保留最近 800 条）做去重，手机端重放同一 `opId` 直接返回 `dup: true`。改动操作逻辑时不要破坏这个语义。

5. **更新前必备份**：`op_update` 会把 `note.md`/`meta.json`/`note.h5` 拷进 `data/backup/<ts>_<nid>/`；删除走 `trash/` 而不是真删（`trash_action` 支持 restore/purge）。改这两个函数时保留备份/回收站行为。

6. **凭据与私钥绝不入库**（`.gitignore` 强制）：`gdrive-sync/rclone.conf`（Google OAuth token）、`kb-server/data/certs/`（CA 与服务器私钥）、`*.key`、`*.pem`、`token.txt`。仓库只保留 `rclone.conf.example` 模板。

7. **证书签名与 IP 绑定**：`server-meta.json` 记录当前证书覆盖的局域网 IP 签名，IP 变化或证书 390 天到期后自动重新签发；改动 `ensure_certs` 时保持"沿用已有证书 vs 重签"的判断逻辑（缺 openssl 时只要旧证书存在就沿用）。

8. **退出码 42 = 手动停止**：`server.py` 的 `sys.exit(42)` 是给外层启动脚本识别"用户主动 Ctrl+C，不要自动重启"的信号；其他异常退出码为 1。

9. **索引缓存**：`get_index` 有 8 秒 TTL + 根目录 mtime 双重失效；所有写操作后必须调用 `invalidate_index()`（新增写路径时别忘）。

10. **LAN IP 检测**：`_ps_ipv4_list` 用 PowerShell 枚举物理网卡 IPv4 以排除 VPN 地址，Windows 专属；非 Windows 环境走 socket 兜底。

11. **bat 脚本是 GBK 编码**（非 UTF-8），用 Read/编辑器按 UTF-8 打开会显示乱码，不代表文件损坏。

12. **Pillow 为可选依赖**：`PILLOW_OK` 标志控制 `/api/image` 的 webp 压缩（质量 76，缓存进 `imgcache/`，未安装则直接回原图）；不要假设一定可用。
