# YouTube 发布（Data API v3 / OAuth2）

与其他平台不同，YouTube 是纯 HTTP 集成（无 Chrome/CDP/Playwright），代码：

- `scripts/youtube_auth.py` — OAuth2 loopback 授权 + token 缓存/自动刷新
- `scripts/platforms/youtube.py` — resumable upload（分块、5xx 断点续传、308 Range 续传）

## 为什么 API Key 不能上传

`videos.insert` 强制 OAuth access token + `youtube.upload` scope；API Key 调上传接口直接
`401 "API keys are not supported by this API"`。Key 只够读公开数据。

## 一次性配置

1. Google Cloud Console → 启用 **YouTube Data API v3**。
2. OAuth 同意屏：External；发布状态 Testing 时把频道 Google 账号加进 **Test users**。
3. **创建 OAuth 客户端 → 应用类型必须选「桌面应用 / Desktop app」**。
   「Web 应用」类型不允许 `http://127.0.0.1` loopback，授权页报 `redirect_uri_mismatch`。
4. 下载 JSON，存为 `secrets/youtube_client_secret.json`（已被 .gitignore 忽略）。
5. 授权一次（浏览器打开，点同意）：

   ```bash
   python3 scripts/youtube_auth.py          # 交互授权，缓存到 secrets/youtube_token.json
   python3 scripts/youtube_auth.py probe    # 验证 token 和 scope
   ```

## 单条上传

```bash
python3 scripts/platforms/youtube.py artifacts/derivatives/out/samurai_a.mp4 \
  --title "侍｜AI 生成短片 Samurai AI Film" \
  --description "武士主题 AI 生成短片。#AI短片 #AIGC" \
  --tags samurai ai-film ai-shorts aigc \
  --privacy public --audio-language zh --notify-subscribers
```

完整字段（CLI 默认值）：`categoryId=22`（People & Blogs）、`defaultLanguage=zh-CN`、
`defaultAudioLanguage` 用 `--audio-language` 指定、标准 YouTube 许可、公开观看计数、
`selfDeclaredMadeForKids=false`、`embeddable=true`；`--publish-at` 支持定时发布。
默认 `--privacy private`，不通知订阅者；公开发布需显式 `--privacy public`。

> **AI 合成内容声明**：Data API v3 不提供"altered/synthetic content"开关，上传后必须去
> YouTube Studio → 视频详情 → "合成内容"勾选披露，否则可能被平台限流或判定违规。

## 网络：Google IP 黑洞（已内置 Happy Eyeballs）

本机环境实测：`www.googleapis.com` 轮询返回 16 个 IP，其中部分（如 172.217.118.4）
TCP 握手直接挂死，标准库 `gethostbyname` 只取一个地址会导致上传永久挂起（曾卡 7 分钟）。
`youtube_auth.robust_opener()` 对所有解析地址并发竞速 TCP+TLS，谁先通信用谁，
实测 init 从 66s（串行回退）降到 ~1s，整条上传 4s 完成。

## 批量（复用 publish_queue.json）

```bash
ONLY_PLATFORMS=youtube LIMIT_PER_PLATFORM=1 \
MX_YT_PRIVACY=private MX_YT_CATEGORY=22 python3 batch_publish.py
```

- YouTube 是 opt-in（不在 DEFAULT_PLATFORMS），只能用 `ONLY_PLATFORMS=youtube` 触发。
- 纯 API 批次自动跳过 Chrome 启动。
- 结果按其他平台相同的契约写回 queue 的 `published.youtube`，重试/去重走 `needs_publish`。

## 常见错误

- `youtubeSignupRequired: Unauthorized` — 授权账号还没有 YouTube 频道，去
  https://www.youtube.com/create_channel 创建；频道是账号级资源，**已有 token 立即生效**。
- 授权页只显示"请求详情"被拦截 — 同意屏 Testing 状态下账号没加进 Test users。

## 配额

默认 10,000 units/天，一次 `videos.insert` ≈ 1,600 units → **每天最多约 6 条**。
`quotaExceeded` 会被当作永久失败（`fail`），不会反复重试烧配额，次日再跑即可。
