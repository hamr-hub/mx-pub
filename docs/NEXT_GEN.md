# mx-pub 自愈与自学习能力 (2026-09)

mx-pub 在原有的"信号驱动、稳定驱动"基础上，新增了一套**自愈 + 自学习 +
扩展**层，目标是实现用户的目标：

> 让 AI 替代人去发布作品，无论什么异常都能发布成功；AI 自己学习最快最优的
> 发布方法；避免封号；预留扩展接口，以后所有视频内容发布都能用这个 mx 发布器。

## 新增模块

```
scripts/
├── core/
│   ├── errors.py        # 错误分类 + 推荐动作
│   ├── recovery.py      # 自动恢复（刷新、重登、冷却）
│   ├── health.py        # 发布前健康检查（cookie、captcha、风控横幅）
│   ├── behavior.py      # 类人行为模拟（防封号 jitter）
│   ├── hooks.py         # 扩展 hook 系统
│   └── pacing.py        # 自适应节奏（避免爆发式发布）
├── self_learn/
│   ├── stats.py         # 每次发布都写入 jsonl 事件流
│   ├── optimizer.py     # 基于历史的策略优化建议
│   └── risk.py          # 封号风险监视
├── extension/
│   ├── registry.py      # 平台/内容生成器注册表
│   └── _example_*.py    # 扩展插件示例
└── intelligence/
    └── report.py        # AI 洞察报告（可调 LLM，否则模板）

mxpub.py                  # 新一代统一入口
```

## 错误分类与自动恢复

`core/errors.py` 把每个失败原因分类到 7 类：

| ErrorClass | 推荐动作 | 例子 |
|---|---|---|
| `TRANSIENT` | RETRY | timeout、CDP 断连（abort_platform） |
| `RECOVERABLE` | REFRESH_PAGE / RELOGIN | cookie 过期、selector 漂移 |
| `CONTENT` | SKIP_VIDEO | 视频太短、版权、违规 |
| `ACCOUNT` | COOL_DOWN / ABORT_PLATFORM | 今日上限、账号异常 |
| `PLATFORM` | RETRY | 5xx、维护中 |
| `FATAL` | ABORT_BATCH | Chrome 找不到、selector 完全坏 |
| `UNKNOWN` | RETRY | 未识别 |

`core/recovery.py` 把推荐动作真正执行掉（页面刷新、重登、冷却等待）。

## 行为模拟（防封号）

`core/behavior.py` 在自动化流程中注入类人节奏：

- 表单字段间随机停顿（120–380ms）
- 上传完成后模拟"看预览"
- 发布按钮点击前的微抖动 + 鼠标微移
- 视频之间 8–30s 间隔（模拟人切换上下文）
- 可设置 `MX_HUMAN=0` 在 CI 模式关闭

## 自适应节奏

`core/pacing.py` 跟踪每个平台：

- **最小间隔**：默认 120s，成功率低时自动放大
- **爆发限制**：连续 5 个后强制休息 5 分钟
- **冷却**：失败率超阈值时自动进入冷却
- 状态持久化到 `pacing_state.json`，重启后继续

## 自学习统计

`self_learn/stats.py` 把每次尝试都写入 `publish_events.jsonl`：

- 时间、平台、视频、内容哈希、错误类、动作、严重度
- 提供 `success_rate()`、`best_hours()`、`worst_hours()`、`error_label_counts()`

## 策略优化器

`self_learn/optimizer.py` 根据历史给出建议：

- 哪个时段成功率最高
- 哪个时段要避开
- 该用什么节奏（间隔、爆发限制）
- 警告（成功率低、爆发失败等）

## 风险监视

`self_learn/risk.py` 检测封号前兆：

- 连续失败 ≥3 → warn，≥5 → critical
- 同分钟 3 次以上尝试 → 自动化爆发签名
- 同一错误类出现 ≥50% → 攻击性行为签名
- 多个高严重度失败 → 立即冷却

`batch_publish.py` 在跑批前会调用 `RiskMonitor.check()`，critical 级别
直接跳过该平台。

## AI 洞察报告

`intelligence/report.py` 拉取所有数据，生成结构化报告：

```bash
python3 mxpub.py insights
```

会写两份：

- `insights_report.md` — 人读
- `insights_report.json` — 机读

若设置了 `ANTHROPIC_API_KEY` 或 `OPENAI_API_KEY`，还会附上 AI 的 3–5 条
可执行建议。

## 扩展接口

### 注册新平台

```python
# scripts/extension/my_bilibili.py
from extension.registry import register_platform, PlatformSpec

register_platform(PlatformSpec(
    name="bilibili",
    display_name="哔哩哔哩",
    url_match=lambda u: "member.bilibili.com" in u,
    publish_url="https://member.bilibili.com/v2#/upload",
    module="platforms.bilibili",
    title_max=80, desc_max=2000, topics_max=10,
))
```

把真实的 `scripts/platforms/bilibili.py` 写好就行（实现
`publish_on_page(page, ...)`）。下次启动 `mxpub.py publish` 自动包含它。

### 自定义 Hook

```python
# scripts/extension/my_telemetry.py
from core.hooks import Hooks

def on_publish(**kw):
    print(f"telemetry: {kw['platform']} {kw['result'].status}")

Hooks.register("video:after_publish", on_publish)
```

可用 hook 名：

| Name | 触发时机 | kwargs |
|---|---|---|
| `batch:before` | 跑批开始 | queue, platforms |
| `batch:after` | 跑批结束 | queue, summary |
| `video:after_publish` | 每次发布尝试后 | video, platform, result |
| `video:on_error` | 每次失败后 | video, platform, classification |
| `platform:before_setup` | 进入平台前 | page, platform |
| `platform:after_setup` | 进入平台后 | page, platform, report |

## 快速上手

```bash
# 跑一次（用现有 batch_publish.py，自动启用 stats + pacing + risk）
python3 mxpub.py publish

# 只发抖音
python3 mxpub.py publish --platform douyin

# 忽略风控强制跑
python3 mxpub.py publish --force

# 健康检查
python3 mxpub.py health

# 统计概览
python3 mxpub.py stats

# AI 洞察报告
python3 mxpub.py insights

# 查看已注册的扩展
python3 mxpub.py extensions
```

## 环境变量

| 变量 | 作用 |
|---|---|
| `MX_HUMAN=0` | 关闭类人行为（CI / 调试） |
| `MX_MIN_INTERVAL_S=120` | 平台间最小间隔秒数 |
| `MX_BURST_LIMIT=5` | 连续发布上限 |
| `MX_BURST_REST_S=300` | 爆发后强制休息秒数 |
| `MX_FAIL_BACKOFF=0.6` | 触发退避的成功率阈值 |
| `MX_INSIGHTS_MODEL=...` | 洞察报告用的 LLM 模型 |
| `MX_PLUGINS_DIR=...` | 自定义插件目录 |

## 设计原则

1. **不破坏向后兼容** — `batch_publish.py` 仍然能独立跑，新增模块都是软依赖。
2. **失败不传染** — Hook 抛错被吞掉；recovery 失败降级为 RETRY；stats 写失败不阻塞发布。
3. **纯统计 + 可选 LLM** — 风控和洞察的核心都是统计，LLM 只是润色，永不阻塞关键路径。
4. **持久化学习** — 节奏和事件都落盘，重启后状态连续。

---

## v2 更新（2026-09-15）

本轮新增的关键能力：

### 智能重试路径（batch_publish.py）

原有的重试路径只做简单的 `is_transient_error()` 子串匹配。**现在**每次
发布失败都会：

1. 调用 `core.errors.classify()` 拿到完整分类（7 种 ErrorClass + 动作）
2. 调用 `core.recovery.perform_recovery()` 真正执行恢复动作：
   - `REFRESH_PAGE` → 重新加载发布页
   - `RELOGIN` → 跳到登录页（需要人工扫码）
   - `COOL_DOWN` → 自动等待（带 cap，防止单次等待过久）
   - `SKIP_VIDEO` → 跳过这条视频
   - `ABORT_PLATFORM` → 退出该平台
   - `ESCALATE` → 标记需要人工介入
3. 按 `cls.cooldown_s` 等待后再重试

### 内容安全门（intelligence/safety.py）

```bash
MX_SAFETY=1 python3 mxpub.py publish
```

启用后，`video:before_publish` hook 会对每条视频做：

- **启发式扫描**（默认）：医疗承诺、赌博、金融、政治、成人、暴力、误导等
- **LLM 评分**（可选）：调 Claude/OpenAI 给 0-1 分

不允许的内容被标记为 `blocked` 并跳过，不会浪费一次发布额度。

### 防封号 jitter（extension/_publish_jitter.py）

启动时自动加载，注册到三个 hook：

- `platform:after_setup` → 跳页后小停顿
- `video:before_publish` → 钩子占位
- `video:after_publish` → 视频间 4–12s 停顿（模拟人切换上下文）

`MX_HUMAN=0` 可关闭（CI/调试用），`MX_HUMAN_INTENSITY=subtle|normal|thorough` 控制强度。

### 智能临时错误判断（core/browser.py）

新增 `is_transient_error_smart()`，委托给 `core.errors.classify()`，更精准：
原来会被认为是"瞬时错误"而重试的"账号封禁"信号现在会立即停止。
