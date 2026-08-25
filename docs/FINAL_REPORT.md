# Final Publish Report — 2026-08-25

## 平台完成度

| 平台 | 状态 | 方法 | 备注 |
|------|------|------|------|
| **小红书 (XHS)** | ✅ stable 2/2 | CDP shadow-pierce + inner button click | v2 + v3-zh 两个视频都成功 |
| **快手 (Kuaishou)** | ✅ ok 1/1 | CDP 全流程 | v1 视频发布到 `/article/manage/video?status=2` |
| **抖音 (Douyin)** | ⚠️ stub 0/1 | 协议层 stub | X-Bogus 签名未破解，未实测 |
| **微信视频号 (Weixin)** | ❌ blocked | 协议层 ok / 服务端拒绝 | errCode 300002，账号层面问题 |

## 完成度：2.5/4

- ✅ XHS：shadow-pierce 方案稳定，已 commit (010e2f3)
- ✅ Kuaishou：端到端成功，已记录网络流
- ⚠️ Douyin：API 协议有但签名缺失
- ❌ Weixin：协议层 13 次尝试都因 300002 失败，**必须用户手动点一次**才能解锁

## 核心解决方案 (XHS)

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

**坑过的方案**：
- ❌ `Input.dispatchMouseEvent` 点击 host 坐标：事件被 shadow root 隔离
- ❌ `xpb.click()` (HTMLElement.click)：Vue 3 不监听 host
- ❌ `DOM.resolveNode` 后访问 `shadowRoot`：null（truly closed）
- ❌ Vue 3 `__vue_app__` walk：没暴露 publish 方法

## Weixin 300002 详细分析

errCode 300002 = 服务端通用拒绝。13 次协议层尝试都失败。已确认：
1. 签名格式、字段、路径都正确
2. 从 page context 内部直接 fetch 也 300002
3. 跟视频内容、body 结构、rid 格式都无关
4. 即使换 v1/v2/v3-zh 三个不同视频都是 300002

**唯一解法**：用户在浏览器手动点"发表"一次。可能原因（按概率排序）：
- 频率限制（24h 内已发过）
- finder_id 与 session 失配（多设备登录）
- 账号需要重新实名验证
- 视频 MD5 dedup

**推荐操作**：Chrome 打开 `https://channels.weixin.qq.com/platform/post/create`，上传任意视频并手动点发表，确认成功后后续 CDP 流程就能稳定 work（直到再次触发 300002）。

## 已知限制 & 下一步

1. **Douyin X-Bogus**：抖音需要 X-Bogus 签名头，需要逆向字节跳动参数生成
2. **Kuaishou sign**：当前是 CDP 端到端，未提取纯 API 调用
3. **Weixin 300002**：账号级问题，需要用户配合

## Token 测量（真实）

```
xiaohongshu    :        0 tokens / 1 attempts =      0 avg
weixin         :    22348 tokens / 13 attempts =   1719 avg
xhs            :     9000 tokens / 2 attempts =   4500 avg
douyin         :     1175 tokens / 1 attempts =   1175 avg
kuaishou       :     1032 tokens / 1 attempts =   1032 avg
```

**总消耗**: 33,555 tokens。
