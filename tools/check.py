"""一鍵完整檢查：單元測試 → 啟動模擬器（tools/sim.py，資料放暫存資料夾）→ API 煙霧測試 → 面板 UI 煙霧測試。

用法：
    python tools/check.py            全部跑（UI 測試約 4 分鐘：深色/淺色 × 1440/390 寬）
    python tools/check.py --no-ui    跳過 UI 測試
    python tools/check.py --ui-only  只跑模擬器 + UI 測試
沒裝 Playwright 時 UI 測試會自動略過（不算失敗）；要裝：pip install playwright && python -m playwright install chromium
UI 截圖存在 smoke_out/（已在 .gitignore）。結束碼：有任何一步失敗 → 1。
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PY = sys.executable


def playwright_available():
    try:
        import playwright.async_api  # noqa: F401
    except Exception:
        return False
    return True


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_sim():
    """背景啟動模擬器 → (process, base_url)；資料放在新的暫存資料夾"""
    port = free_port()
    env = dict(os.environ, SIM_PORT=str(port), SIM_SONG_LEN=os.getenv("SIM_SONG_LEN", "20"), SIM_DATA=tempfile.mkdtemp(prefix="v6sim-"), PYTHONIOENCODING="utf-8")
    log = open(os.path.join(env["SIM_DATA"], "sim.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([PY, os.path.join(ROOT, "tools", "sim.py")], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if proc.poll() is not None:
            raise RuntimeError(f"模擬器啟動失敗（看 {log.name}）")
        try:
            urllib.request.urlopen(urllib.request.Request(base + "/login", headers={"Host": "localhost"}), timeout=2)
            return proc, base
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("模擬器 60 秒內沒有回應")


def stop_sim(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def run(title, args, env=None):
    print(f"\n===== {title} =====", flush=True)
    r = subprocess.run([PY] + args, cwd=ROOT, env=dict(os.environ, PYTHONIOENCODING="utf-8", **(env or {})))
    print(f"----- {title}：{'通過' if r.returncode == 0 else '失敗'}", flush=True)
    return r.returncode == 0


def main(argv):
    ui = "--no-ui" not in argv
    results = {}
    if "--ui-only" not in argv:
        results["單元測試"] = run("單元測試", ["-m", "unittest", "discover", "-s", "tests"])
    proc, base = start_sim()
    try:
        if "--ui-only" not in argv:
            results["API 煙霧測試"] = run("API 煙霧測試", [os.path.join("tools", "smoke_api.py")], {"SMOKE_BASE": base})
        if ui and playwright_available():
            results["UI 煙霧測試"] = run("UI 煙霧測試", [os.path.join("tools", "smoke_ui.py"), "smoke_out"], {"SMOKE_BASE": base})
        elif ui:
            print("\n===== UI 煙霧測試：略過（沒裝 Playwright：pip install playwright && python -m playwright install chromium）=====")
    finally:
        stop_sim(proc)
    print("\n" + "\n".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
