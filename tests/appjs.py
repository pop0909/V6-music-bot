"""前端程式拆成 static/js/app/*.js（index.html 依序用一般 <script> 載入，共用同一個全域範圍）。
測試要看「整份前端程式」時用 read_app_js()：照 index.html 的載入順序接起來，等於拆分前的 app.js。"""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP_DIR = os.path.join(ROOT, "static", "js", "app")
SCRIPT_RE = re.compile(r'<script src="/static/js/app/([\w.-]+\.js)\?v=([\w.-]+)"(?:\s+defer)?>\s*</script>')


def app_js_files():
    """index.html 載入的前端檔名（依載入順序）"""
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        return [m.group(1) for m in SCRIPT_RE.finditer(f.read())]


def app_js_paths(rel=False):
    return [("static/js/app/" + n) if rel else os.path.join(APP_DIR, n) for n in app_js_files()]


def read_app_js():
    out = []
    for p in app_js_paths():
        with open(p, encoding="utf-8") as f:
            out.append(f.read())
    return "".join(out)
