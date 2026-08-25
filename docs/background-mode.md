# 真后台模式（CDP 直连）

> **状态**: 未实现。架构已就位（webbridge_client.py 可扩展），等用户准备好重启 Chrome。

## 为什么 webbridge 默认不是真后台

Kimi WebBridge 的 `evaluate / click / fill / snapshot / upload / screenshot` 都只跑在**用户的
前台 tab**。`find_tab` 切换 session-side "current" 指针不影响 evaluate 行为。

要想让脚本真正在 4 个**后台** tab 上并行操作，必须绕开 webbridge，直接连 Chrome DevTools
Protocol（CDP）。

## 启用方式

### 1. 重启 Chrome 加 debug port

```bash
# 完全退出 Chrome（cmd+Q），然后：
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --remote-debugging-port=9222 \
    --remote-debugging-address=0.0.0.0 \
    > ~/logs/chrome-debug.log 2>&1 &

# webbridge 扩展会自动 re-attach（已安装的扩展）
```

### 2. 验证 9222 端口

```bash
curl http://127.0.0.1:9222/json/version
# 应输出 {"Browser":"Chrome/...","Protocol-Version":"1.3",...}
```

### 3. 用 Playwright `connect_over_cdp`

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    # 现有 pages（包含 4 个 publish tab）
    for ctx in browser.contexts:
        for page in ctx.pages:
            if "douyin.com" in page.url:
                page.click("input[type=file]")
                # ... 真后台操作
```

### 4. 集成到 webbridge_client.py

加一个 `WebBridgeCdp` 类，把 `evaluate / click / upload` 通过 Playwright 转给指定 `page`，
而不是 webbridge daemon。Publisher 可以接受 backend=("webbridge"|"playwright")。

## 限制

- **必须重启 Chrome**：debug port 不能后加
- **必须让 webbridge 也连得上**：扩展重新注入（一般自动）
- **macOS 沙盒**：Chrome from `/Applications/...` 可能需要 `sudo` 或重新签名才能加
  `--remote-debugging-port`。如果失败，备选：用 ChromeDriver 独立启一个 user-data-dir，
  但 cookie 不共享 — 不实用。

## 优先级

前台协作模式（默认）已能完成需求（每个平台切 1 次 tab）。除非用户每天发布量很大（>5 平台
轮转 / 单次），否则没必要为后台模式重启 Chrome。
