import os, json, asyncio, hmac, sqlite3, time, httpx
from contextlib import contextmanager
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles

LLM_BACKEND = os.environ.get("LLM_BACKEND", "openrouter").strip().lower()
KEY   = os.environ.get("LLM_API_KEY", "")
BASE  = os.environ.get("LLM_BASE", "https://openrouter.ai/api/v1/chat/completions")
MODEL = os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4.5")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
ACCESS_TOKEN = os.environ.get("ALCOVE_ACCESS_TOKEN", "")

if LLM_BACKEND not in ("openrouter", "claude_code"):
    raise RuntimeError("LLM_BACKEND must be openrouter or claude_code")
if LLM_BACKEND == "openrouter" and not KEY:
    raise RuntimeError("LLM_API_KEY is required for OpenRouter")
if LLM_BACKEND == "claude_code" and len(ACCESS_TOKEN) < 32:
    raise RuntimeError("Set a random ALCOVE_ACCESS_TOKEN of at least 32 characters before using Claude Code")
if LLM_BACKEND == "claude_code" and os.environ.get("ANTHROPIC_API_KEY"):
    raise RuntimeError("Remove ANTHROPIC_API_KEY from this service so Claude Code uses the signed-in plan")

# Claude Agent SDK emits plan rate-limit events while responding. These figures
# are a snapshot, not an API-billing statement or a live poll of claude.ai.
PLAN_USAGE: dict[str, dict] = {}
CLAUDE_STATUS = {"last_success_at": None}

# Optional private Telegram bridge. The owner ID is explicit so nobody can
# claim the bot by messaging it first. Bot credentials stay on the VPS.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_OWNER_ID = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
TELEGRAM_DB = Path(os.environ.get("ALCOVE_DATA_DB", "/opt/alcove/alcove.sqlite3"))
if TELEGRAM_OWNER_ID and not TELEGRAM_OWNER_ID.isdigit():
    raise RuntimeError("TELEGRAM_OWNER_ID must be numeric")
if TELEGRAM_TOKEN and len(ACCESS_TOKEN) < 32:
    raise RuntimeError("Set a random ALCOVE_ACCESS_TOKEN of at least 32 characters before enabling Telegram")

# --- 记忆库（Ombre Brain）配置，全部从环境变量读，不硬编码 ---
# 本机 Ombre Brain 是 streamable-http 的 MCP 服务，工具端点在 /mcp，
# 另有一个轻量 GET /health 用于探活。本地访问无需鉴权；若将来走带 OAuth
# 的公网地址，把 MEMORY_TOKEN 设成 Bearer 令牌即可，仍然不落在代码里。
MEMORY_MCP_URL     = os.environ.get("MEMORY_MCP_URL", "http://127.0.0.1:8000/mcp")
MEMORY_HEALTH_URL  = os.environ.get(
    "MEMORY_HEALTH_URL",
    MEMORY_MCP_URL[:-4] + "/health" if MEMORY_MCP_URL.endswith("/mcp") else "",
)
MEMORY_TOKEN       = os.environ.get("MEMORY_TOKEN", "").strip()
# breath 每条记忆都过一遍 DeepSeek 脱水，实测 ~5.5s，所以超时给 8s；可用环境变量调。
MEMORY_TIMEOUT     = float(os.environ.get("MEMORY_TIMEOUT", "15"))
MEMORY_STATUS_TIMEOUT = float(os.environ.get("MEMORY_STATUS_TIMEOUT", "3"))
MEMORY_MAX_RESULTS = int(os.environ.get("MEMORY_MAX_RESULTS", "3"))

# breath 在检索失败/无结果时会返回这些中文提示语，它们不是真正的记忆，别塞给 LLM。
_MEMORY_SENTINELS = ("检索过程出错", "记忆系统暂时无法访问", "权重池平静", "没有找到")

# --- 联网搜索 ---
# OpenRouter 的 web 插件：对 anthropic/claude-* 默认走 engine="native"，
# 底层就是 Anthropic 自己的 web search tool。搜不搜由模型自己判断，
# 所以不需要在这里做关键词粗筛——它不会每句话都去搜。
# 按 Anthropic 官方定价透传（约 $10 / 1000 次搜索，外加结果占用的 token）。
WEB_SEARCH_DEFAULT = os.environ.get("WEB_SEARCH", "1").strip().lower() \
    not in ("0", "false", "no", "off", "")

app = FastAPI()
TELEGRAM_LOCK = asyncio.Lock()
TELEGRAM_STATUS = {"connected": False, "error": None,
                   "candidate_id": None, "candidate_name": None}


@contextmanager
def _telegram_db():
    TELEGRAM_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(TELEGRAM_DB, timeout=10)
    TELEGRAM_DB.chmod(0o600)
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("CREATE TABLE IF NOT EXISTS tg_messages ("
               "id INTEGER PRIMARY KEY, role TEXT NOT NULL, text TEXT NOT NULL, "
               "origin TEXT UNIQUE, created_at INTEGER NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS tg_updates ("
               "update_id INTEGER PRIMARY KEY, reply TEXT, delivered INTEGER NOT NULL DEFAULT 0)")
    db.execute("CREATE TABLE IF NOT EXISTS tg_state ("
               "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.commit()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _telegram_history(limit: int = 30) -> list[dict]:
    with _telegram_db() as db:
        rows = db.execute("SELECT role, text FROM tg_messages ORDER BY id DESC LIMIT ?",
                          (limit,)).fetchall()
    return [{"role": role, "content": text} for role, text in reversed(rows)]


def _telegram_append(role: str, message: str, origin: str | None = None) -> None:
    with _telegram_db() as db:
        db.execute("INSERT OR IGNORE INTO tg_messages(role, text, origin, created_at) "
                   "VALUES (?, ?, ?, ?)", (role, message, origin, int(time.time())))


def _telegram_offset() -> int:
    with _telegram_db() as db:
        row = db.execute("SELECT value FROM tg_state WHERE key = 'offset'").fetchone()
    return int(row[0]) if row else 0


def _telegram_set_offset(offset: int) -> None:
    with _telegram_db() as db:
        db.execute("INSERT OR REPLACE INTO tg_state(key, value) VALUES ('offset', ?)",
                   (str(offset),))


async def _telegram_process(update: dict, client: httpx.AsyncClient) -> bool:
    update_id = update.get("update_id")
    message = update.get("message") or {}
    sender = message.get("from") or {}
    if (message.get("chat") or {}).get("type") != "private":
        return True
    if not TELEGRAM_OWNER_ID:
        TELEGRAM_STATUS["candidate_id"] = sender.get("id")
        TELEGRAM_STATUS["candidate_name"] = sender.get("username")
        return True  # Discovery only: never auto-bind the first sender.
    if str(sender.get("id", "")) != TELEGRAM_OWNER_ID:
        return True
    text = (message.get("text") or "").strip()
    if not text:
        return True  # Text first; photos and voice can be added after the bridge works.
    chat_id = (message.get("chat") or {}).get("id")
    if not isinstance(chat_id, int):
        return True

    async with TELEGRAM_LOCK:
        with _telegram_db() as db:
            db.execute("INSERT OR IGNORE INTO tg_updates(update_id) VALUES (?)", (update_id,))
            row = db.execute("SELECT reply, delivered FROM tg_updates WHERE update_id = ?",
                             (update_id,)).fetchone()
        if row[1]:
            return True
        _telegram_append("user", text, f"tg:{update_id}")
        reply = row[0]
        if reply is None:
            reply = await _answer(_telegram_history(), True, WEB_SEARCH_DEFAULT)
            with _telegram_db() as db:
                db.execute("UPDATE tg_updates SET reply = ? WHERE update_id = ?",
                           (reply, update_id))
        _telegram_append("assistant", reply, f"tg-reply:{update_id}")

        # Telegram sendMessage has no idempotency key: a network timeout after
        # delivery may result in a duplicate on retry. Keep the same reply text.
        if not await _telegram_send(client, chat_id, reply):
            return False
        with _telegram_db() as db:
            db.execute("UPDATE tg_updates SET delivered = 1 WHERE update_id = ?",
                       (update_id,))
    return True


async def _telegram_send(client: httpx.AsyncClient, chat_id: int, message: str) -> bool:
    for start in range(0, len(message), 4096):
        result = await client.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                   json={"chat_id": chat_id,
                                         "text": message[start:start + 4096]})
        result.raise_for_status()
        if not result.json().get("ok"):
            return False
    return True


async def _telegram_poll() -> None:
    # One uvicorn worker only. getUpdates cannot run while a webhook is set.
    async with httpx.AsyncClient(timeout=40) as client:
        while True:
            try:
                offset = _telegram_offset()
                response = await client.get(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                    params={"offset": offset, "timeout": 25, "allowed_updates": '["message"]'},
                )
                response.raise_for_status()
                data = response.json()
                if not data.get("ok"):
                    raise RuntimeError("Telegram getUpdates failed")
                TELEGRAM_STATUS.update(connected=True, error=None)
                for update in data.get("result", []):
                    if await _telegram_process(update, client):
                        _telegram_set_offset(update["update_id"] + 1)
                    else:
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                TELEGRAM_STATUS.update(connected=False, error=type(exc).__name__)
                await asyncio.sleep(10)


@app.on_event("startup")
async def _start_telegram():
    if TELEGRAM_TOKEN:
        with _telegram_db():
            pass
        app.state.telegram_task = asyncio.create_task(_telegram_poll())


@app.on_event("shutdown")
async def _stop_telegram():
    task = getattr(app.state, "telegram_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _require_access(req: Request) -> None:
    """Keep the private Claude subscription behind a server-side access key."""
    if not ACCESS_TOKEN:
        return
    supplied = req.headers.get("x-alcove-access", "")
    if not hmac.compare_digest(supplied, ACCESS_TOKEN):
        raise HTTPException(401, "请在设置中填写 Alcove 访问口令")


def _claude_prompt(msgs: list) -> tuple[str, list]:
    """Keep recent text context and preserve images in the latest user turn."""
    prior = []
    for msg in msgs[:-1][-24:]:
        if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(str(block.get("text", "")) for block in content
                             if isinstance(block, dict) and block.get("type") == "text")
            if any(isinstance(block, dict) and block.get("type") == "image" for block in content):
                text += " [图片]"
        else:
            continue
        prior.append(("用户" if msg["role"] == "user" else "助手") + "：" + text[:4000])
    history = "\n".join(prior)

    latest = msgs[-1].get("content", "")
    blocks = [{"type": "text", "text": "以下是近期对话，请据此继续回应。\n" + history + "\n\n用户的新消息："}]
    if isinstance(latest, str):
        blocks.append({"type": "text", "text": latest})
    elif isinstance(latest, list):
        for block in latest:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                blocks.append({"type": "text", "text": str(block.get("text", ""))})
            elif block.get("type") == "image":
                source = block.get("source") or {}
                if source.get("type") == "base64" and source.get("data"):
                    blocks.append({"type": "image", "source": {
                        "type": "base64",
                        "media_type": source.get("media_type", "image/jpeg"),
                        "data": source["data"],
                    }})
    return history, blocks


def _record_plan_usage(info) -> None:
    """Prefer the CLI's per-window snapshot when available; leave gaps explicit."""
    known = {"five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus"}
    reported = getattr(info, "raw", {})
    raw = reported if isinstance(reported, dict) else {}
    windows = raw.get("unifiedWindows") or raw.get("unified_windows") or {}
    observed = int(time.time())
    found = False
    if isinstance(windows, dict):
        for name, value in windows.items():
            if name not in known or not isinstance(value, dict):
                continue
            fraction = value.get("utilization")
            reset = value.get("resetsAt", value.get("resets_at"))
            PLAN_USAGE[name] = {
                "used_percentage": round(fraction * 100, 1)
                if isinstance(fraction, (int, float)) else None,
                "resets_at": reset,
                "status": info.status,
                "observed_at": observed,
            }
            found = True
    if not found and info.rate_limit_type in known:
        PLAN_USAGE[info.rate_limit_type] = {
            "used_percentage": round(info.utilization * 100, 1)
            if info.utilization is not None else None,
            "resets_at": info.resets_at,
            "status": info.status,
            "observed_at": observed,
        }


# 让 Claude 自己也能翻记忆库：检索、存新记忆、写日记、看状态。
# 不放 trace——它能删记忆、改钉选，这种事留给你自己做。
MEMORY_TOOLS = ["breath", "hold", "grow", "pulse"]


def _memory_mcp_config() -> dict:
    cfg = {"type": "http", "url": MEMORY_MCP_URL}
    if MEMORY_TOKEN:
        cfg["headers"] = {"Authorization": f"Bearer {MEMORY_TOKEN}"}
    return cfg


async def _claude_reply(msgs: list, system: str, use_web: bool,
                        use_memory: bool = False) -> str:
    """Run the signed-in local Claude Code account through the official Agent SDK."""
    try:
        from claude_agent_sdk import (query, ClaudeAgentOptions, AssistantMessage,
                                      ResultMessage, RateLimitEvent, TextBlock)
    except ImportError as exc:
        raise HTTPException(503, "服务器尚未安装 claude-agent-sdk") from exc

    _, blocks = _claude_prompt(msgs)

    async def prompt_stream():
        yield {"type": "user", "message": {"role": "user", "content": blocks}}

    web_tools = ["WebSearch", "WebFetch"] if use_web else []
    memory_tools = [f"mcp__memory__{name}" for name in MEMORY_TOOLS] if use_memory else []
    options = ClaudeAgentOptions(
        model=CLAUDE_MODEL,
        system_prompt=system,
        tools=web_tools,
        allowed_tools=web_tools + memory_tools,
        mcp_servers={"memory": _memory_mcp_config()} if use_memory else {},
        setting_sources=[],
        permission_mode="dontAsk",
        max_turns=8 if (use_web or use_memory) else 1,
    )
    last_text = ""
    final = None
    try:
        async for event in query(prompt=prompt_stream(), options=options):
            if isinstance(event, RateLimitEvent):
                _record_plan_usage(event.rate_limit_info)
            elif isinstance(event, AssistantMessage):
                last_text = "\n".join(block.text for block in event.content
                                      if isinstance(block, TextBlock)) or last_text
            elif isinstance(event, ResultMessage):
                final = event
    except Exception as exc:
        raise HTTPException(502, "Claude Code 调用失败：" + type(exc).__name__) from exc
    if final is None or final.is_error:
        raise HTTPException(502, "Claude Code 未能完成回复")
    CLAUDE_STATUS["last_success_at"] = int(time.time())
    return (final.result or last_text).strip()


def _recent_user_text(msgs: list, n: int = 3) -> str:
    """最近几条用户消息拼在一起，给记忆检索当关键词——只看最后一句太容易扑空。"""
    texts = []
    for m in reversed(msgs):
        if len(texts) >= n:
            break
        if isinstance(m, dict) and m.get("role") == "user":
            t = _latest_user_text([m])
            if t:
                texts.append(t)
    return "\n".join(reversed(texts))


def _latest_user_text(msgs: list) -> str:
    """取最新一条 user 消息的纯文本，content 可能是字符串或含图片的块列表。"""
    for m in reversed(msgs):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            return "\n".join(p for p in parts if p).strip()
        return ""
    return ""


def _to_openai_content(content):
    """前端按 Anthropic 的块格式发图片（{type:"image", source:{...}}），
    但 BASE 指向的是 OpenAI 兼容的 /chat/completions，它只认 image_url。
    两边格式对不上时图片会被悄悄丢掉，所以在转发前统一翻译一遍。

    翻译放在这里而不是前端：前端对着一份固定的契约写（见仓库 README），
    换后端时只动这个函数，界面不用跟着改。
    """
    if not isinstance(content, list):
        return content

    out = []
    for b in content:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "text":
            out.append({"type": "text", "text": b.get("text", "")})
        elif kind == "image_url":
            out.append(b)                      # 已经是 OpenAI 格式，原样放行
        elif kind == "image":
            src = b.get("source") or {}
            if src.get("type") == "base64":
                media = src.get("media_type", "image/jpeg")
                data = src.get("data", "")
                if data:
                    out.append({"type": "image_url",
                                "image_url": {"url": f"data:{media};base64,{data}"}})
            elif src.get("type") == "url" and src.get("url"):
                out.append({"type": "image_url", "image_url": {"url": src["url"]}})
    return out


def _to_openai_messages(msgs: list) -> list:
    return [{**m, "content": _to_openai_content(m.get("content"))}
            for m in msgs if isinstance(m, dict)]


def _format_sources(annotations) -> str:
    """联网搜索引用过的来源，OpenRouter 放在 message.annotations 里。
    前端只渲染文本，所以在正文末尾附一小段来源列表，而不是丢掉。"""
    if not isinstance(annotations, list):
        return ""
    seen, lines = set(), []
    for a in annotations:
        if not isinstance(a, dict) or a.get("type") != "url_citation":
            continue
        cite = a.get("url_citation") or {}
        url = (cite.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        title = (cite.get("title") or url).strip()
        lines.append(f"{len(lines) + 1}. {title}\n   {url}")
        if len(lines) >= 6:
            break
    return "\n\n———\n来源：\n" + "\n".join(lines) if lines else ""


def _parse_jsonrpc(text: str):
    """streamable-http 的响应是 SSE（data: {json}），也可能是纯 JSON。"""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except Exception:
                return None
    try:
        return json.loads(text)
    except Exception:
        return None


async def retrieve_memory(query: str):
    """用 query 调 Ombre Brain 的 breath 工具，返回记忆文本；任何失败都静默返回 None。"""
    query = (query or "").strip()
    if not query:
        return None
    query = query[-1500:]   # 太长时保留最新的部分

    async def _do():
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if MEMORY_TOKEN:
            headers["Authorization"] = f"Bearer {MEMORY_TOKEN}"

        async with httpx.AsyncClient(timeout=MEMORY_TIMEOUT) as c:
            # 1. initialize —— 建立 MCP 会话，拿 session id
            init = {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "alcove", "version": "1"},
                },
            }
            r = await c.post(MEMORY_MCP_URL, headers=headers, json=init)
            r.raise_for_status()
            sid = r.headers.get("mcp-session-id")
            h2 = dict(headers)
            if sid:
                h2["mcp-session-id"] = sid

            # 2. initialized 通知（fire-and-forget）
            await c.post(MEMORY_MCP_URL, headers=h2,
                         json={"jsonrpc": "2.0", "method": "notifications/initialized"})

            # 3. tools/call breath
            call = {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {
                    "name": "breath",
                    "arguments": {"query": query, "max_results": MEMORY_MAX_RESULTS},
                },
            }
            r = await c.post(MEMORY_MCP_URL, headers=h2, json=call)
            r.raise_for_status()

            data = _parse_jsonrpc(r.text)
            if not isinstance(data, dict):
                return None
            result = data.get("result")
            if not isinstance(result, dict) or result.get("isError"):
                return None
            parts = [b.get("text", "") for b in result.get("content", [])
                     if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n".join(p for p in parts if p).strip()
            if not text or any(s in text for s in _MEMORY_SENTINELS):
                return None
            return text

    try:
        return await asyncio.wait_for(_do(), timeout=MEMORY_TIMEOUT)
    except Exception:
        # 记忆库挂了/超时/协议异常都不能拖垮聊天，静默跳过。
        return None


async def memory_status() -> dict:
    """探测记忆库是否可达，走轻量的 GET /health。"""
    if not MEMORY_HEALTH_URL:
        return {"ok": False, "error": "no_health_url"}
    try:
        headers = {}
        if MEMORY_TOKEN:
            headers["Authorization"] = f"Bearer {MEMORY_TOKEN}"
        async with httpx.AsyncClient(timeout=MEMORY_STATUS_TIMEOUT) as c:
            r = await c.get(MEMORY_HEALTH_URL, headers=headers)
        if r.status_code == 200:
            try:
                detail = r.json()
            except Exception:
                detail = {}
            return {"ok": True, "detail": detail}
        return {"ok": False, "status_code": r.status_code}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}


@app.get("/api/memory/status")
async def memory_status_endpoint(req: Request):
    _require_access(req)
    return await memory_status()


@app.get("/api/usage")
async def usage_endpoint(req: Request):
    _require_access(req)
    return {"backend": LLM_BACKEND, "plan": PLAN_USAGE,
            "last_success_at": CLAUDE_STATUS["last_success_at"]}


@app.get("/api/telegram/history")
async def telegram_history_endpoint(req: Request):
    _require_access(req)
    return {"enabled": bool(TELEGRAM_TOKEN), "connected": TELEGRAM_STATUS["connected"],
            "owner_configured": bool(TELEGRAM_OWNER_ID),
            "candidate_id": TELEGRAM_STATUS["candidate_id"],
            "candidate_name": TELEGRAM_STATUS["candidate_name"],
            "error": TELEGRAM_STATUS["error"],
            "messages": _telegram_history(100) if TELEGRAM_TOKEN else []}


async def _answer(msgs: list, use_memory: bool, use_web: bool) -> str:
    if not isinstance(msgs, list) or not msgs or not isinstance(msgs[-1], dict) \
            or msgs[-1].get("role") != "user":
        raise HTTPException(400, "最后一条消息必须是用户消息")

    system_texts = ["你是克劳德。用中文，语气自然，不要客套。"]

    # 转发给 LLM 前，先用最新一条用户消息检索记忆，拼成额外的 system 段落。
    if use_memory:
        memory = await retrieve_memory(_recent_user_text(msgs))
        if memory:
            system_texts.append(
                "以下是这一轮从你们共同的记忆库里自动检索到的内容。自然地用，不用每句都说出处；"
                "但对方问你怎么知道的，就如实说是记忆库里记着的。\n\n" + memory)
        # 每轮都是全新进程：上一轮检索到的记忆不会带过来，只剩对话文字。
        # 不说清楚这一点，它看到自己前面说过的事却找不到出处，就会怀疑是自己编的、
        # 来回改口——之前的"记忆错乱"就是这么来的。
        system_texts.append(
            "注意：对话历史里你之前提到的往事，通常来自当时检索到的记忆，"
            "只是那些检索结果不会保留到这一轮。拿不准时先用 breath 核实，"
            "查到了就照记忆说，查不到就说“我这会儿没查到”，"
            "不要因为眼前看不到出处，就断定是自己编的、推翻前面的话。"
            "反过来，记忆和对话里都没有的事，也不要说成自己记得。")

    if LLM_BACKEND == "claude_code":
        if use_memory:
            system_texts.append(
                "你可以用 memory 工具翻你们共同的记忆库：breath 检索（不带 query 就是随机浮现），"
                "hold 存一条新记忆，grow 把一段经历写成日记，pulse 看记忆库状态。"
                "上面没检索到、但这轮需要了解对方的时候（比如对方问你记不记得某事、问自己的情况），"
                "先用 breath 查一下再回答，别说自己不知道。"
                "对方明确让你记住什么，或者聊到值得长期记住的事，就用 hold 存下来。"
                "用工具时不用汇报过程，自然地回应就好。")
        return await _claude_reply(msgs, "\n\n".join(system_texts), use_web, use_memory)

    payload = {
        "model": MODEL,
        "max_tokens": 4096,
        "messages": [{"role": "system", "content": text} for text in system_texts]
                    + _to_openai_messages(msgs),
    }

    # 挂上 web 插件只是把搜索这个工具交给模型，用不用它自己决定——
    # 所以开着不等于每轮都花钱。
    if use_web:
        payload["plugins"] = [{"id": "web"}]

    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(BASE, headers={"Authorization": f"Bearer {KEY}"}, json=payload)
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text[:300])
    d = r.json()
    message = d["choices"][0]["message"]
    return (message.get("content") or "") + _format_sources(message.get("annotations"))


@app.post("/api/chat")
async def chat(req: Request):
    _require_access(req)
    body = await req.json()
    msgs = body.get("messages") or []
    if body.get("channel") == "telegram":
        if not TELEGRAM_TOKEN or not TELEGRAM_OWNER_ID:
            raise HTTPException(503, "Telegram 尚未连接")
        latest = _latest_user_text(msgs)
        if not latest:
            raise HTTPException(400, "Telegram 同步对话暂时只支持文字")
        async with TELEGRAM_LOCK:
            history = _telegram_history()
            answer = await _answer(history + [{"role": "user", "content": latest}],
                                   body.get("use_memory", True),
                                   body.get("use_web", WEB_SEARCH_DEFAULT))
            _telegram_append("user", latest)
            _telegram_append("assistant", answer)
            # Mirror the web turn into the owner's private Telegram chat.
            # The database remains the source of truth if Telegram is offline.
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    if not await _telegram_send(client, int(TELEGRAM_OWNER_ID), "你（网页）：" + latest):
                        raise RuntimeError("Telegram rejected the message")
                    if not await _telegram_send(client, int(TELEGRAM_OWNER_ID), "Claude：" + answer):
                        raise RuntimeError("Telegram rejected the reply")
            except (httpx.HTTPError, RuntimeError):
                TELEGRAM_STATUS.update(connected=False, error="SendMessageError")
    else:
        answer = await _answer(msgs, body.get("use_memory", True),
                               body.get("use_web", WEB_SEARCH_DEFAULT))
    return {"content": [{"type": "text", "text": answer}]}

app.mount("/", StaticFiles(directory="static", html=True), name="static")

