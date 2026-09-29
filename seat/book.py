#!/usr/bin/env python3
"""图书馆自动约座。

每天开放时刻（默认北京时间 7:30:00）抢次日的位子。
提前登录好，卡着整点提交，失败少量重试，结果推到手机。

跑法：
    python3 book.py            # 等到下一个开放时刻再抢
    python3 book.py --now      # 立刻跑一遍，用来试接口
    python3 book.py --dry-run  # 只登录不提交
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import tomllib
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BJ = ZoneInfo("Asia/Shanghai")
HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "log"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def setup_log() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    log = logging.getLogger("seat")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
    for h in (
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / f"{datetime.now(BJ):%Y-%m-%d}.log", encoding="utf-8"),
    ):
        h.setFormatter(fmt)
        log.addHandler(h)
    return log


LOG = setup_log()


# ---------------------------------------------------------------- 时间


def clock_offset(session: requests.Session, base: str) -> float:
    """本机时钟比服务器快多少秒。VPS 的钟未必准，整点抢座差半秒就没了。"""
    try:
        r = session.head(base, timeout=5)
        server = parsedate_to_datetime(r.headers["Date"])
        off = time.time() - server.timestamp()
        LOG.info("时钟偏差 %+.2f 秒（本机比服务器快为正）", off)
        return off
    except Exception as e:
        LOG.warning("拿不到服务器时间，按本机走：%s", e)
        return 0.0


def next_open(open_at: str) -> datetime:
    """下一个开放时刻。已经过了就是明天这个点。"""
    h, m, s = (int(x) for x in open_at.split(":"))
    now = datetime.now(BJ)
    t = now.replace(hour=h, minute=m, second=s, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def sleep_until(target: datetime, offset: float) -> None:
    """睡到 target（服务器时间）。最后两秒改成小步轮询，避免 sleep 睡过头。"""
    deadline = target.timestamp() + offset
    while True:
        left = deadline - time.time()
        if left <= 0:
            return
        time.sleep(min(left - 0.5, 30) if left > 2 else 0.002)


# ---------------------------------------------------------------- 接口
# 下面两个函数是整份脚本唯一跟学校系统耦合的地方。
# 抓包拿到登录和提交预约那两条请求后，照着填进去，别的都不用动。


def login(session: requests.Session, cfg: dict) -> None:
    """登录，把会话 cookie 留在 session 里。

    TODO 等抓包：郑大这套系统的登录一般是 POST 到某个 /auth 或 /login，
    表单字段名、密码是否要加密（有的站会先 RSA/MD5 一道）都得看真实请求。
    """
    raise NotImplementedError("把浏览器里抓到的登录请求填进来")


def submit(session: requests.Session, cfg: dict, seat: dict) -> tuple[bool, str]:
    """提交一个座位的预约。返回 (成功与否, 说明)。

    TODO 等抓包：提交通常是 POST 一段 JSON 或表单，含座位 id、日期、起止时间。
    日期一般是次日，即 date.today() + 1 天。
    """
    raise NotImplementedError("把浏览器里抓到的预约请求填进来")


# ---------------------------------------------------------------- 通知


def notify(cfg: dict, title: str, body: str) -> None:
    n = cfg.get("notify", {})
    try:
        if url := n.get("bark_url"):
            requests.get(f"{url.rstrip('/')}/{title}/{body}", timeout=8)
        if url := n.get("serverchan"):
            requests.post(url, data={"title": title, "desp": body}, timeout=8)
        if url := n.get("webhook"):
            requests.post(url, json={"title": title, "body": body}, timeout=8)
    except Exception as e:
        LOG.warning("通知没发出去：%s", e)


# ---------------------------------------------------------------- 主流程


def run(cfg: dict, wait: bool, dry: bool) -> int:
    site = cfg["site"]["base"].rstrip("/")
    book = cfg["booking"]
    retry = cfg.get("retry", {})

    session = requests.Session()
    session.headers["User-Agent"] = UA

    offset = clock_offset(session, site)
    target = next_open(book["open_at"])

    if wait:
        login_at = target - timedelta(seconds=book.get("login_ahead_sec", 60))
        LOG.info("开放时刻 %s，%s 先登录", f"{target:%m-%d %H:%M:%S}", f"{login_at:%H:%M:%S}")
        sleep_until(login_at, offset)

    LOG.info("登录中")
    login(session, cfg)
    LOG.info("登录好了")

    if dry:
        LOG.info("dry-run，不提交")
        return 0

    if wait:
        LOG.info("等整点")
        sleep_until(target, offset)

    seats = [s for s in book["seats"] if s.get("id")]
    if not seats:
        LOG.error("config 里一个座位 id 都没填")
        return 2

    for attempt in range(1, int(retry.get("times", 8)) + 1):
        for seat in seats:
            label = seat.get("label") or seat["id"]
            try:
                ok, msg = submit(session, cfg, seat)
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
            LOG.info("第 %d 轮 · %s · %s · %s", attempt, label, "成功" if ok else "没抢到", msg)
            if ok:
                notify(cfg, "约到了", f"{label}　{book['start']}-{book['end']}")
                return 0
        time.sleep(float(retry.get("interval", 0.4)))

    LOG.error("都没抢到")
    notify(cfg, "没约到", "座位都被抢了，或者接口变了，看日志")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true", help="不等开放时刻，立刻跑")
    ap.add_argument("--dry-run", action="store_true", help="只登录，不提交")
    ap.add_argument("-c", "--config", default=str(HERE / "config.toml"))
    a = ap.parse_args()

    path = Path(a.config)
    if not path.exists():
        LOG.error("没有 %s，先从 config.example.toml 复制一份", path)
        return 2
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))

    try:
        return run(cfg, wait=not a.now, dry=a.dry_run)
    except NotImplementedError as e:
        LOG.error("接口还没填：%s", e)
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
