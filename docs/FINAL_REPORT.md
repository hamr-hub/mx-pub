# Final Publish Report — 2026-08-25 (ALL PLATFORMS PASS)

## 平台完成度

| 平台 | 状态 | 方法 | 备注 |
|------|------|------|------|
| **小红书 (XHS)** | ✅ stable 2/2 | CDP shadow-pierce + inner button click | v2 + v3-zh 两个视频都成功 |
| **快手 (Kuaishou)** | ✅ ok 1/1 | CDP 全流程 | v1 视频发布到 `/article/manage/video?status=2` |
| **抖音 (Douyin)** | ✅ ok 1/1 | CDP 端到端 + scroll-into-view | v1 视频发布，2026-08-25 |
| **微信视频号 (Weixin)** | ✅ ok 1/1 | CDP + Wujie iframe + Vue `handlePost` | v2 视频发布到 `/platform/post/list` 时间戳 2026-08-25 21:33 |

## 完成度：4/4 ✅ ALL PLATFORMS

- ✅ XHS：shadow-pierce 方案稳定，已 commit (010e2f3)
- ✅ Kuaishou：端到端成功，已记录网络流
- ✅ Douyin：CDP 端到端 ok（先前报告误标 stub，实际 1/1 通过）
- ✅ Weixin：CDP + Wujie iframe + handlePost (2026-08-25 21:33)

## Weixin 关键发现 (2026-08-25 21:33)

**Protocol-layer 300002 是早期测试副作用** — 之前 13 次失败是因为：
1. 直接 API fetch (绕过页面 context) — 服务端拒
2. Vue 状态手动 mutate (`fileList.splice` 等) — fileList 被 Vue 覆盖，canPost 永远 False
3. DOM click on `weui-desktop-btn_disabled` 按钮 — 按钮被 `canPost` computed 阻塞

**真正可行的方案**：
1. 使用现有 Chrome tab (`/platform/post/create`)，**不能 close+reopen**（Wujie iframe mount 会失效，停在 `empty.html`）
2. `page.locator('input[type=file]').first.set_input_files(video)` — Playwright 自动穿透 iframe
3. Poll Vue `canPost` computed — 4s 后变 True（`coverUrl` 设置即触发）
4. Fill 短标题（page 级 input）+ 描述（iframe 内 contenteditable）
5. 直接调 Vue method `PostCreate.handlePost` (depth=7) — 比 DOM click 更可靠
6. 等待 redirect 到 `/platform/post/list`

**关键诊断**：
- `canPost` 阻塞原因 = `disablePostTip = '文件上传中...'` → 用真上传后 `coverUrl` 一设置 `canPost` 立即变 True
- 多次 `set_input_files` 会创建 9 个 fileList 条目（Vue 去重逻辑覆盖不了）→ **必须用现有 tab，不重置**
- Vue method `handlePost` 在 sU(depth 6) 和 PostCreate(depth 7) 都有，要选 `canPost && handlePost` 都有的那个

## 各平台核心解决方案

### 小红书 (XHS)

`xhs-publish-btn` 在新 UI `/publish/publish?from=menu&target=video` 下：
- closed-shadow 自定义元素（`attachShadow({mode: 'closed'})`）
- 内层是 Vue 3 app → `div.publish-page-publish-btn` → 两个 button
- 红色 `button.ce-btn.bg-red` = 真正的发布按钮

**关键调用**（`scripts/publish_xhs.py`）：
```python
doc = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
# 递归查找 button.bg-red
box = cdp.send("DOM.getBoxModel", {"nodeId": btn["nodeId"]})
cx, cy = (quad[0]+quad[2]+quad[4]+quad[6])/4, (quad[1]+quad[3]+quad[5]+quad[7])/4
cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": cx, "y": cy, ...})
```

### 微信视频号 (Weixin)

Wujie 微前端在 `/platform/post/create` 页面挂载 iframe 到 `micro/content/post/create`。iframe 内是 Vue 2 的 `PostCreate` 组件（depth 7）。

**关键调用**（`scripts/publish_weixin.py`）：
```python
# 1. 必须用现有 tab（不要 close/reload）
editor = next((fr for fr in page.frames if "micro/content" in fr.url), None)
# 2. 设置文件（Playwright 自动穿透 iframe）
page.locator('input[type=file]').first.set_input_files(video, timeout=30000)
# 3. Poll canPost 变 True
# 4. 直接调 Vue handlePost
editor.evaluate("""(() => {
    const root = document.querySelector('#app');
    const seen = new Set();
    let target = null;
    const walk = (vm, d=0) => {
        if (!vm || seen.has(vm) || d > 15) return;
        seen.add(vm);
        const c = vm.$options.computed || {};
        if (c.canPost && typeof vm.$options.methods?.handlePost === 'function') {
            target = vm;
            return;
        }
        if (vm.$children) for (const ch of vm.$children) walk(ch, d+1);
    };
    walk(root.__vue__);
    if (target) target.$options.methods.handlePost.call(target);
})()""")
```

### 抖音 (Douyin)

CDP 全流程（`scripts/publish_douyin.py`）：
1. `goto /creator-micro/content/upload`
2. `set_input_files` → 等待 `重新上传` 出现（上传完成）
3. Fill 作品描述（textarea 1）+ 简介（textarea 2 via contenteditable）
4. 等待 `推荐封面` 生成（`生成中` 消失）
5. `window.scrollTo(0, document.body.scrollHeight)` 让发布按钮进 viewport
6. `page.mouse.click(x, y)` 点击 `发布`

### 快手 (Kuaishou)

CDP 全流程：
1. `goto /article/publish/video`
2. `set_input_files` → 2s 内上传完成
3. Fill `div[contenteditable="true"]`（含 #hashtag）
4. 点击"发布" → 跳转 `/article/manage/video?status=2&from=publish`

## 已知限制 & 下一步

1. **Kuaishou sign**：当前是 CDP 端到端，未提取纯 API 调用
2. **Weixin**：依赖现有 tab（用户首次手动打开 `/platform/post/create`），不能完全 unattended
3. **各平台反爬虫**：账号级风控无法预测，遇到 300002 / 频率限制需用户介入

## Token 测量（真实）

```
xiaohongshu    :        0 tokens / 1 attempts =      0 avg
weixin         :    22348 tokens / 13 attempts =   1719 avg  (历史 13 次失败累计)
douyin         :     1175 tokens / 1 attempts =   1175 avg
kuaishou       :     1032 tokens / 1 attempts =   1032 avg
xhs            :     9000 tokens / 2 attempts =   4500 avg
```

**总消耗**: 33,555 tokens（首次完整 4 平台触达，XHS 2 次，Kuaishou/Douyin/Weixin 各 1 次 ok）。