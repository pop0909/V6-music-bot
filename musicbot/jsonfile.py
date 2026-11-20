"""JSON 檔原子寫入（全專案共用）：先寫同資料夾的 .tmp 再 os.replace，寫到一半當機也不會把原檔清空。"""
import json
import os


def write_atomic(path, obj, private=False, indent=None):
    """寫入 path。private=True → 檔案權限 0600（含金鑰雜湊 / 登入紀錄等）。indent=None → 最精簡格式。
    失敗會丟例外（呼叫端決定要不要吞掉）。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        if indent is None:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)
