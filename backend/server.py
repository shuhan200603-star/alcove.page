import os, json, asyncio, httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles

KEY   = os.environ["LLM_API_KEY"]
BASE  = os.environ.get("LLM_BASE", "https://openrouter.ai/api/v1/chat/completions")
MODEL = os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4.5")

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
MEMORY_TIMEOUT     = float(os.environ.get("MEMORY_TIMEOUT", "8"))
MEMORY_STATUS_TIMEOUT = float(os.environ.get("MEMORY_STATUS_TIMEOUT", "3"))
MEMORY_MAX_RESULTS = int(os.environ.get("MEMORY_MAX_RESULTS", "3"))

# breath 在检索失败/无结果时会返回这些中文提示语，它们不是真正的记忆，别塞给 LLM。
_MEMORY_SENTINELS = ("检索过程出错", "记忆系统暂时无法访问", "权重池平静", "没有找到")

app = FastAPI()


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
    query = query[:1500]

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
async def memory_status_endpoint():
    return await memory_status()


@app.post("/api/chat")
async def chat(req: Request):
    body = await req.json()
    msgs = body.get("messages") or []
    if not msgs:
        raise HTTPException(400, "messages 是空的")

    system_msgs = [{"role": "system", "content": "你是克劳德。用中文，语气自然，不要客套。"}]

    # 转发给 LLM 前，先用最新一条用户消息检索记忆，拼成额外的 system 段落。
    if body.get("use_memory", True):
        memory = await retrieve_memory(_latest_user_text(msgs))
        if memory:
            system_msgs.append({
                "role": "system",
                "content": "以下是从你的记忆库里检索到的相关记忆，供你参考，"
                           "自然地融入回应，不要照搬也不要提及“记忆库”：\n\n" + memory,
            })

    payload = {
        "model": MODEL,
        "max_tokens": 4096,
        "messages": system_msgs + _to_openai_messages(msgs),
    }
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(BASE, headers={"Authorization": f"Bearer {KEY}"}, json=payload)
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text[:300])
    d = r.json()
    return {"content": [{"type": "text", "text": d["choices"][0]["message"]["content"]}]}

app.mount("/", StaticFiles(directory="static", html=True), name="static")
