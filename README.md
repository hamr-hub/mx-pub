# mx-pub — 多平台发布工作流

把 `~/images/` 项目里生成的 AIGC 视频 / 图片，自动发布到 **抖音 / 快手 / 小红书 / 微信视频号**，
并每天采集 4 个平台的数据汇总成报告。

依赖：
- [Kimi WebBridge](https://github.com/MoonshotAI/kimi-webbridge) 浏览器扩展 + 本地 daemon
  （让 AI 控制真实 Chrome，复用你的登录态）
- `python3` (>= 3.10)
- 持续运行中的 Chrome
- 可选 [Playwright](https://playwright.dev/python/)（`pip install playwright && playwright install chromium`）—— 启用**后台模式**

---

## 后台 / 前台双模式（自动选择）

mx-pub 默认同时支持**真后台**（Playwright + CDP）和**前台协作**（webbridge）。
**默认行为**：`--backend auto` → 先试 CDP（Chrome debug port 9222），通了就用 Playwright
后台模式；不通则降级到 webbridge 前台协作（你需要手动切 4 次 tab）。

| 模式 | 触发条件 | 切 tab 次数 | 浏览器要求 |
|------|----------|-------------|----------|
| **后台 CDP** | Chrome 启了 `--remote-debugging-port=9222` | **0** | Chrome 重启 + debug port |
| **前台 webbridge** | 降级默认 | 4（每平台 1 次） | 任意 Chrome |

强制选择：

```bash
./scripts/publish_douyin.sh                                # auto（默认）
python scripts/publish_to_social.py --backend cdp-only ... # 强制后台，失败报错
python scripts/publish_to_social.py --backend webbridge ...# 强制前台
```

**手动启用后台模式**（一次性）：

```bash
# 1. 完全退出 Chrome（Cmd+Q），然后启动带 debug port
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --remote-debugging-port=9222 \
    --remote-allow-origins=* \
    > ~/logs/chrome-debug.log 2>&1 &

# 2. 验证
curl http://127.0.0.1:9222/json/version    # 应返回 Chrome 版本

# 3. 跑 mx-pub — 会自动用 Playwright 后台模式
./scripts/publish_douyin.sh --asset 20260819/minimax/video_1.mp4
```

如果不想重启 Chrome，保持 `--backend auto`（默认）就行，每次跑会切 4 次 tab。

---

## 目录结构

```
mx-pub/
├── scripts/
│   ├── webbridge_client.py          # 封装 POST http://127.0.0.1:10086/command
│   ├── platforms.json               # 4 平台 select / limits / analytics 配置
│   ├── publish_to_social.py         # 发布主入口（CLI: --platform X）
│   ├── stats.py                     # 数据采集入口
│   ├── report.py                    # 跨平台汇总报告
│   ├── publish_douyin.sh            # 单平台发布包装（每个平台一个）
│   ├── publish_kuaishou.sh
│   ├── publish_xiaohongshu.sh
│   ├── publish_weixin_channels.sh
│   ├── stats_douyin.sh              # 单平台数据采集包装
│   ├── stats_kuaishou.sh
│   ├── stats_xiaohongshu.sh
│   ├── stats_weixin_channels.sh
│   └── publish_state.db             # SQLite：发布历史 + 断点续传（运行后产生）
└── _benchmark/
    └── stats/
        ├── douyin_2026-08-25.json   # 各平台每日快照
        ├── kuaishou_2026-08-25.json
        ├── xiaohongshu_2026-08-25.json
        ├── weixin_channels_2026-08-25.json
        └── REPORT_2026-08-25.md    # 汇总报告
```

---

## 运行模式

**auto（默认）** —— 脚本先探测 `http://127.0.0.1:9222`（Chrome debug port）：

- **通了** → 用 Playwright `connect_over_cdp` 后台跑（无需切 tab）
- **不通** → 用 Kimi WebBridge，**每个平台让你切一次 tab**

**前台协作（降级路径）**：脚本每跳到一个新平台，会检测 Chrome 前台 tab 的 URL。
若不在目标平台的 publish 页，就打印

```
  ┌─ 需要你手动切 tab ─────────
  │  平台: 抖音 (douyin)
  │  把以下任一 URL 对应的 tab 切到前台:
  │    https://creator.douyin.com/creator-micro/content/upload
  │  切好后回到这里按回车继续…
  └────────────────────────────
```

你把对应 tab 切到前台，按回车，脚本继续。**整个流程每个平台只切一次 tab**，之后填表 / 上传 / 点击发布都不再打扰你。

**自动前台（headless / CI 友好）**：加 `--auto-foreground` 或 `MX_PUB_AUTO_FOREGROUND=1`，
脚本会直接把前台 tab 导航到 publish URL，**但会打断你当前看的内容**。

**真后台（CDP 直连）**：见 [docs/background-mode.md](docs/background-mode.md)。

---

## 一次性环境准备

### 1. 安装 Kimi WebBridge

```bash
# 浏览器扩展（Chrome Web Store 或 GitHub release）
# 本地 daemon:
curl -fsSL https://raw.githubusercontent.com/MoonshotAI/kimi-webbridge/main/install.sh | bash
kimi-webbridge run   # 后台启动，端口 10086
```

启动后 Chrome 工具栏会出现蓝色 kimi 图标，确保它能连上本地 daemon。

### 2. 在 Chrome 里打开 4 个平台的 publish tab

每个平台只需要打开任意一个 URL（登录后会停在 dashboard / publish 入口）：

| 平台 | URL |
|------|-----|
| 抖音 | https://creator.douyin.com/creator-micro/content/upload |
| 快手 | https://cp.kuaishou.com/article/publish/video |
| 小红书 | https://creator.xiaohongshu.com/new/home?source=official |
| 视频号 | https://channels.weixin.qq.com/platform |

让 4 个 tab 都登好录（cookie 在；脚本运行过程中不要退出 Chrome）。

### 3. 验证 daemon + 平台配置

```bash
python scripts/publish_to_social.py --health
# 应输出 running=True + 4 个平台都 OK（selectors 完整）
```

---

## 日常使用

### 发布创作内容到某平台

```bash
# 默认从 ~/images/ 扫描 YYYYMMDD 路径前缀的最新资产，每个平台发 1 个
./scripts/publish_douyin.sh

# 发最近 3 天产出的 5 个视频
./scripts/publish_douyin.sh --since 20260820 --limit 5

# 指定某个具体文件（多文件用逗号分隔）
./scripts/publish_douyin.sh --asset "20260819/minimax/video_1.mp4"

# dry-run：填表 + 截图到 _benchmark/，不点击发布（用于调 selectors）
./scripts/publish_douyin.sh --asset "20260819/minimax/video_1.mp4" --dry-run
```

### 采集 4 平台当日数据

```bash
./scripts/stats_douyin.sh        # 单平台
# 或
python scripts/stats.py --platform all     # 4 个平台全跑
```

输出：`_benchmark/stats/<platform>_<YYYY-MM-DD>.json`

### 汇总报告

```bash
python scripts/report.py                              # 当天
python scripts/report.py --date 2026-08-20            # 历史某天
```

输出：`_benchmark/stats/REPORT_<YYYY-MM-DD>.md`，包含
1. 发布尝试统计（来自 publish_state.db）
2. 平台数据快照（来自 stats JSON）
3. 单平台详情

---

## 断点续传与限速

`scripts/publish_state.db` 记录每个 `(platform, asset_sha1)` 的发布结果：

| status | 含义 |
|--------|------|
| `ok` | 发布成功 |
| `failed` | 失败（reason 写在 note 字段） |
| `skipped` | 已发过（按 sha1 跳过，避免重复） |
| `dry_run` | dry-run 模式（不点击发布） |

重新跑同一条命令，已 ok 的会自动跳过。要重置：删 publish_state.db 或改 `--force`（TODO）。

每个平台还有反垃圾策略（在 `platforms.json` 的 `anti_spam` 块）：

| 平台 | 最小发布间隔 | 单日上限 |
|------|-------------|----------|
| 抖音 | 120s | 30 |
| 快手 | 120s | 30 |
| 小红书 | 180s | 20 |
| 视频号 | 180s | 20 |

超限会被记录为 failed，附 note：*距上次 X 发布 Ns，< Ns 节流*。

---

## Cookie 过期

脚本会检测到 publish 页跳到 login，此时 `evaluate('location.href')` 返回的不是 publish URL。
你需要：

1. 在该平台的 tab 里手动登录（手机号 + 验证码，或扫码）
2. 登录后导航回 publish URL（脚本里给的 URL）
3. 回到终端按回车继续

如果脚本无法自动重试（已 hang），手动 ctrl-c 后重新跑：
```bash
./scripts/publish_douyin.sh  # 续跑
```

---

## Cookie / Selector 失效

平台改版后 selector 会失效。修复流程：

1. 打开对应平台的 publish tab，前台
2. 跑 dry-run：`./scripts/publish_douyin.sh --asset <some> --dry-run`
3. 截图保存到 `_benchmark/publish_<platform>_<name>_dryrun.png`
4. 看截图 + 浏览器 DevTools 的 selector 反查新 className
5. 更新 `scripts/platforms.json` 的 `selectors` 块
6. 重跑 dry-run 验证

---

## 每日自动发布（Cron / launchd）

把以下脚本加入 launchd（macOS）或 cron：

```bash
# 每天凌晨 4:00 跑 4 平台发布最新创作（仅 1 个视频/平台，避免触发限速）
0 4 * * * cd /Users/hyx/workspace/mx-pub && for p in douyin kuaishou xiaohongshu weixin_channels; do ./scripts/publish_${p}.sh --since $(date -v-1d +%Y%m%d) --limit 1 --yes >> logs/cron.log 2>&1; sleep 180; done
```

需要 Chrome **一直开着**（webbridge daemon 持续连接）。

---

## 与 `~/images/` 项目的集成

mx-pub 是独立项目（已推送 GH），但依赖 `~/images/` 的资产目录结构：
- 路径前缀必须是 `YYYYMMDD/provider/`（如 `20260819/minimax/video_1.mp4`）
- 文件扩展名 `.mp4 / .jpg / .png / .webp` 自动识别 video/image
- AIGC 产出的资产直接喂给 publish_to_social.py 即可

`run_daily_cron.sh`（在 ~/images/scripts/）跑完后，可追加：
```bash
# 在 daily_generate 成功后追加
python /Users/hyx/workspace/mx-pub/scripts/publish_to_social.py \
    --platform all --since $(date +%Y%m%d) --limit 1 --yes
```

---

## 安全 / 责任

- 本工具只在你**自己的账号**、**你自己的浏览器**、**你自己的登录态**下工作
- webbridge 不会把 cookie / 凭证上传到任何地方
- 发布内容由你 `/images/` 下的创作决定，脚本不联网拉素材
- 反垃圾参数是**保守值**（抖音 / 快手每天最多 30），不会刷屏
- 任何 selector 失效都立即 dry-run 验证，不盲点发布按钮

---

## License

Private (per-project). 不对外发布。
