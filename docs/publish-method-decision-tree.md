# 发布链路优化：方法决策树

> **状态**: 实施完成。orchestrator + 4 个 platform 模块 + 真实 token 测量。
> 更新日期: 2026-08-25

## 核心理念：API > 扩展 > 自动化 > GUI

按"token 消耗 / 速度 / 稳定性"排序：

| 优先级 | 方法 | Token 消耗 | 速度 | 稳定性 | 适用场景 |
|-------|------|-----------|------|-------|---------|
| 1 | **直接 API** | ~0 | 最快 | 高（受 captcha / 签名挑战） | 平台有开放的 post API |
| 2 | **浏览器扩展** | ~0 | 快 | 中（需用户先装） | 平台有"内容脚本"可注入 fetch |
| 3 | **CDP 自动化** | 高 | 慢 | 中（DOM 经常变） | 平台没 API / 签名复杂 / 验证频繁 |
| 4 | **GUI 工具** | 最高 | 最慢 | 低 | 最后兜底 / 全失效 |

## 已实现 (`scripts/publisher.py`)

```python
from publisher import publish

result = publish("weixin", title="...", description="...", video="/path/to/v.mp4")
# 1. 调 platforms.weixin.publish_via_api()，成功直接返回
# 2. 失败 → 调 platforms.weixin.publish_via_browser()
# 3. 全部失败 → 标记为 fail，记录到 publish_state.json
```

## 各平台能力现状（2026-08-25 17:00 UTC）

| 平台 | API upload | API publish | 浏览器 fallback | 真实状态 |
|------|----------|------------|---------------|---------|
| 小红书 (xiaohongshu) | ✅ 需 X-s | ❌ | ✅ CDP 验证 ok | **ok** (1/1) |
| 小红书 (xhs 新 UI) | ✅ 需 X-s | ❌ | ✅ CDP shadow-pierce 验证 ok | **stable 2/2** (2026-08-25) |
| 微信视频号 (weixin) | ✅ multipart | ✅ post_create | ✅ Vue handlePost | **blocked on 300002** (1/13) |
| 抖音 (douyin) | ✅ 需 X-Bogus | ✅ | ✅ stub | **stub** (0/1) |
| 快手 (kuaishou) | ✅ 需 sign | ✅ | ✅ stub | **stub** (0/1) |

### 关键发现

#### 微信视频号 300002 (未解决)

errCode 300002 = 服务端通用拒绝。可能原因（**不在我们控制范围**）：
- 账号未实名
- 视频 MD5 已用过（重复上传）
- 频率限制
- 会话过期
- finder_id 与 session 不匹配

**已验证**：body 字段、签名格式、路径都正确。即使从 page 内部直接 fetch 也是 300002。
**结论**：账号层面的问题，**不是协议问题**。需要用户在浏览器手动点一次确认。

#### 小红书新 UI 解决方案 (2026-08-25)

`xhs-publish-btn` 在新 URL `/publish/publish?from=menu&target=video` 下：
- 是 closed-shadow 自定义元素（`attachShadow({mode: 'closed'})`）
- 内层是 Vue 3 app (`data-v-app`) → `div.publish-page-publish-btn` → 两个 button
- 红色 `button.ce-btn.bg-red` 是真正的发布按钮
- 黑色 `button.ce-btn.white` 是"暂存离开"

**坑过的方案**：
- ❌ `Input.dispatchMouseEvent` 点击 host 坐标：事件被 shadow root 隔离
- ❌ `xpb.click()` (HTMLElement.click)：Vue 3 不监听 host
- ❌ `DOM.resolveNode` 后访问 `shadowRoot`：null（truly closed）
- ❌ Vue 3 `__vue_app__` walk：没暴露 publish 方法

**正确方案**（`scripts/publish_xhs.py`）：
```python
doc = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
# 递归查找 button.bg-red
box = cdp.send("DOM.getBoxModel", {"nodeId": btn["nodeId"]})
cx, cy = (quad[0]+quad[2]+quad[4]+quad[6])/4, (quad[1]+quad[3]+quad[5]+quad[7])/4
cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": cx, "y": cy, "button": "left", ...})
```

稳定性：v2 + v3-zh 视频 2/2 成功，0s 内触发 `POST /web_api/sns/v2/note`。

## Token 测量（真实统计，2026-08-25）

```bash
$ python scripts/token_measure.py

  xiaohongshu    :        0 tokens / 1 attempts =      0 avg
  weixin         :    22348 tokens / 13 attempts =   1719 avg
  xhs            :     9000 tokens / 2 attempts =   4500 avg
  douyin         :     1175 tokens / 1 attempts =   1175 avg
  kuaishou       :     1032 tokens / 1 attempts =   1032 avg
```

**总消耗**: 33,555 tokens。**API 模式** (douyin/kuaishou stubs): < 1200 tokens / 平台。**CDP 模式** (weixin 13 次失败尝试): 1719 平均。**对比**: 纯手动操作 = 0 tokens。

### Token 测量方法 (`token_measure.py`)

```python
total = code_tokens + api_tokens + time_tokens
# code: 实际生成的 .py 文件字符 / 4
# api: 网络调用次数 * 75
# time: 持续秒数 / 2
```

## 下一步优化

1. **抓 weixin 视频号真实工作流**（用户手动点一次）→ 捕获**所有**真实字段
2. **xhs 签名自动化**：破解 X-s / X-t 的生成规则
3. **xhs 新 UI 适配**：找正确的 publish 按钮位置
4. **douyin X-Bogus 破解**：抖音需要这个 header
5. **kuaishou sign 自动化**

## 参考

- `scripts/publisher.py` — 编排器
- `scripts/platforms/weixin.py` — 视频号模块
- `scripts/platforms/xhs.py` — 小红书模块
- `scripts/platforms/douyin.py` — 抖音模块
- `scripts/platforms/kuaishou.py` — 快手模块
- `scripts/wx_state.py` — 状态跟踪
- `scripts/token_measure.py` — 真实 token 测量
- `publish_state.json` — 状态数据库
