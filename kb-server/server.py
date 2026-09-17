#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
天禧随行知识库 · 本地服务
===========================================
在电脑上运行，为手机提供知识库的读取与写入服务。

  · HTTPS 8788   完整应用（手机端 PWA 的页面、数据与同步接口）
  · HTTP  8787   证书引导页（在手机上安装本地 CA 证书用，其余请求自动跳转到 HTTPS）

依赖：Python 3.8+
  可选 Pillow（图片压缩，pip install Pillow）
  可选 openssl（Git for Windows 自带；用于自动生成 HTTPS 证书）
"""

import base64
import gzip
import http.server
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid

# ---------------------------------------------------------------- 基础配置
APP_NAME = "天禧随行知识库"
APP_VERSION = "1.0.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.path.join(BASE_DIR, "data")
CERT_DIR = os.path.join(DATA_DIR, "certs")
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
TRASH_DIR = os.path.join(DATA_DIR, "trash")
OVERLAY_DIR = os.path.join(DATA_DIR, "overlay")
IMGCACHE_DIR = os.path.join(DATA_DIR, "imgcache")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
JOURNAL_PATH = os.path.join(DATA_DIR, "journal.jsonl")
CLOUD_ITEMS_PATH = os.path.join(DATA_DIR, "extra-cloud-items.json")

# 上游知识库数据目录 —— 必须按你的实际环境通过环境变量指定。
#
# 用途：本服务把某个「桌面应用的知识库目录」暴露给手机端读写。
# 该目录因应用与账号而异，所以这里不写死，默认指向用户目录下的 kb-data，
# 你需要改成真实路径。例如（Windows，注意不要带行尾空格）：
#
#   set KB_BASE_DIR=C:\ProgramData\<应用名>\user\<你的用户ID>        # cmd
#   $env:KB_BASE_DIR="C:\ProgramData\<应用名>\user\<你的用户ID>"     # PowerShell
#   export KB_BASE_DIR=/path/to/kb-data                              # Linux / macOS
KB_BASE = os.environ.get("KB_BASE_DIR", os.path.join(os.path.expanduser("~"), "kb-data"))
CUSNOTE_DIR = os.path.join(KB_BASE, "cusnote")
CLOUDTHUMB_DIR = os.path.join(KB_BASE, "cloudthumb")

PORT_HTTP = 8787    # 仅证书引导
PORT_HTTPS = 8788   # 完整应用

SCAN_TTL = 8.0          # 索引扫描缓存秒数
MAX_BODY = 64 * 1024 * 1024
MAX_TEXT_IDS = 300

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from PIL import Image
    PILLOW_OK = True
except Exception:
    PILLOW_OK = False


# ---------------------------------------------------------------- 小工具
def now_ms():
    return int(time.time() * 1000)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return default


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def clean_title(raw):
    t = (raw or "").strip()
    t = re.sub(r"\.note$", "", t, flags=re.I).strip()
    return t or "未命名笔记"


def norm_title(raw):
    t = (raw or "").strip()
    t = re.sub(r"\.note$", "", t, flags=re.I)
    t = re.sub(r"\s+", "", t).lower()
    return t


def _ps_ipv4_list():
    """用 PowerShell 枚举已启用的物理网卡 IPv4（排除 VPN/虚拟网卡/回环/APIPA）。"""
    try:
        cmd = ("Get-NetAdapter | Where-Object {$_.Status -eq 'Up' -and -not $_.Virtual} | "
               "ForEach-Object { (Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 "
               "-ErrorAction SilentlyContinue).IPAddress }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                             capture_output=True, timeout=20)
        ips = []
        for line in (out.stdout or b"").decode("utf-8", "replace").splitlines():
            ip = line.strip()
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip) and not ip.startswith(("127.", "169.254.")):
                if ip not in ips:
                    ips.append(ip)
        return ips
    except Exception:
        return []


def get_lan_ips():
    """本机所有可能被手机访问的局域网 IPv4（优先物理网卡，避免把 VPN 地址当 LAN）。"""
    ips = _ps_ipv4_list()
    if ips:
        return ips
    # 兜底 1：默认路由出口地址（若挂着 VPN，可能落到 VPN 网卡）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith(("127.", "169.254.")):
            ips.append(ip)
    except Exception:
        pass
    # 兜底 2：主机名解析
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith(("127.", "169.254.")) and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def get_lan_ip():
    ips = get_lan_ips()
    return ips[0] if ips else "127.0.0.1"


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d B" % n)
        n /= 1024
    return "%.1f TB" % n


def dir_total_size(path):
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
    except OSError:
        pass
    return total


# ---------------------------------------------------------------- 状态与日志
STATE_LOCK = threading.Lock()


def default_state():
    return {
        "writeback": {"create": "kb", "update": "kb", "delete": "kb"},
        "token": None,
        "favorites": [],
        "trashed": {},
        "hidden": [],
        "processedOps": [],
        "validation": {"result": "pending", "checkedAt": None,
                       "note": "写入链路验证：在 cusnote 中创建测试笔记，观察官方客户端是否上云。"},
    }


def load_state():
    st = read_json(STATE_PATH)
    if not isinstance(st, dict):
        st = default_state()
    base = default_state()
    for k, v in base.items():
        if k not in st:
            st[k] = v
    return st


def save_state(st):
    with STATE_LOCK:
        write_json_atomic(STATE_PATH, st)


def journal(entry):
    entry = dict(entry)
    entry.setdefault("t", now_ms())
    try:
        with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 知识库索引
SCAN_LOCK = threading.Lock()
_scan = {"at": 0.0, "root_mtime": 0.0, "items": [], "cloud": {}}


def list_images(note_dir):
    img_dir = os.path.join(note_dir, "image")
    out = []
    if os.path.isdir(img_dir):
        for n in sorted(os.listdir(img_dir)):
            p = os.path.join(img_dir, n)
            if os.path.isfile(p):
                out.append(n)
    return out


def load_meta(note_dir, fallback_title):
    meta = read_json(os.path.join(note_dir, "meta.json"))
    if not isinstance(meta, dict):
        meta = {}
    try:
        st = os.stat(note_dir)
        fb_ct = int(st.st_ctime * 1000)
        fb_ut = int(st.st_mtime * 1000)
    except OSError:
        fb_ct = fb_ut = now_ms()
    ct = meta.get("createTime") or fb_ct
    ut = meta.get("updateTime") or fb_ut
    try:
        ct = int(ct)
    except Exception:
        ct = fb_ct
    try:
        ut = int(ut)
    except Exception:
        ut = fb_ut
    title = meta.get("title") or fallback_title
    return meta, title, ct, ut


def scan_note_root(root, src, hidden, trashed):
    items = []
    try:
        names = os.listdir(root)
    except OSError:
        return items
    for name in names:
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        if not os.path.isfile(os.path.join(d, "meta.json")):
            continue
        if name in hidden or name in trashed:
            continue
        meta, title, ct, ut = load_meta(d, name)
        imgs = list_images(d)
        md_p = os.path.join(d, "note.md")
        md_sz = os.path.getsize(md_p) if os.path.isfile(md_p) else 0
        att = meta.get("attachment") if isinstance(meta.get("attachment"), dict) else {}
        item = {
            "id": name,
            "title": clean_title(title),
            "ct": ct,
            "ut": ut,
            "src": src,
            "imgs": len(imgs),
            "img0": imgs[0] if imgs else None,
            "md": md_sz,
            "att": {
                "image": int(att.get("image") or 0),
                "audio": int(att.get("audio") or 0),
                "video": int(att.get("video") or 0),
                "doodle": int(att.get("doodle") or 0),
            },
        }
        item["thumb"] = ("/api/image?id=%s&name=%s&w=360" % (name, imgs[0])) if imgs else None
        items.append(item)
    return items


def cloud_thumb_path(fid):
    """缩略图文件名为 <fid>#<N>.webp（N 不固定），扫描目录取第一个 webp。"""
    d = os.path.join(CLOUDTHUMB_DIR, fid)
    if not os.path.isdir(d):
        return None
    try:
        for n in os.listdir(d):
            if n.lower().endswith(".webp"):
                p = os.path.join(d, n)
                if os.path.isfile(p):
                    return p
    except OSError:
        pass
    return None


def load_cloud_items():
    raw = read_json(CLOUD_ITEMS_PATH, [])
    cloud = {}
    if isinstance(raw, list):
        for e in raw:
            if not isinstance(e, dict) or not e.get("id"):
                continue
            if str(e.get("type") or "note") == "directory":
                continue
            fid = str(e["id"])
            cloud[fid] = {
                "id": fid,
                "title": clean_title(e.get("name") or fid),
                "nt": norm_title(e.get("name") or fid),
                "ut": int(e.get("mtime") or 0),
                "type": str(e.get("type") or "note"),
            }
    return cloud


def get_index(force=False):
    """返回 (items, counts)。带 TTL 与根目录 mtime 失效。"""
    with SCAN_LOCK:
        try:
            root_mtime = os.stat(CUSNOTE_DIR).st_mtime
        except OSError:
            root_mtime = 0.0
        fresh = (time.time() - _scan["at"]) < SCAN_TTL and _scan["root_mtime"] == root_mtime
        if fresh and not force:
            return _scan["items"], _scan["cloud"]

        st = load_state()
        hidden = set(st.get("hidden") or [])
        trashed = set((st.get("trashed") or {}).keys())

        items = scan_note_root(CUSNOTE_DIR, "local", hidden, trashed)
        overlay_items = scan_note_root(OVERLAY_DIR, "app", hidden, trashed)

        cloud = load_cloud_items()
        seen = set(norm_title(i["title"]) for i in items)
        seen |= set(norm_title(i["title"]) for i in overlay_items)

        items = items + overlay_items
        for fid, c in sorted(cloud.items(), key=lambda kv: -(kv[1]["ut"] or 0)):
            if c["nt"] in seen:
                continue
            seen.add(c["nt"])
            it = {
                "id": fid,
                "title": c["title"],
                "ct": c["ut"],
                "ut": c["ut"],
                "src": "cloud",
                "imgs": 0,
                "img0": None,
                "md": 0,
                "att": {"image": 0, "audio": 0, "video": 0, "doodle": 0},
                "thumb": ("/api/cthumb?fid=%s" % fid) if cloud_thumb_path(fid) else None,
            }
            items.append(it)

        items.sort(key=lambda x: -(x["ut"] or 0))
        counts = {
            "local": sum(1 for i in items if i["src"] == "local"),
            "app": sum(1 for i in items if i["src"] == "app"),
            "cloud": sum(1 for i in items if i["src"] == "cloud"),
            "total": len(items),
        }
        _scan["at"] = time.time()
        _scan["root_mtime"] = root_mtime
        _scan["items"] = items
        _scan["cloud"] = counts
        return items, counts


def invalidate_index():
    with SCAN_LOCK:
        _scan["at"] = 0.0


def find_note_dir(nid):
    """返回 (dir, src)；找不到返回 (None, None)"""
    if re.match(r"^[0-9A-Za-z_\-]+$", nid or ""):
        p = os.path.join(CUSNOTE_DIR, nid)
        if os.path.isdir(p):
            return p, "local"
        p = os.path.join(OVERLAY_DIR, nid)
        if os.path.isdir(p):
            return p, "app"
    return None, None


def h5_to_text(node):
    if not isinstance(node, dict):
        return ""
    t = node.get("type")
    if t == "text":
        return node.get("text") or ""
    if t == "linebreak":
        return "\n"
    children = node.get("children") or []
    inner = "".join(h5_to_text(c) for c in children)
    if t == "paragraph":
        return inner + "\n\n"
    return inner


def read_note_text(note_dir):
    md = ""
    md_p = os.path.join(note_dir, "note.md")
    if os.path.isfile(md_p):
        try:
            with open(md_p, "r", encoding="utf-8", errors="replace") as f:
                md = f.read()
        except Exception:
            md = ""
    if md.strip():
        return md
    h5_p = os.path.join(note_dir, "note.h5")
    if os.path.isfile(h5_p) and os.path.getsize(h5_p) > 2:
        root = read_json(h5_p)
        if isinstance(root, dict):
            txt = h5_to_text(root)
            txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
            if txt:
                return txt
    return md


# ---------------------------------------------------------------- 写回操作
def safe_image_name(name):
    name = os.path.basename((name or "").replace("\\", "/"))
    name = name.strip()
    if not name or name in (".", ".."):
        return None
    if len(name) > 180:
        base, ext = os.path.splitext(name)
        name = base[:150] + ext[:20]
    return name


def op_create(op, st):
    mode = st["writeback"].get("create", "kb")
    title = (op.get("title") or "").strip()[:120] or "未命名笔记"
    markdown = op.get("markdown") or ""
    images = op.get("images") or []
    want_id = str(op.get("id") or "")
    if mode == "kb":
        nid = want_id if re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", want_id) else str(uuid.uuid4())
        root = CUSNOTE_DIR
    else:
        nid = want_id if re.match(r"^app-[0-9a-zA-Z]{8,32}$", want_id) else "app-" + uuid.uuid4().hex[:16]
        root = OVERLAY_DIR
    d = os.path.join(root, nid)
    if os.path.exists(d):
        return {"ok": False, "error": "笔记 ID 已存在：%s" % nid}
    os.makedirs(os.path.join(d, "image"), exist_ok=True)
    os.makedirs(os.path.join(d, "audio"), exist_ok=True)
    saved = []
    for im in images[:20]:
        if not isinstance(im, dict):
            continue
        nm = safe_image_name(im.get("name"))
        data = im.get("data")
        if not nm or not data:
            continue
        try:
            blob = base64.b64decode(data)
        except Exception:
            continue
        if len(blob) > 20 * 1024 * 1024:
            continue
        with open(os.path.join(d, "image", nm), "wb") as f:
            f.write(blob)
        saved.append(nm)
    md_p = os.path.join(d, "note.md")
    with open(md_p, "w", encoding="utf-8") as f:
        f.write(markdown)
    ts = now_ms()
    meta = {
        "attachment": {"audio": 0, "audio_track": 0, "doodle": 0, "image": len(saved), "video": 0},
        "coverPath": "",
        "createTime": ts,
        "deviceInfo": "901CA529-7684-478F-95A2-183D2D549137",
        "deviceType": "PC",
        "source": "PKB",
        "title": title,
        "updateTime": ts,
        "version": "1",
    }
    write_json_atomic(os.path.join(d, "meta.json"), meta)
    if mode == "kb":
        try:
            open(os.path.join(d, "note.h5"), "w", encoding="utf-8").close()
        except Exception:
            pass
    journal({"op": "create", "id": nid, "mode": mode, "title": title, "images": len(saved)})
    invalidate_index()
    return {"ok": True, "id": nid, "mode": mode}


def op_update(op, st):
    nid = op.get("id") or ""
    d, src = find_note_dir(nid)
    if not d:
        return {"ok": False, "error": "笔记不存在（可能为云端条目）"}
    mode = st["writeback"].get("update", "kb")
    # 备份原文件
    ts = now_ms()
    bdir = os.path.join(BACKUP_DIR, "%d_%s" % (ts, nid))
    try:
        os.makedirs(bdir, exist_ok=True)
        for fn in ("note.md", "meta.json", "note.h5"):
            p = os.path.join(d, fn)
            if os.path.isfile(p):
                shutil.copy2(p, os.path.join(bdir, fn))
    except Exception:
        pass
    if op.get("markdown") is not None:
        with open(os.path.join(d, "note.md"), "w", encoding="utf-8") as f:
            f.write(op.get("markdown") or "")
    for im in (op.get("images") or [])[:20]:
        if not isinstance(im, dict):
            continue
        nm = safe_image_name(im.get("name"))
        data = im.get("data")
        if not nm or not data:
            continue
        try:
            blob = base64.b64decode(data)
        except Exception:
            continue
        if len(blob) > 20 * 1024 * 1024:
            continue
        os.makedirs(os.path.join(d, "image"), exist_ok=True)
        with open(os.path.join(d, "image", nm), "wb") as f:
            f.write(blob)
    meta_p = os.path.join(d, "meta.json")
    meta = read_json(meta_p) or {}
    if not isinstance(meta, dict):
        meta = {}
    if op.get("title"):
        meta["title"] = (op.get("title") or "").strip()[:120]
    meta["updateTime"] = now_ms()
    meta.setdefault("attachment", {"audio": 0, "audio_track": 0, "doodle": 0, "image": 0, "video": 0})
    try:
        meta["attachment"]["image"] = len(list_images(d))
    except Exception:
        pass
    write_json_atomic(meta_p, meta)
    journal({"op": "update", "id": nid, "mode": mode, "src": src,
             "title": op.get("title") or "", "backup": os.path.basename(bdir)})
    invalidate_index()
    return {"ok": True, "id": nid, "mode": mode}


def op_delete(op, st):
    nid = op.get("id") or ""
    d, src = find_note_dir(nid)
    mode = st["writeback"].get("delete", "kb")
    if not d:
        # 云端条目：仅从清单隐藏
        hidden = st.setdefault("hidden", [])
        if nid not in hidden:
            hidden.append(nid)
        journal({"op": "delete", "id": nid, "mode": "hide", "note": "cloud-only item hidden"})
        invalidate_index()
        return {"ok": True, "id": nid, "mode": "hide"}
    ts = now_ms()
    tdir = os.path.join(TRASH_DIR, "%d_%s" % (ts, nid))
    try:
        shutil.copytree(d, tdir)
    except Exception as e:
        return {"ok": False, "error": "移入回收站失败：%s" % e}
    try:
        shutil.rmtree(d)
    except Exception as e:
        return {"ok": False, "error": "删除失败：%s" % e}
    with STATE_LOCK:
        st.setdefault("trashed", {})[nid] = {"at": ts, "mode": mode, "src": src,
                                             "title": clean_title((read_json(os.path.join(tdir, "meta.json")) or {}).get("title") or nid)}
    journal({"op": "delete", "id": nid, "mode": mode, "src": src, "trash": os.path.basename(tdir)})
    invalidate_index()
    return {"ok": True, "id": nid, "mode": mode}


OPS = {"create": op_create, "update": op_update, "delete": op_delete}


def apply_ops(ops, st):
    results = []
    done = set(st.get("processedOps") or [])
    for op in (ops or [])[:50]:
        if not isinstance(op, dict):
            continue
        op_id = str(op.get("opId") or "")
        typ = op.get("type")
        if op_id and op_id in done:
            results.append({"opId": op_id, "ok": True, "dup": True})
            continue
        fn = OPS.get(typ)
        if not fn:
            results.append({"opId": op_id, "ok": False, "error": "未知操作类型 %s" % typ})
            continue
        try:
            r = fn(op, st)
        except Exception as e:
            r = {"ok": False, "error": str(e)}
        r["opId"] = op_id
        r["type"] = typ
        results.append(r)
        if op_id and r.get("ok"):
            done.add(op_id)
    st["processedOps"] = list(done)[-800:]
    invalidate_index()
    return results


# ---------------------------------------------------------------- 回收站
def trash_list():
    st = load_state()
    out = []
    for name in sorted(os.listdir(TRASH_DIR)) if os.path.isdir(TRASH_DIR) else []:
        p = os.path.join(TRASH_DIR, name)
        if not os.path.isdir(p):
            continue
        parts = name.split("_", 1)
        ts = int(parts[0]) if parts and parts[0].isdigit() else 0
        nid = parts[1] if len(parts) > 1 else name
        meta = read_json(os.path.join(p, "meta.json")) or {}
        out.append({
            "id": nid,
            "title": clean_title((meta.get("title") if isinstance(meta, dict) else "") or nid),
            "at": ts,
            "size": dir_total_size(p),
        })
    out.sort(key=lambda x: -(x["at"] or 0))
    return out


def trash_action(action, nid, st):
    if action == "restore":
        target = None
        for name in (os.listdir(TRASH_DIR) if os.path.isdir(TRASH_DIR) else []):
            if name.endswith("_" + nid):
                target = os.path.join(TRASH_DIR, name)
                break
        if not target:
            return {"ok": False, "error": "回收站中未找到该笔记"}
        # 还原到原位置：优先本地目录
        dest_local = os.path.join(CUSNOTE_DIR, nid)
        dest_app = os.path.join(OVERLAY_DIR, nid)
        if os.path.exists(dest_local) or os.path.exists(dest_app):
            return {"ok": False, "error": "原位置已存在同名笔记"}
        dest = dest_app if nid.startswith("app-") else dest_local
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            shutil.copytree(target, dest)
            shutil.rmtree(target)
        except Exception as e:
            return {"ok": False, "error": "还原失败：%s" % e}
        with STATE_LOCK:
            (st.get("trashed") or {}).pop(nid, None)
        journal({"op": "restore", "id": nid})
        invalidate_index()
        return {"ok": True, "id": nid}
    if action == "purge":
        for name in (os.listdir(TRASH_DIR) if os.path.isdir(TRASH_DIR) else []):
            if name.endswith("_" + nid):
                p = os.path.join(TRASH_DIR, name)
                if os.path.isdir(p):
                    shutil.rmtree(p)
                    with STATE_LOCK:
                        (st.get("trashed") or {}).pop(nid, None)
                    journal({"op": "purge", "id": nid})
                    invalidate_index()
                    return {"ok": True, "id": nid}
        return {"ok": False, "error": "回收站中未找到该笔记"}
    if action == "unhide":
        hidden = st.get("hidden") or []
        if nid in hidden:
            hidden.remove(nid)
            journal({"op": "unhide", "id": nid})
            invalidate_index()
            return {"ok": True, "id": nid}
        return {"ok": False, "error": "未找到该隐藏条目"}
    return {"ok": False, "error": "未知操作"}


# ---------------------------------------------------------------- 证书
def find_openssl():
    cands = [
        shutil.which("openssl"),
        r"C:\Program Files\Git\usr\bin\openssl.exe",
        r"C:\Program Files\Git\mingw64\bin\openssl.exe",
        r"C:\Program Files (x86)\Git\usr\bin\openssl.exe",
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def run_cmd(args, cwd=None):
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, timeout=60, creationflags=flags)
        return p.returncode, (p.stderr or b"").decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)


def ensure_certs(lan_ips):
    """确保 CA 与服务器证书存在且覆盖当前全部局域网地址。返回 (https_ready, message)"""
    if isinstance(lan_ips, str):
        lan_ips = [lan_ips]
    lan_ips = [ip for ip in (lan_ips or []) if ip] or ["127.0.0.1"]
    os.makedirs(CERT_DIR, exist_ok=True)
    ca_key = os.path.join(CERT_DIR, "ca.key")
    ca_crt = os.path.join(CERT_DIR, "ca.crt")
    srv_key = os.path.join(CERT_DIR, "server.key")
    srv_crt = os.path.join(CERT_DIR, "server.crt")
    meta_p = os.path.join(CERT_DIR, "server-meta.json")

    ops = find_openssl()
    if not ops:
        if os.path.isfile(srv_key) and os.path.isfile(srv_crt):
            return True, "未找到 openssl，沿用已有证书"
        return False, "未找到 openssl（可安装 Git for Windows 后重试）"

    if not (os.path.isfile(ca_key) and os.path.isfile(ca_crt)):
        code, err = run_cmd([ops, "req", "-x509", "-newkey", "rsa:2048", "-sha256",
                             "-days", "3650", "-nodes", "-keyout", ca_key, "-out", ca_crt,
                             "-subj", "/CN=Tianxi KB Local CA",
                             "-addext", "basicConstraints=critical,CA:TRUE"])
        if code != 0:
            return False, "生成 CA 失败：%s" % err.strip()[:200]

    meta = read_json(meta_p, {}) or {}
    lan_sig = ",".join(lan_ips)
    need_srv = True
    if os.path.isfile(srv_key) and os.path.isfile(srv_crt) and isinstance(meta, dict):
        if meta.get("lan") == lan_sig and float(meta.get("expires") or 0) > time.time() + 30 * 86400:
            need_srv = False
    if need_srv:
        csr = os.path.join(CERT_DIR, "server.csr")
        ext = os.path.join(CERT_DIR, "ext.cnf")
        with open(ext, "w", encoding="utf-8") as f:
            f.write("subjectAltName=DNS:localhost,IP:127.0.0.1"
                    + "".join(",IP:%s" % ip for ip in lan_ips) + "\n")
            f.write("basicConstraints=CA:FALSE\n")
            f.write("keyUsage=digitalSignature,keyEncipherment\n")
            f.write("extendedKeyUsage=serverAuth\n")
        code, err = run_cmd([ops, "req", "-newkey", "rsa:2048", "-sha256", "-nodes",
                             "-keyout", srv_key, "-out", csr, "-subj", "/CN=tianxi-kb"])
        if code != 0:
            return False, "生成服务器证书请求失败：%s" % err.strip()[:200]
        code, err = run_cmd([ops, "x509", "-req", "-in", csr, "-CA", ca_crt, "-CAkey", ca_key,
                             "-CAcreateserial", "-out", srv_crt, "-days", "390",
                             "-sha256", "-extfile", ext])
        if code != 0:
            return False, "签发服务器证书失败：%s" % err.strip()[:200]
        write_json_atomic(meta_p, {"lan": lan_sig, "expires": time.time() + 390 * 86400,
                                   "generatedAt": now_ms()})
    return True, "ok"


# ---------------------------------------------------------------- HTTP 处理
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml; charset=utf-8",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "TianxiKB/" + APP_VERSION
    protocol_version = "HTTP/1.1"

    # -------- 输出工具
    def _respond(self, code=200, ctype="application/json; charset=utf-8", body=b"",
                 cache="no-store", headers=None, gzip_ok=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        if gzip_ok and len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 6)
            headers = dict(headers or {})
            headers["Content-Encoding"] = "gzip"
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._respond(code=code, body=body, gzip_ok=True)

    def _err(self, code, msg):
        self._json({"ok": False, "error": msg}, code=code)

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return None
        try:
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return None

    def log_message(self, fmt, *args):
        # 控制台静默（避免轮询刷屏），但写入访问日志，便于诊断手机是否连达
        try:
            line = "%s %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                   self.client_address[0], fmt % args)
            with open(os.path.join(DATA_DIR, "access.log"), "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    # -------- 鉴权（默认关闭）
    def _authed(self):
        st = getattr(self.server, "tianxi_state", None) or load_state()
        token = st.get("token")
        if not token:
            return True
        q = self._query()
        supplied = q.get("t", [""])[0] or (self.headers.get("X-Token") or "")
        return supplied == token

    def _query(self):
        import urllib.parse
        if not hasattr(self, "_q_cache"):
            p = urllib.parse.urlparse(self.path)
            self._q_cache = urllib.parse.parse_qs(p.query)
        return self._q_cache

    def _path(self):
        import urllib.parse
        return urllib.parse.urlparse(self.path).path

    # -------- 路由
    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._err(500, "服务器内部错误：%s" % e)
            except Exception:
                pass

    def do_POST(self):
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._err(500, "服务器内部错误：%s" % e)
            except Exception:
                pass

    def _route_get(self):
        path = self._path()
        if path.startswith("/api/"):
            if not self._authed():
                return self._err(401, "访问令牌无效")
            return self._api_get(path)
        return self._serve_static(path)

    def _route_post(self):
        path = self._path()
        if path.startswith("/api/"):
            if not self._authed():
                return self._err(401, "访问令牌无效")
            return self._api_post(path)
        return self._err(404, "not found")

    # -------- API GET
    def _api_get(self, path):
        q = self._query()
        st = load_state()
        if path == "/api/health":
            _items, counts = get_index()
            return self._json({
                "ok": True, "app": APP_NAME, "version": APP_VERSION,
                "lan": self.server.tianxi_lan, "httpPort": PORT_HTTP, "httpsPort": PORT_HTTPS,
                "secure": bool(self.server.tianxi_https),
                "writeback": st.get("writeback"), "validation": st.get("validation"),
                "counts": counts, "pillow": PILLOW_OK,
                "favorites": st.get("favorites") or [],
                "serverTime": now_ms(),
            })
        if path == "/api/items":
            items, counts = get_index()
            clean = [{k: v for k, v in it.items()} for it in items]
            return self._json({"ok": True, "items": clean, "counts": counts,
                               "writeback": st.get("writeback"),
                               "favorites": st.get("favorites") or [],
                               "serverTime": now_ms()})
        if path == "/api/note":
            nid = (q.get("id") or [""])[0]
            d, src = find_note_dir(nid)
            if not d:
                return self._err(404, "笔记不存在（云端条目仅显示标题）")
            meta, title, ct, ut = load_meta(d, nid)
            md = read_note_text(d)
            imgs = list_images(d)
            return self._json({"ok": True, "id": nid, "title": clean_title(title),
                               "ct": ct, "ut": ut, "src": src, "markdown": md,
                               "images": [{"name": n, "url": "/api/image?id=%s&name=%s&w=1400" % (nid, n)} for n in imgs]})
        if path == "/api/image":
            return self._serve_note_image(q)
        if path == "/api/cthumb":
            fid = (q.get("fid") or [""])[0]
            if not re.match(r"^[0-9A-Za-z_#\-]+$", fid):
                return self._err(400, "bad fid")
            p = cloud_thumb_path(fid)
            if p:
                with open(p, "rb") as f:
                    return self._respond(ctype="image/webp", body=f.read(),
                                         cache="max-age=86400")
            return self._err(404, "no thumb")
        if path == "/api/trash":
            return self._json({"ok": True, "items": trash_list()})
        if path == "/api/stats":
            _items, counts = get_index()
            cache_sz = dir_total_size(IMGCACHE_DIR)
            overlay_sz = dir_total_size(OVERLAY_DIR)
            trash = trash_list()
            return self._json({"ok": True, "counts": counts,
                               "imgcacheBytes": cache_sz, "overlayBytes": overlay_sz,
                               "trashCount": len(trash),
                               "writeback": st.get("writeback"),
                               "validation": st.get("validation"),
                               "serverTime": now_ms()})
        return self._err(404, "not found")

    # -------- API POST
    def _api_post(self, path):
        body = self._body_json()
        if body is None:
            return self._err(400, "请求体不是合法 JSON")
        st = load_state()
        if path == "/api/changes":
            items, _counts = get_index()
            since = int(body.get("since") or 0)
            ids = body.get("ids") or []
            known = set(i["id"] for i in items)
            updated = [i for i in items if (i["ut"] or 0) > since]
            removed = [x for x in ids if x not in known]
            return self._json({"ok": True, "updated": updated, "removed": removed,
                               "serverTime": now_ms(),
                               "writeback": st.get("writeback")})
        if path == "/api/texts":
            ids = (body.get("ids") or [])[:MAX_TEXT_IDS]
            texts = {}
            for nid in ids:
                d, _src = find_note_dir(str(nid))
                if d:
                    texts[str(nid)] = read_note_text(d)
            return self._json({"ok": True, "texts": texts})
        if path == "/api/ops":
            results = apply_ops(body.get("ops") or [], st)
            save_state(st)
            return self._json({"ok": True, "results": results, "serverTime": now_ms()})
        if path == "/api/trash":
            r = trash_action(body.get("action") or "", str(body.get("id") or ""), st)
            save_state(st)
            return self._json(r)
        if path == "/api/favorite":
            nid = str(body.get("id") or "")
            favs = st.setdefault("favorites", [])
            on = bool(body.get("on"))
            if on and nid not in favs:
                favs.append(nid)
            if not on and nid in favs:
                favs.remove(nid)
            save_state(st)
            return self._json({"ok": True, "favorites": favs})
        if path == "/api/writeback":
            wb = st.setdefault("writeback", {})
            for k in ("create", "update", "delete"):
                v = body.get(k)
                if v in ("kb", "app"):
                    wb[k] = v
            save_state(st)
            journal({"op": "writeback-config", "config": wb})
            return self._json({"ok": True, "writeback": wb})
        return self._err(404, "not found")

    # -------- 图片
    def _serve_note_image(self, q):
        nid = (q.get("id") or [""])[0]
        name = safe_image_name((q.get("name") or [""])[0])
        try:
            w = int((q.get("w") or ["0"])[0])
        except ValueError:
            w = 0
        if not name:
            return self._err(400, "bad name")
        d, _src = find_note_dir(nid)
        if not d:
            return self._err(404, "no note")
        src = os.path.join(d, "image", name)
        if not os.path.isfile(src):
            return self._err(404, "no image")
        # 压缩缓存
        if w and PILLOW_OK:
            key = "%d_%s.webp" % (w, name)
            cpath = os.path.join(IMGCACHE_DIR, nid, key)
            if os.path.isfile(cpath):
                with open(cpath, "rb") as f:
                    return self._respond(ctype="image/webp", body=f.read(), cache="max-age=3600")
            try:
                im = Image.open(src)
                im = im.convert("RGB")
                if im.width > w:
                    h = max(1, int(im.height * w / im.width))
                    im = im.resize((w, h), Image.LANCZOS)
                os.makedirs(os.path.dirname(cpath), exist_ok=True)
                im.save(cpath, "WEBP", quality=76, method=4)
                with open(cpath, "rb") as f:
                    return self._respond(ctype="image/webp", body=f.read(), cache="max-age=3600")
            except Exception:
                pass  # 压缩失败则回退原图
        ext = os.path.splitext(name)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        with open(src, "rb") as f:
            return self._respond(ctype=ctype, body=f.read(), cache="max-age=3600")

    # -------- 静态文件
    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        if path == "/favicon.ico":
            path = "/icons/icon-192.png"
        rel = path.lstrip("/")
        if ".." in rel.split("/"):
            return self._err(400, "bad path")
        fp = os.path.join(WEB_DIR, rel.replace("/", os.sep))
        if not os.path.isfile(fp):
            # SPA 回退到 index
            fp = os.path.join(WEB_DIR, "index.html")
            if not os.path.isfile(fp):
                return self._err(404, "web 目录尚未部署页面文件")
        ext = os.path.splitext(fp)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        cache = "no-cache" if ext in (".html", ".js", ".webmanifest") else "max-age=86400"
        with open(fp, "rb") as f:
            body = f.read()
        headers = {}
        if rel == "sw.js":
            headers["Service-Worker-Allowed"] = "/"
        self._respond(ctype=ctype, body=body, cache=cache, headers=headers)


class OnboardHandler(Handler):
    """HTTP 端口：仅证书引导页与 CA 下载，其余跳转 HTTPS。"""

    def _route_get(self):
        path = self._path()
        if path == "/setup" or path == "/":
            return self._serve_setup()
        if path == "/ca.crt":
            p = os.path.join(CERT_DIR, "ca.crt")
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    return self._respond(ctype="application/x-x509-ca-cert", body=f.read(),
                                         cache="no-store",
                                         headers={"Content-Disposition": 'attachment; filename="tianxi-kb-ca.crt"'})
            return self._err(404, "证书尚未生成")
        if path == "/api/health":
            return self._json({"ok": True, "app": APP_NAME, "version": APP_VERSION,
                               "lan": self.server.tianxi_lan, "secure": bool(self.server.tianxi_https),
                               "serverTime": now_ms()})
        host = (self.headers.get("Host") or self.server.tianxi_lan).split(":")[0]
        self._respond(code=302, ctype="text/plain; charset=utf-8", body=b"",
                      headers={"Location": "https://%s:%d%s" % (host, PORT_HTTPS, path)})

    def _route_post(self):
        self._route_get()

    def _serve_setup(self):
        # 以手机实际访问用的地址为准（Host 头），回退到检测到的局域网地址
        ip = self.server.tianxi_lan
        host_hdr = (self.headers.get("Host") or "").split(":")[0].strip()
        if host_hdr and re.match(r"^[0-9a-zA-Z][0-9a-zA-Z.\-]*$", host_hdr):
            ip = host_hdr
        secure = bool(self.server.tianxi_https)
        app_url = "https://%s:%d/" % (ip, PORT_HTTPS)
        if secure:
            steps_html = (
                "<h2>第一次使用：安装证书（仅需一次）</h2>"
                "<ol>"
                "<li>点下面的按钮下载证书：<br><a class='btn' href='/ca.crt'>① 下载证书文件</a></li>"
                "<li>弹窗选择「允许」下载描述文件</li>"
                "<li>打开 iPhone「设置」，顶部会出现「已下载描述文件」→ 点进去 → 右上角「安装」（需要输密码）</li>"
                "<li>再打开「设置 → 通用 → 关于本机 → 证书信任设置」→ 找到 <b>Tianxi KB Local CA</b> → 打开信任开关</li>"
                "<li>完成后点下面按钮打开应用：</li>"
                "</ol>"
                "<a class='btn primary' href='%s'>② 打开知识库应用</a>" % app_url
            )
        else:
            steps_html = (
                "<h2>HTTPS 尚未就绪</h2>"
                "<p>电脑端服务未生成证书（可能缺少 openssl）。请先在电脑上查看启动窗口的提示。</p>"
            )
        html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>天禧随行知识库 · 连接引导</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         max-width: 560px; margin: 0 auto; padding: 28px 20px 60px; line-height: 1.75;
         background: #f6f5f4; color: rgba(0,0,0,.88); }
  @media (prefers-color-scheme: dark) { body { background: #202020; color: rgba(255,255,255,.9); } }
  h1 { font-size: 22px; letter-spacing: -.01em; }
  h2 { font-size: 17px; margin-top: 28px; }
  ol { padding-left: 20px; }
  li { margin: 8px 0; }
  .btn { display: inline-block; margin: 12px 0; padding: 13px 20px; border-radius: 10px;
         background: #ffffff; border: 1px solid rgba(0,0,0,.15); color: inherit;
         text-decoration: none; font-size: 16px; }
  .btn.primary { background: #0075de; border-color: #0075de; color: #fff; font-weight: 600; }
  .tip { font-size: 13.5px; opacity: .65; margin-top: 30px; }
  code { font-size: 14px; }
</style></head><body>
<h1>天禧随行知识库</h1>
<p>这个页面用于让 iPhone 与电脑上的知识库服务建立安全连接。</p>
%s
<p class="tip">说明：证书只在本机与电脑之间生效，用于让浏览器允许「离线打开」能力。<br>
电脑端地址：<code>%s:%d</code> · 应用地址：<code>%s</code></p>
</body></html>""" % (steps_html, ip, PORT_HTTPS, app_url)
        self._respond(ctype="text/html; charset=utf-8", body=html)


class ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    tianxi_lan = "127.0.0.1"
    tianxi_https = False

    def handle_error(self, request, client_address):
        pass  # 断开类错误静默


# ---------------------------------------------------------------- 运行日志
class _Tee:
    """把 stdout/stderr 同时写入日志文件（崩溃诊断用）。"""

    def __init__(self, stream, logfile):
        self._stream = stream
        self._file = logfile

    def write(self, s):
        try:
            self._stream.write(s)
        except Exception:
            pass
        try:
            self._file.write(s)
        except Exception:
            pass
        return len(s)

    def flush(self):
        for f in (self._stream, self._file):
            try:
                f.flush()
            except Exception:
                pass


_LOG_FILE = None


def _install_file_log():
    """本轮运行的全部输出（含报错回溯）追加到 data/server.log；超过 1MB 自动重写。"""
    global _LOG_FILE
    try:
        path = os.path.join(DATA_DIR, "server.log")
        mode = "a"
        if os.path.isfile(path) and os.path.getsize(path) > 1024 * 1024:
            mode = "w"
        _LOG_FILE = open(path, mode, encoding="utf-8", buffering=1)
        _LOG_FILE.write("\n===== 服务启动 %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        sys.stdout = _Tee(sys.stdout, _LOG_FILE)
        sys.stderr = _Tee(sys.stderr, _LOG_FILE)
    except Exception:
        _LOG_FILE = None


# ---------------------------------------------------------------- 启动
def main():
    _install_file_log()
    for d in (DATA_DIR, CERT_DIR, BACKUP_DIR, TRASH_DIR, OVERLAY_DIR, IMGCACHE_DIR, WEB_DIR):
        os.makedirs(d, exist_ok=True)
    if not os.path.isfile(STATE_PATH):
        write_json_atomic(STATE_PATH, default_state())

    lans = get_lan_ips()
    lan = lans[0] if lans else "127.0.0.1"
    https_ok, cert_msg = ensure_certs(lans)

    # 完整应用服务（优先 HTTPS）
    if https_ok:
        try:
            app_srv = ThreadingServer(("0.0.0.0", PORT_HTTPS), Handler)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(os.path.join(CERT_DIR, "server.crt"),
                                os.path.join(CERT_DIR, "server.key"))
            app_srv.socket = ctx.wrap_socket(app_srv.socket, server_side=True)
            app_srv.tianxi_https = True
        except Exception as e:
            print("！HTTPS 启动失败（%s），改用 HTTP %d 提供应用。" % (e, PORT_HTTPS))
            app_srv = ThreadingServer(("0.0.0.0", PORT_HTTPS), Handler)
            https_ok = False
    else:
        app_srv = ThreadingServer(("0.0.0.0", PORT_HTTPS), Handler)
    app_srv.tianxi_lan = lan

    onboard_srv = ThreadingServer(("0.0.0.0", PORT_HTTP), OnboardHandler)
    onboard_srv.tianxi_lan = lan
    onboard_srv.tianxi_https = https_ok

    threading.Thread(target=app_srv.serve_forever, daemon=True).start()
    threading.Thread(target=onboard_srv.serve_forever, daemon=True).start()

    items, counts = get_index()
    st = load_state()
    line = "=" * 62
    print(line)
    print("  %s · 本地服务已启动 (v%s)" % (APP_NAME, APP_VERSION))
    print(line)
    print("  手机访问地址（同一 Wi-Fi 下）")
    print("    首次连接（装证书）:  http://%s:%d/setup" % (lan, PORT_HTTP))
    if https_ok:
        print("    应用主页          :  https://%s:%d/" % (lan, PORT_HTTPS))
    else:
        print("    应用主页(无HTTPS) :  http://%s:%d/   —— %s" % (lan, PORT_HTTPS, cert_msg))
    for extra in lans[1:]:
        print("    备用地址          :  http://%s:%d/setup" % (extra, PORT_HTTP))
    print(line)
    print("  笔记数量：共 %d 篇（本机 %d · 应用层 %d · 仅云端 %d）"
          % (counts["total"], counts["local"], counts["app"], counts["cloud"]))
    wb = st.get("writeback") or {}
    wb_name = {"kb": "知识库", "app": "应用内"}
    print("  写回模式：新建=%s 修改=%s 删除=%s"
          % tuple(wb_name.get(wb.get(k), wb.get(k)) for k in ("create", "update", "delete")))
    v = st.get("validation") or {}
    val_name = {"pending": "进行中", "cloud-ok": "已通过（云端可见）",
                "no-cloud": "文件级通过；云端未通（见使用说明）", "kb-local": "文件级通过（云端未验证）"}
    print("  写入验证：%s" % val_name.get(v.get("result"), "未通过（见使用说明）"))
    print("  图片压缩：%s" % ("Pillow 已启用" if PILLOW_OK else "未安装 Pillow（将直接返回原图）"))
    print(line)
    print("  保持此窗口开启；关闭窗口即停止服务。按 Ctrl+C 退出。")
    print(line)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n服务已手动停止（Ctrl+C）。")
        sys.exit(42)  # 42 = 手动停止，启动脚本据此不再自动重启


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        try:
            print("\n服务已手动停止（Ctrl+C）。")
        except Exception:
            pass
        sys.exit(42)  # 42 = 手动停止
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
