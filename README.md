# Alcove

一个单页聊天前端。iOS 液态玻璃质感、多会话、四套主题、可发图片、可添加到主屏幕。

线上地址：<https://alcove.page>

## 文件

| 文件 | 说明 |
|---|---|
| `index.html` | 全部界面与逻辑，无构建步骤 |
| `fonts/` | 霞鹜文楷 Regular / Light，自托管 |
| `manifest.webmanifest` | PWA 清单 |
| `icons/` | 三套图标，各 4 个尺寸 + SVG 源文件 |
| `sw.js` | 墓碑 Service Worker，清掉旧版本留下的缓存 |
| `backend/server.py` | FastAPI 后端：转发 LLM、检索记忆库 |
| `deploy.sh` | 服务器上一步部署 |

## 部署

前端进静态目录，后端进它上一层：

```bash
# 首次
cd /opt/alcove && git clone https://github.com/shuhan200603-star/alcove.page frontend

# 之后每次更新，就这一行
sudo /opt/alcove/frontend/deploy.sh
```

`deploy.sh` 拉代码、同步前端，只在 `backend/server.py` 真的变了时才复制并重启——重启会掐断正在进行的对话，能免则免。

`backend/` 不进静态目录：那个目录是公开的，后端代码不该出现在那里。

**别在 `/opt/alcove/frontend/` 里改文件。** 它是仓库的一面镜子，改了下次 `git pull --ff-only` 会直接失败。要改代码就改仓库，再 pull 下来。

前端是一组静态文件，没有构建步骤——`index.html` 加上 `fonts/`、`icons/` 就是全部。

## 后端约定

前端默认 POST 到同源的 `/api/chat`，跟后端一起部署时无需配置；设置里可以改成别的地址，留空则进演示模式。

默认地址返回 404/405 或压根连不上时，会安静地退回演示模式——那只说明这份页面旁边没有后端。用户自己填过地址就不再吞错误，状态码会直接显示在气泡里。

**请求**

```json
{
  "messages": [
    { "role": "user", "content": "你好" },
    { "role": "assistant", "content": "……" }
  ]
}
```

带图片时该条消息的 `content` 变成 Anthropic 的内容块数组：

```json
{
  "role": "user",
  "content": [
    { "type": "image", "source": { "type": "base64", "media_type": "image/jpeg", "data": "…" } },
    { "type": "text", "text": "这件怎么样" }
  ]
}
```

顶层还有两个可选布尔值，都默认 `true`：`use_memory` 关掉记忆检索，`use_web` 关掉联网搜索。

后端另有 `GET /api/memory/status`，返回 `{"ok": bool, ...}`，顶栏的状态点就是问它。

**响应**

```json
{ "content": [ { "type": "text", "text": "回复内容" } ] }
```

多个 text 块会按顺序换行拼接。非 2xx 会在气泡里显示状态码。

API 密钥只放在服务器上，前端不接触——`server.py` 从环境变量读，不落在代码里。

## 后端

`backend/server.py` 是一层薄适配器，做三件事：

1. **转发**。把消息发给 `LLM_BASE`（默认 OpenRouter 的 `/chat/completions`），把回复转成上面那个 `{content:[...]}` 形状。
2. **检索记忆**。每轮先拿最新一条用户消息去问 Ombre Brain 的 `breath` 工具（MCP over streamable-http），检索到的内容作为额外的 system 段落一起发出去。记忆库超时或出错一律静默跳过，不拖垮聊天。
3. **翻译图片块**。前端按 Anthropic 的 `{type:"image", source:{...}}` 发图，OpenAI 兼容的 `/chat/completions` 只认 `image_url` —— `_to_openai_content()` 在转发前统一翻译。换后端时只动这个函数，界面不用跟着改。
4. **联网搜索**。挂上 OpenRouter 的 `plugins: [{"id": "web"}]`。对 `anthropic/claude-*` 它默认走 `engine: "native"`，底层就是 Anthropic 官方的 web search tool——所以搜不搜由模型自己判断，不需要在这里做关键词粗筛。引用过的来源在 `message.annotations` 里，`_format_sources()` 把它们附在正文末尾（前端只渲染文本，不然就丢了）。

联网按 Anthropic 官方定价透传：约 $10 / 1000 次搜索，外加搜索结果占用的 token。开着不等于每轮都花钱——只有模型真的去搜才计费。用 `WEB_SEARCH=0` 可以在服务端整体关掉。

全部配置走环境变量：`LLM_API_KEY`、`LLM_BASE`、`LLM_MODEL`、`MEMORY_MCP_URL`、`MEMORY_TOKEN`、`MEMORY_TIMEOUT`、`MEMORY_MAX_RESULTS`、`WEB_SEARCH`。

`/api/chat` 目前没有访问控制——任何人打开站点就能消耗你的 API 额度。

## 关于「液态玻璃」

玻璃是**浮层**，不是全局滤镜。侧栏、气泡、输入条、顶栏各自用 `backdrop-filter` 模糊自己背后的内容，边缘保持锐利并带一道内高光；页面内容本身永远清晰。背景是三团缓慢漂移的色块，所以透过玻璃看到的花纹会变——这是「液态」的来源。

`prefers-reduced-motion` 下漂移会停止。

## 存储

会话、主题、服务器地址都存在 `localStorage`，不上传。清浏览器数据即清空。

| 键 | 内容 |
|---|---|
| `alcove.sessions` | 全部会话与消息 |
| `alcove.current` | 当前会话 id |
| `alcove.theme` | 主题 |
| `alcove.conf` | 服务器地址、记忆库和联网开关 |

## 图标

侧栏「图标」三选一，默认「龛里」：

| | |
|---|---|
| 龛里 | 夜色天，月亮在右上 |
| 月在左 | 同上，月亮挪到左上角 |
| 浅色 | 白天的天，壁龛是暗的那块 |

每套是 `icons/<名字>.svg` 加 180 / 192 / 512 / 512-maskable 四个 PNG。SVG 给标签页用（任意尺寸都清晰），PNG 给 iOS 和 Android 装应用用。

切换改的是 `<link rel="icon">` 和 `<link rel="apple-touch-icon">` 的 `href`：

- **标签页图标**立刻跟着变
- **主屏幕图标不会**。iOS 在「添加到主屏幕」那一刻就把图存进系统了，网页之后够不着它——要换只能删掉重加
- **`manifest.webmanifest` 是静态文件**，改不了，所以 Android 的安装图标固定是「龛里」那套

新增一套：往 `icons/` 放好文件，再往侧栏加一个 `.ic` 按钮即可。

## 主题

薰衣草紫 · 青柠 · 奶茶 · 深夜。在侧栏底部「外观」切换。

新增主题只需在 `index.html` 顶部的 CSS 里加一组 `[data-theme="名字"]` 变量，再往侧栏加一个 `.sw` 按钮。

## 字体

侧栏「字体」四选一，默认文楷：

| | 说明 |
|---|---|
| 文楷 | 霞鹜文楷 Regular，自托管 |
| 细楷 | 霞鹜文楷 Light |
| 楷体 | 系统楷体，零下载（iOS/macOS 有，其他平台会退回黑体） |
| 黑体 | 系统无衬线 |

字体文件按 unicode-range 切成了约 97 个子集，浏览器只下载页面上真正用到的那几块，通常一两百 KB，不是仓库里那 11 MB。

字重跟着字体走：`--w-strong` / `--w-med` 定义在每个 `[data-font]` 里。楷体笔画自带粗细变化，字重压到 400 才不会糊；黑体则用 650/560 保持层次。加新字体时这两个变量要一起给。

霞鹜文楷采用 SIL Open Font License 1.1，许可证见 `fonts/OFL.txt`。
