# Alcove

一个单页聊天前端。iOS 液态玻璃质感、多会话、四套主题、可发图片、可添加到主屏幕。

线上地址：<https://alcove.page>

## 文件

| 文件 | 说明 |
|---|---|
| `index.html` | 全部界面与逻辑，无外部依赖，无构建步骤 |
| `manifest.webmanifest` | PWA 清单 |
| `icon.svg` | 应用图标 |

## 部署

把这三个文件放进静态目录即可：

```bash
scp index.html manifest.webmanifest icon.svg root@你的服务器:/opt/alcove/static/
```

只复制 `index.html` 也能跑——图标是内嵌的 data URI，iOS 添加到主屏幕照常工作，只是少了 Android 的安装提示。

## 后端约定

前端 POST 到设置里填的地址（留空则进演示模式）：

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
