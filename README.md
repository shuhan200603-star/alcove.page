# Alcove

一个单页聊天前端。iOS 液态玻璃质感、多会话、四套主题、可发图片、可添加到主屏幕。

线上地址：<https://alcove.page>

## 文件

| 文件 | 说明 |
|---|---|
| `index.html` | 全部界面与逻辑，无构建步骤 |
| `fonts/` | 霞鹜文楷 Regular / Light，自托管 |
| `manifest.webmanifest` | PWA 清单 |
| `icon.svg` | 应用图标 |

## 部署

整个目录放进静态目录即可：

```bash
rsync -a --exclude .git ./ root@你的服务器:/opt/alcove/static/
```

或者直接在服务器上 clone：

```bash
cd /opt/alcove/static && git clone https://github.com/shuhan200603-star/alcove.page .
```

只复制 `index.html` 也能跑——字体会退回系统楷体，图标是内嵌的 data URI，iOS 添加到主屏幕照常工作。

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

设置里填了「记忆库」地址时，请求体额外带一个顶层 `mcp` 字段。

**响应**

```json
{ "content": [ { "type": "text", "text": "回复内容" } ] }
```

多个 text 块会按顺序换行拼接。非 2xx 会在气泡里显示状态码。

API 密钥只放在服务器上，前端不接触。

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
| `alcove.conf` | 服务器地址、记忆库地址 |

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
