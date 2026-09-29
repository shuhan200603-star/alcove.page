#!/usr/bin/env python3
"""探路：看看这台机器能不能够到学校的约座系统，以及它的接口长什么样。

在 VPS 上跑：
    python3 probe.py

只发 GET，不登录、不提交、不预约任何东西，也不碰你的账号。
结果打印在屏幕上，同时存一份完整的到 probe-report.txt。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
except ImportError:
    sys.exit("先装依赖：sudo apt install -y python3-requests")

BASE = "http://order.lib.zzu.edu.cn"
H5 = f"{BASE}/h5/index.html"
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

HERE = Path(__file__).resolve().parent
REPORT = HERE / "probe-report.txt"

PATH_RE = re.compile(
    r"""["'`](/[\w\-./]{2,80}?"""
    r"""(?:api|login|auth|token|seat|space|reserv|book|order|user|dev|room|area|floor)"""
    r"""[\w\-./]{0,60})["'`]""",
    re.I,
)
BASEURL_RE = re.compile(r"""baseURL\s*[:=]\s*["'`]([^"'`]{0,120})["'`]""", re.I)
SCRIPT_RE = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)
# 压缩代码里登录/提交那几行的样子：post("/x/y",{a:b,c:d})
CALL_RE = re.compile(
    r"""(?:get|post|put|request)\s*\(\s*["'`]([^"'`]{3,90})["'`]\s*(,[^)]{0,240})?""",
    re.I,
)

KEYWORDS = ("login", "auth", "token", "reserv", "book", "submit", "seat", "space", "dev", "room")

lines: list[str] = []


def say(msg: str = "") -> None:
    print(msg)
    lines.append(msg)


def hr(title: str) -> None:
    say()
    say("=" * 58)
    say(title)
    say("=" * 58)


def relevance(text: str) -> int:
    return sum(k in text.lower() for k in KEYWORDS)


def fetch(session: requests.Session, url: str, label: str):
    t0 = time.time()
    try:
        r = session.get(url, timeout=15)
    except Exception as e:
        say(f"  {label}: 连不上 — {type(e).__name__}: {e}")
        return None
    say(f"  {label}: HTTP {r.status_code}  {time.time() - t0:.2f}s  {len(r.content)} 字节")
    if r.url.rstrip("/") != url.rstrip("/"):
        say(f"      跳到了 {r.url}")
    return r


def main() -> int:
    session = requests.Session()
    session.headers["User-Agent"] = UA

    hr("一、这台机器够不够得到")
    fetch(session, BASE, "站点根")
    page = fetch(session, H5, "H5 页面")

    if page is None or page.status_code >= 400:
        say()
        say("→ 直连不行，多半真的要校园网。顺手看看有没有别的门：")
        for url, label in (
            ("http://webvpn.zzu.edu.cn", "WebVPN"),
            ("https://webvpn.zzu.edu.cn", "WebVPN(https)"),
            ("http://cas.zzu.edu.cn", "统一认证"),
            ("http://lib.zzu.edu.cn", "图书馆主站"),
        ):
            fetch(session, url, label)
        say()
        say("→ 到此为止，把这一整段发我，我看着挑退路。")
        REPORT.write_text("\n".join(lines), encoding="utf-8")
        return 1

    if any(k in page.url.lower() for k in ("cas", "authserver", "sso")):
        say()
        say("→ 注意：被弹到统一认证了，登录得走 CAS。")

    hr("二、把前端代码抓下来")
    srcs = SCRIPT_RE.findall(page.text)
    say(f"  页面引了 {len(srcs)} 个脚本")

    blobs = [("index.html", page.text)]
    for src in srcs[:12]:
        url = urljoin(page.url, src)
        try:
            r = session.get(url, timeout=25)
            blobs.append((src.split("/")[-1], r.text))
            say(f"    {src.split('/')[-1]}  {len(r.content)} 字节")
        except Exception as e:
            say(f"    抓不到 {src}  ({type(e).__name__})")

    hr("三、接口前缀")
    bases = {b for _, t in blobs for b in BASEURL_RE.findall(t)}
    if bases:
        for b in sorted(bases):
            say(f"  baseURL = {b!r}")
    else:
        say("  没找到显式的 baseURL，接口八成是写全路径的")

    hr("四、调用现场（这段最要紧）")
    say("  下面是代码里实际发请求的地方，连参数一起。")
    say("  字段名就在里面，不用猜，也不用拿账号去试。")
    say()

    seen: set[str] = set()
    calls = []
    for name, text in blobs:
        for m in CALL_RE.finditer(text):
            path, args = m.group(1), (m.group(2) or "").strip()
            if not path.startswith(("/", "http")) or path in seen:
                continue
            score = relevance(path) * 3 + relevance(args)
            if score:
                seen.add(path)
                calls.append((score, path, args[:240], name))

    calls.sort(key=lambda c: -c[0])
    if not calls:
        say("  一个都没匹配上，代码大概被混淆了。看第五节的裸路径。")
    for _, path, args, name in calls[:18]:
        say(f"  ── {path}")
        if args:
            say(f"     参数 {args}")
        say(f"     出自 {name}")
        say()

    hr("五、所有翻到的路径")
    paths = {m.group(1) for _, t in blobs for m in PATH_RE.finditer(t)}
    for p in sorted(paths, key=lambda p: (-relevance(p), len(p)))[:45]:
        say(f"  {p}")
    say(f"  （共 {len(paths)} 条，完整清单在 {REPORT.name}）")

    hr("六、不用登录就能看的东西")
    say("  拿几个像「列表」的接口试试，看看返回结构、顺便找三楼中的 id。")
    say()
    guesses = [p for p in paths if relevance(p) >= 1 and not re.search(r"login|auth|token", p, re.I)]
    guesses.sort(key=lambda p: (-relevance(p), len(p)))
    for p in guesses[:8]:
        url = urljoin(BASE, p)
        try:
            r = session.get(url, timeout=10)
            body = r.text[:300].replace("\n", " ")
            say(f"  {p} → HTTP {r.status_code}  {body}")
        except Exception as e:
            say(f"  {p} → {type(e).__name__}")
        say()

    REPORT.write_text("\n".join(lines) + "\n\n\n完整路径清单：\n" + "\n".join(sorted(paths)), encoding="utf-8")
    say("=" * 58)
    say(f"完整结果存在 {REPORT.name}，截图发我就行")
    say("=" * 58)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
