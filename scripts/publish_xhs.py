"""
XHS 小红书 publisher - WORKING version (2026-08-25).

Solution: xhs-publish-btn is a closed-shadow custom element.
- `Input.dispatchMouseEvent` clicks on the host element don't reach the inner button
- `element.click()` doesn't trigger Vue's @click (Vue 3 listens on inner button)
- `DOM.resolveNode` returns null shadow root (truly closed)
- **SOLUTION**: Use CDP's `DOM.getDocument({depth: -1, pierce: true})` to access
  the closed shadow root. Find the inner `<button class="ce-btn bg-red">`,
  get its center via `DOM.getBoxModel`, then click the real coordinates.

Verified: 2/2 stability test passed (v2 + v3-zh videos).

Workflow:
1. Navigate to publish page
2. Set file input (wait for upload to complete)
3. Set title via input fill
4. Set description via contenteditable innerHTML + input event
5. CDP: DOM.getDocument({pierce: true}) → find button.bg-red → DOM.getBoxModel
6. CDP: Input.dispatchMouseEvent at the inner button's actual center
7. Wait for "发布成功"
"""
import json
import time
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from publisher import PublishResult

CDP_URL = "http://127.0.0.1:9222"
PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?from=menu&target=video"


def _find_publish_button_node(cdp):
    """Use CDP to find the inner publish button inside xhs-publish-btn's closed shadow root."""
    doc = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})

    def walk(node):
        if node.get("nodeName", "").lower() == "button":
            attrs = dict(zip(node.get("attributes", [])[::2], node.get("attributes", [])[1::2]))
            if "bg-red" in (attrs.get("class", "") or ""):
                return node
        for sr in node.get("shadowRoots", []):
            r = walk(sr)
            if r:
                return r
        for child in node.get("children", []):
            r = walk(child)
            if r:
                return r
        return None

    return walk(doc["root"])


def _click_publish_button(cdp, node):
    """Get the inner button's bounding box center, dispatch real mouse events there."""
    box = cdp.send("DOM.getBoxModel", {"nodeId": node["nodeId"]})
    quad = box["model"]["content"]
    cx = (quad[0] + quad[2] + quad[4] + quad[6]) / 4
    cy = (quad[1] + quad[3] + quad[5] + quad[7]) / 4
    cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": cx, "y": cy, "button": "none"})
    cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": cx, "y": cy, "button": "left", "buttons": 1, "clickCount": 1})
    cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": cx, "y": cy, "button": "left", "buttons": 0, "clickCount": 1})
    return cx, cy


def publish(*, title, description, video, topics=None, **kwargs) -> PublishResult:
    """Publish to XHS via CDP pierce + inner shadow button click."""
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL, timeout=15000)
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if "xiaohongshu" in pg.url), None)
        if not page:
            return PublishResult("xhs", "fail", error="no_browser_tab")
        page.bring_to_front()
        page.set_viewport_size({"width": 1280, "height": 800})
        page.on("dialog", lambda d: d.accept())

        if "publish/publish" not in page.url:
            page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=15000)
        time.sleep(3)

        # Set file
        file_inputs = page.locator('input[type=file]').all()
        if not file_inputs:
            return PublishResult("xhs", "fail", error="no_file_input")
        file_inputs[0].set_input_files(video, timeout=20000)

        # Wait for upload
        for i in range(45):
            time.sleep(1)
            body = page.evaluate("() => document.body.innerText")
            pcts = re.findall(r'\d+%', body)
            if '重新上传' in body and not pcts and i > 5:
                break

        # Set title
        page.locator('input[placeholder*="标题"]').first.fill(title, timeout=5000)

        # Set description
        page.evaluate("""(function() {
            const ce = document.querySelector('[contenteditable=true]');
            if (ce) {
                ce.focus();
                ce.innerHTML = arguments[0];
                ce.dispatchEvent(new Event('input', {bubbles: true}));
            }
        })()""", description)
        time.sleep(1)

        # Find and click the inner publish button
        cdp = ctx.new_cdp_session(page)
        cdp.send("DOM.enable")
        pub_btn = _find_publish_button_node(cdp)
        if not pub_btn:
            return PublishResult("xhs", "fail", error="no_inner_publish_button")
        cx, cy = _click_publish_button(cdp, pub_btn)

        # Wait for success
        for i in range(15):
            time.sleep(2)
            body = page.evaluate("() => document.body.innerText")
            if '发布成功' in body or '审核中' in body:
                return PublishResult("xhs", "ok", method="cdp-shadow-click", url=page.url, click_coords=(cx, cy))
            if '发布失败' in body or '提交失败' in body:
                return PublishResult("xhs", "fail", method="cdp-shadow-click", error="page_reported_fail", body=body[:300])

        return PublishResult("xhs", "fail", method="cdp-shadow-click", error="timeout_no_success")
