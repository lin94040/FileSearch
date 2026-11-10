#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地文件搜索工具（浏览器版）
- 只在配置的目录里找文件
- Web 页面查询，显示文件名/大小/时间/路径
- 打开所在文件夹并选中该文件 / 使用默认程序打开文件
- 可切换是否允许局域网访问
零第三方依赖，标准库实现。
"""
import json
import os
import sys
import time
import threading
import subprocess
import webbrowser
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _pick_config_path():
    """选一个可写的位置放 config.json：优先脚本目录，沙箱/无权限时回退到 TEMP。"""
    candidates = [
        os.path.join(BASE_DIR, "config.json"),
        os.path.join(os.environ.get("TEMP", os.getcwd()), "file_search_config.json"),
    ]
    for p in candidates:
        try:
            d = os.path.dirname(p)
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".write_test")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            return p
        except Exception:
            continue
    return candidates[-1]


CONFIG_PATH = _pick_config_path()

_home = os.path.expanduser("~")
DEFAULT_CONFIG = {
    "port": 8765,
    "allow_lan": False,
    "dirs": [
        os.path.join(_home, "Desktop"),
        os.path.join(_home, "Documents"),
        os.path.join(_home, "Downloads"),
    ],
}

# 内存索引
_index_lock = threading.Lock()
_file_index = []  # list of dict: name, path, size, mtime, is_dir


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    cfg.setdefault(k, v)
                return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def rebuild_index(dirs):
    global _file_index
    new_index = []
    skip_dirs = {"$RECYCLE.BIN", "System Volume Information", "node_modules", ".git", "AppData"}
    for d in dirs:
        d = os.path.expandvars(os.path.expanduser(d))
        if not os.path.isdir(d):
            continue
        for root, subdirs, files in os.walk(d, topdown=True):
            subdirs[:] = [
                s for s in subdirs
                if not s.startswith(".") and s not in skip_dirs
            ]
            for name in files:
                full = os.path.join(root, name)
                try:
                    st = os.stat(full)
                    new_index.append({
                        "name": name,
                        "path": full,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "is_dir": False,
                    })
                except (OSError, PermissionError):
                    pass
    with _index_lock:
        _file_index = new_index
    print(f"[索引] 已收录 {len(new_index)} 个文件")


def human_size(n):
    s = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if s < 1024:
            return f"{s:.1f} {unit}"
        s /= 1024
    return f"{s:.1f} PB"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send_html(PAGE_HTML)
        elif u.path == "/api/config":
            cfg = load_config()
            self._send_json({
                "port": cfg["port"],
                "allow_lan": cfg["allow_lan"],
                "dirs": cfg["dirs"],
            })
        elif u.path == "/api/pickdir":
            # 后端弹出系统文件夹选择框：用 PowerShell + WinForms
            try:
                ps = (
                    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
                    "Add-Type -AssemblyName System.Windows.Forms;"
                    "$parent = New-Object System.Windows.Forms.Form;"
                    "$parent.TopMost = $true;"
                    "$parent.ShowInTaskbar = $false;"
                    "$parent.WindowState = 'Minimized';"
                    "$f = New-Object System.Windows.Forms.FolderBrowserDialog;"
                    "$f.Description = '选择要加入搜索的目录';"
                    "if ($f.ShowDialog($parent) -eq 'OK') { Write-Output $f.SelectedPath };"
                    "$parent.Dispose()"
                )
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-STA", "-Command", ps],
                    capture_output=True, timeout=300,
                )
                path = r.stdout.decode("utf-8", errors="ignore").strip()
                self._send_json({"ok": bool(path), "path": path})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        elif u.path == "/api/search":
            qs = parse_qs(u.query)
            q = qs.get("q", [""])[0].strip().lower()
            try:
                limit = int(qs.get("limit", ["300"])[0])
            except ValueError:
                limit = 300
            with _index_lock:
                idx = list(_file_index)
                if not q:
                    results = idx[:limit]
                else:
                    results = [f for f in idx if q in f["name"].lower()][:limit]
            for r in results:
                r["size_h"] = human_size(r["size"])
                r["mtime_h"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["mtime"]))
            self._send_json({"count": len(results), "results": results})
        else:
            self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            data = json.loads(body)
        except Exception:
            return self._send_json({"error": "bad json"}, 400)

        if u.path == "/api/config":
            cfg = load_config()
            if "dirs" in data:
                cfg["dirs"] = [str(d).strip() for d in data["dirs"] if str(d).strip()]
            if "allow_lan" in data:
                cfg["allow_lan"] = bool(data["allow_lan"])
            if "port" in data:
                try:
                    cfg["port"] = int(data["port"])
                except Exception:
                    pass
            save_config(cfg)
            threading.Thread(target=rebuild_index, args=(cfg["dirs"],), daemon=True).start()
            self._send_json({
                "ok": True,
                "note": "配置已保存，正在重建索引；若改了端口或局域网开关需重启本程序生效",
            })
        elif u.path == "/api/locate":
            path = data.get("path", "")
            cfg = load_config()
            real_path = os.path.normpath(path)
            allowed = False
            for d in cfg["dirs"]:
                d = os.path.normpath(os.path.expandvars(os.path.expanduser(d)))
                try:
                    if os.path.commonpath([d, real_path]).lower() == d.lower():
                        allowed = True
                        break
                except ValueError:
                    continue
            if not allowed:
                return self._send_json({"ok": False, "error": "路径不在允许范围"}, 400)
            if not os.path.exists(real_path):
                return self._send_json({"ok": False, "error": "文件不存在"}, 400)
            try:
                subprocess.Popen(f'explorer.exe /select,"{real_path}"', shell=True)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        elif u.path == "/api/open":
            path = data.get("path", "")
            cfg = load_config()
            real_path = os.path.normpath(path)
            allowed = False
            for d in cfg["dirs"]:
                d = os.path.normpath(os.path.expandvars(os.path.expanduser(d)))
                try:
                    if os.path.commonpath([d, real_path]).lower() == d.lower():
                        allowed = True
                        break
                except ValueError:
                    continue
            if not allowed:
                return self._send_json({"ok": False, "error": "路径不在允许范围"}, 400)
            if not os.path.exists(real_path):
                return self._send_json({"ok": False, "error": "文件不存在"}, 400)
            try:
                # Windows 默认程序打开，等同于双击
                subprocess.Popen(["start", "", real_path], shell=True)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        else:
            self.send_error(404)


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>本地文件搜索</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: "Microsoft YaHei", sans-serif; background: #f5f6f8; color: #333; padding: 20px; }
.container { max-width: 1200px; margin: 0 auto; }
h1 { font-size: 22px; margin-bottom: 16px; }
.search-bar { display: flex; gap: 8px; margin-bottom: 16px; }
.search-bar input { flex: 1; padding: 10px 14px; font-size: 15px; border: 1px solid #d0d3d9; border-radius: 6px; }
.search-bar button { padding: 10px 20px; background: #2563eb; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; }
.search-bar button.sec { background: #6b7280; }
table { width: 100%; background: #fff; border-radius: 6px; overflow: hidden; border-collapse: collapse; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #eef0f3; font-size: 13px; vertical-align: top; }
th { background: #fafbfc; font-weight: 600; color: #666; }
tr:hover { background: #f8fafc; }
td.path { color: #888; font-family: Consolas, monospace; font-size: 12px; word-break: break-all; }
.locate-btn { padding: 4px 10px; background: #16a34a; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; white-space: nowrap; margin-right: 4px; }
.open-btn { padding: 4px 10px; background: #2563eb; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; white-space: nowrap; }
.modal-bg { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.4); z-index: 100; justify-content: center; align-items: center; }
.modal-bg.show { display: flex; }
.modal { background: #fff; border-radius: 8px; padding: 24px; width: 560px; max-width: 92vw; max-height: 85vh; overflow: auto; }
.modal h2 { font-size: 18px; margin-bottom: 16px; }
.field { margin-bottom: 14px; }
.field > label { display: block; font-size: 13px; color: #555; margin-bottom: 6px; }
.dir-row { display: flex; gap: 6px; margin-bottom: 6px; }
.dir-row input { flex: 1; padding: 6px 10px; border: 1px solid #d0d3d9; border-radius: 4px; font-size: 13px; font-family: Consolas, monospace; }
.dir-row button { padding: 6px 10px; background: #ef4444; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; }
.add-dir { padding: 6px 12px; background: #2563eb; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; }
.switch-row { display: flex; align-items: center; gap: 8px; margin: 14px 0; }
.switch-row input[type=checkbox] { width: 18px; height: 18px; }
.switch-row label { margin: 0 !important; }
.port-input { padding: 6px 10px; border: 1px solid #d0d3d9; border-radius: 4px; width: 120px; }
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 20px; }
.modal-actions button { padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }
.save { background: #2563eb; color: #fff; }
.cancel { background: #e5e7eb; color: #333; }
.hint { font-size: 12px; color: #888; margin-top: 8px; line-height: 1.6; }
.count { font-size: 13px; color: #666; margin: 8px 0; }
.empty { color: #aaa; text-align: center; padding: 30px; }
</style>
</head>
<body>
<div class="container">
  <h1>本地文件搜索</h1>
  <div class="search-bar">
    <input id="q" placeholder="输入文件名关键词，回车搜索…" autofocus>
    <button onclick="doSearch()">搜索</button>
    <button class="sec" onclick="openSettings()">设置</button>
  </div>
  <div class="count" id="count"></div>
  <table>
    <thead><tr><th style="width:25%">文件名</th><th style="width:8%">大小</th><th style="width:14%">修改时间</th><th>所在位置</th><th style="width:16%">操作</th></tr></thead>
    <tbody id="tbody"><tr><td colspan="5" class="empty">输入关键词开始搜索</td></tr></tbody>
  </table>
</div>

<div class="modal-bg" id="modalBg">
  <div class="modal">
    <h2>搜索设置</h2>
    <div class="field">
      <label>搜索目录（只在这些目录里找，可多个；路径一行一个）</label>
      <div id="dirList"></div>
      <div style="margin-top:6px;"><button class="add-dir" onclick="addDirRow()">+ 添加一行目录</button></div>
    </div>
    <div class="switch-row">
      <input type="checkbox" id="allowLan">
      <label for="allowLan">允许局域网其他电脑访问本机这个搜索页面</label>
    </div>
    <div class="field">
      <label>端口号</label>
      <input id="port" class="port-input" value="8765">
    </div>
    <div class="hint">提示：修改"端口"或"局域网开关"后，需要关闭本程序（黑窗口）重新运行才生效。目录列表保存后会自动重建索引，无需重启。</div>
    <div class="modal-actions">
      <button class="cancel" onclick="closeSettings()">取消</button>
      <button class="save" onclick="saveSettings()">保存</button>
    </div>
  </div>
</div>

<script>
let dirs = [];
async function loadConfig(){
  const r = await fetch('/api/config');
  const c = await r.json();
  dirs = (c.dirs || []).slice();
  document.getElementById('allowLan').checked = !!c.allow_lan;
  document.getElementById('port').value = c.port || 8765;
  renderDirs();
}
function renderDirs(){
  const box = document.getElementById('dirList');
  box.innerHTML = '';
  dirs.forEach((d,i)=>{
    const row = document.createElement('div');
    row.className = 'dir-row';
    const inp = document.createElement('input');
    inp.value = d;
    inp.oninput = () => dirs[i] = inp.value;
    const btnPick = document.createElement('button');
    btnPick.textContent = '浏览…';
    btnPick.style.background = '#0ea5e9';
    btnPick.onclick = async () => {
      btnPick.disabled = true; btnPick.textContent = '选择中…';
      try {
        const r = await fetch('/api/pickdir');
        const j = await r.json();
        if (j.ok && j.path) { inp.value = j.path; dirs[i] = j.path; }
      } finally { btnPick.disabled = false; btnPick.textContent = '浏览…'; }
    };
    const btnDel = document.createElement('button');
    btnDel.textContent = '删除';
    btnDel.onclick = () => { dirs.splice(i,1); renderDirs(); };
    row.appendChild(inp);
    row.appendChild(btnPick);
    row.appendChild(btnDel);
    box.appendChild(row);
  });
}
function addDirRow(){ dirs.push(''); renderDirs(); }
function openSettings(){ loadConfig().then(()=>document.getElementById('modalBg').classList.add('show')); }
function closeSettings(){ document.getElementById('modalBg').classList.remove('show'); }
async function saveSettings(){
  const body = {
    dirs: dirs.map(s=>s.trim()).filter(Boolean),
    allow_lan: document.getElementById('allowLan').checked,
    port: parseInt(document.getElementById('port').value) || 8765
  };
  await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  closeSettings();
  alert('设置已保存，正在重建索引，稍等几秒后搜索即为最新结果。');
  doSearch();
}
async function doSearch(){
  const q = document.getElementById('q').value.trim();
  const r = await fetch('/api/search?q=' + encodeURIComponent(q));
  const d = await r.json();
  document.getElementById('count').textContent = '共 ' + d.count + ' 个结果（最多显示 300 条）';
  const tb = document.getElementById('tbody');
  if (!d.results.length) {
    tb.innerHTML = '<tr><td colspan="5" class="empty">没有匹配的文件</td></tr>';
    return;
  }
  tb.innerHTML = '';
  d.results.forEach(f=>{
    const tr = document.createElement('tr');
    const tdName = document.createElement('td'); tdName.textContent = f.name;
    const tdSize = document.createElement('td'); tdSize.textContent = f.size_h;
    const tdTime = document.createElement('td'); tdTime.textContent = f.mtime_h;
    const tdPath = document.createElement('td'); tdPath.className = 'path'; tdPath.textContent = f.path;
    const tdAct = document.createElement('td');

    const btnLocate = document.createElement('button');
    btnLocate.className = 'locate-btn';
    btnLocate.textContent = '打开所在文件夹';
    btnLocate.onclick = () => locate(f.path);

    const btnOpen = document.createElement('button');
    btnOpen.className = 'open-btn';
    btnOpen.textContent = '打开文件';
    btnOpen.onclick = () => openFile(f.path);

    tdAct.appendChild(btnLocate);
    tdAct.appendChild(btnOpen);

    tr.appendChild(tdName);
    tr.appendChild(tdSize);
    tr.appendChild(tdTime);
    tr.appendChild(tdPath);
    tr.appendChild(tdAct);
    tb.appendChild(tr);
  });
}
async function locate(p){
  await fetch('/api/locate', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({path:p})
  });
}
async function openFile(p){
  await fetch('/api/open', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({path:p})
  });
}
document.getElementById('q').addEventListener('keydown', e=>{ if(e.key==='Enter') doSearch(); });
loadConfig().then(doSearch);
</script>
</body>
</html>
"""


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def main():
    cfg = load_config()
    print("=" * 50)
    print("  本地文件搜索工具")
    print("=" * 50)
    print("正在建立索引（首次会扫描配置目录，稍候）…")
    rebuild_index(cfg["dirs"])
    host = "0.0.0.0" if cfg["allow_lan"] else "127.0.0.1"
    port = cfg["port"]
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"[错误] 端口 {port} 启动失败：{e}")
        print("请换一个端口（在设置里改，或删掉 config.json 重新运行）。")
        input("按回车退出…")
        return
    print(f"\n本机访问: http://127.0.0.1:{port}/")
    if cfg["allow_lan"]:
        ip = get_lan_ip()
        if ip:
            print(f"局域网访问: http://{ip}:{port}/  （同WiFi/同网段的其他设备可打开）")
    print("\n按 Ctrl+C 退出。启动后会自动打开浏览器。")
    threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
