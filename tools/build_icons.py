"""產生自帶的 Font Awesome 圖示子集（取代 cdnjs 的整套 all.min.css + 三個字型檔）。

只在新增 / 換圖示時才需要跑（一般部署、tools/check.py 都不用）：
    python tools/build_icons.py                # 從 cdnjs 下載 Font Awesome 6.5.0 原檔
    python tools/build_icons.py --src 資料夾    # 或用已下載的 all.min.css 與 webfonts/*.woff2

需要 fontTools + brotli（pip install fonttools brotli）；不需要 Node.js。
做法：掃 templates/*.html、static/js/app/*.js、static/css/style.css 用到的 fa-* 類別 →
- static/css/icons.css：只留用到的圖示規則 + 共用的 .fa/.fas/.fab/動畫規則（原樣取自 all.min.css）
- static/fonts/fa-solid-900.woff2、fa-brands-400.woff2：只含用到的字形
字型網址帶內容雜湊（?v=…），換了圖示瀏覽器就會拿新檔；tests/test_icons.py 會檢查每個用到的圖示都有規則。
"""
import argparse
import hashlib
import io
import os
import re
import sys
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CDN = "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/"
SCAN = ["templates/index.html", "templates/members.html", "templates/login.html", "static/css/style.css",
        *sorted("static/js/app/" + n for n in os.listdir(os.path.join(ROOT, "static", "js", "app")) if n.endswith(".js"))]
# 不是圖示名稱的通用類別（樣式 / 大小 / 動畫），規則照樣保留
GENERIC = {"fa", "fas", "fab", "far", "fa-solid", "fa-brands", "fa-regular", "fa-classic", "fa-sharp"}
TOKEN = re.compile(r"\bfa-[a-z0-9-]+")
FONTS = {"solid": "fa-solid-900", "brands": "fa-brands-400"}


def used_names():
    names = set()
    for rel in SCAN:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            names |= set(TOKEN.findall(f.read()))
    return names


def read_src(src, name, binary=False):
    if src:
        sub = "css" if name.endswith(".css") else "webfonts"
        for cand in (os.path.join(src, name), os.path.join(src, sub, name)):
            if os.path.isfile(cand):
                with open(cand, "rb") as f:
                    data = f.read()
                return data if binary else data.decode("utf-8")
        raise SystemExit(f"找不到 {name}（在 {src}）")
    url = CDN + ("css/" if name.endswith(".css") else "webfonts/") + name
    with urllib.request.urlopen(url, timeout=30) as r:
        data = r.read()
    return data if binary else data.decode("utf-8")


def blocks(css):
    """頂層 selector{...} / @規則{...}（含巢狀大括號）→ [(前導, 內容)]"""
    out, i, n = [], 0, len(css)
    while i < n:
        j = css.find("{", i)
        if j < 0:
            break
        depth, k = 1, j + 1
        while k < n and depth:
            depth += {"{": 1, "}": -1}.get(css[k], 0)
            k += 1
        out.append((css[i:j].strip(), css[j + 1:k - 1]))
        i = k
    return out


def keep_selector(sel, used):
    toks = set(re.findall(r"\.(fa-[a-z0-9-]+|fa[bsr]?)\b", sel))
    return all(t in used or t in GENERIC for t in toks)


def build(src=None):
    from fontTools import subset
    from fontTools.ttLib import TTFont
    used = used_names() | {"fa-spin", "fa-pulse", "fa-spin-pulse"}
    css = read_src(src, "all.min.css")
    head = css[:css.find("*/") + 2] if css.startswith("/*") else ""
    rules, cps, keyframes = [], {"solid": set(), "brands": set()}, set()
    for pre, body in blocks(css[len(head):]):
        if pre.startswith("@font-face"):
            continue
        if pre.startswith("@keyframes") or pre.startswith("@-webkit-keyframes"):
            rules.append((pre, body, "kf"))
            continue
        if pre.startswith("@media"):
            inner = [f"{','.join(s for s in p.split(',') if keep_selector(s, used))}{{{b}}}"
                     for p, b in blocks(body) if any(keep_selector(s, used) for s in p.split(","))]
            if inner:
                rules.append((pre, "".join(inner), "media"))
            continue
        sels = [s for s in pre.split(",") if s.startswith(":") or keep_selector(s, used)]
        if not sels:
            continue
        rules.append((",".join(sels), body, "rule"))
        for kf in re.findall(r"animation-name:([a-z-]+)", body):
            keyframes.add(kf)
    # 圖示 → 字碼
    icon_cp = {}
    for pre, body, kind in rules:
        m = re.search(r'content:"\\([0-9a-f]+)"', body)
        if kind == "rule" and m:
            for s in pre.split(","):
                nm = re.match(r"\.(fa-[a-z0-9-]+):before$", s)
                if nm:
                    icon_cp[nm.group(1)] = int(m.group(1), 16)
    brands_font = TTFont(io.BytesIO(read_src(src, FONTS["brands"] + ".woff2", binary=True)))
    brand_cmap = set(brands_font.getBestCmap())
    solid_font = TTFont(io.BytesIO(read_src(src, FONTS["solid"] + ".woff2", binary=True)))
    solid_cmap = set(solid_font.getBestCmap())
    missing = []
    for name in sorted(used - GENERIC):
        if name not in icon_cp:
            continue
        cp = icon_cp[name]
        if cp in brand_cmap and cp not in solid_cmap:
            cps["brands"].add(cp)
        elif cp in solid_cmap:
            cps["solid"].add(cp)
        else:
            missing.append(name)
    if missing:
        raise SystemExit(f"字型裡找不到這些圖示：{missing}")
    os.makedirs(os.path.join(ROOT, "static", "fonts"), exist_ok=True)
    faces = []
    for kind, family, weight, font in (("solid", "Font Awesome 6 Free", 900, solid_font),
                                       ("brands", "Font Awesome 6 Brands", 400, brands_font)):
        opts = subset.Options()
        opts.flavor = "woff2"
        opts.layout_features = ["*"]
        opts.name_IDs = ["*"]
        opts.notdef_outline = True
        sub = subset.Subsetter(opts)
        sub.populate(unicodes=sorted(cps[kind]))
        sub.subset(font)
        buf = io.BytesIO()
        font.flavor = "woff2"
        font.save(buf)
        data = buf.getvalue()
        name = FONTS[kind] + ".woff2"
        with open(os.path.join(ROOT, "static", "fonts", name), "wb") as f:
            f.write(data)
        ver = hashlib.sha256(data).hexdigest()[:10]
        faces.append(f'@font-face{{font-family:"{family}";font-style:normal;font-weight:{weight};font-display:block;'
                     f'src:url(/static/fonts/{name}?v={ver}) format("woff2")}}')
        print(f"{name}: {len(cps[kind])} 個字形，{len(data)} bytes")
    out = [head, "/* 自帶子集：tools/build_icons.py 產生，請勿手改。只含本專案用到的圖示。 */"]
    for pre, body, kind in rules:
        if kind == "kf":
            if pre.split()[-1] not in keyframes:
                continue
        out.append(f"{pre}{{{body}}}")
    out.extend(faces)
    text = "\n".join(out) + "\n"
    with open(os.path.join(ROOT, "static", "css", "icons.css"), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"icons.css：{len(text)} bytes，圖示 {len(icon_cp)} 個名稱（含別名）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", help="已下載的 Font Awesome 6.5.0（all.min.css 與 *.woff2 所在資料夾）")
    a = ap.parse_args()
    build(a.src)
