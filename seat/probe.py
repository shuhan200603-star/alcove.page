#!/usr/bin/env python3
"""探路：看看这台机器能不能够到学校的约座系统，以及它的接口长什么样。

在 VPS 上跑：
    python3 probe.py

它只读不写，不会预约任何东西。结果打印在屏幕上，同时存一份到 probe-report.txt。
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
except ImportError:
    sys.exit("先装依赖：pip3 install requests")

BASE = "http://order.lib.zzu.edu.cn"
H5 = f"{BASE}/h5/index.html"
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

HERE = Path(__file__).resolve().parent
REPORT = HERE / "probe-report.txt"

# 接口路径长这样：带 api / login / auth / seat / space / reserve 字样的绝对路径
PATH_RE = re.compile(
    r"""["'`](/[\w\-./]{2,80}?(?:api|login|auth|token|seat|space|reserv|book|order|user|dev)[\w\-./]{0,60})["'`]""",
    re.I,
)
BASEURL_RE = re.compile(r"""baseURL\s*[:=]\s*["'`]([^"'`]{1,120})["'`]""", re.I)
SCRIPT_RE = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)

out_lines: list[str] = []


def say(msg: str = "") -> None:
    print(msg)
    out_lines.append(msg)


def fetch(session: requests.Session, url: str, label: str):
    t0 = time.time()
    try:
        r = session.get(url, timeout=15, allow_redirects=True)
    except Exception as e:
        say(f"  {label}: 连不上 — {type(e).__name__}: {e}")
        return None
    dt = time.time() - t0
    say(f"  {label}: HTTP {r.status_code}  {dt:.2f}s  {len(r.content)} 字节")
    if r.url != url:
        say(f"      跳到了 {r.url}")
    return r


def main() -> int:
    session = requests.Session()
    session.headers["User-Agent"] = UA

    say("=" * 56)
    say("一、这台机器够不够得到")
    say("=" * 56)

    root = fetch(session, BASE, "站点根")
    page = fetch(session, H5, "H5 页面")

    if page is None:
        say()
        say("→ 页面都打不开，多半真的要校园网。到此为止。")
        REPORT.write_text("\n".join(out_lines), encoding="utf-8")
        return 1

    html = page.text
    if any(k in page.url.lower() for k in ("cas", "authserver", "sso")):
        say()
        say("→ 注意：被弹到统一认证了，登录要走 CAS，比直连麻烦一些。")

    say()
    say("=" * 56)
    say("二、它的接口长什么样")
    say("=" * 56)

    srcs = SCRIPT_RE.findall(html)
    say(f"  页面引了 {len(srcs)} 个脚本")

    blobs = [html]
    for src in srcs[:12]:
        url = urljoin(page.url, src)
        try:
            r = session.get(url, timeout=20)
            blobs.append(r.text)
            say(f"    抓到 {src.split('/')[-1]}  {len(r.content)} 字节")
        except Exception as e:
            say(f"    抓不到 {src}  ({type(e).__name__})")

    paths, bases = set(), set()
    for b in blobs:
        paths.update(m.group(1) for m in PATH_RE.finditer(b))
        bases.update(BASEURL_RE.findall(b))

    if bases:
        say()
        say("  接口前缀 baseURL：")
        for b in sorted(bases):
            say(f"    {b}")

    say()
    say(f"  翻出 {len(paths)} 条接口路径，挑相关的：")

    def score(p: str) -> tuple:
        kw = ("login", "auth", "token", "reserv", "book", "seat", "space", "dev", "user")
        return (-sum(k in p.lower() for k in kw), len(p))

    for p in sorted(paths, key=score)[:40]:
        say(f"    {p}")

    say()
    say("=" * 56)
    say(f"完整结果存在 {REPORT.name}")
    say("=" * 56)

    REPORT.write_text("\n".join(out_lines), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
