"""
Weixin 视频号 publisher.

Known API endpoints (from reverse-engineering the web app):
- POST /micro/content/cgi-bin/mmfinderassistant-bin/post/check_finder_comm_face  (precheck)
- POST /micro/content/cgi-bin/mmfinderassistant-bin/post/post_create  (publish)
- POST /micro/content/cgi-bin/mmfinderassistant-bin/upload/...  (video upload, multi-step)

Common cause: errCode 300002 "request failed" is a generic server-side block.
- Account may need real-name verification
- Session may be expired
- Video may be a duplicate (md5 already used)
- Account may have hit rate limits

When API fails with 300002, fall back to browser automation.
"""
import json
import time
import sys
import os
from pathlib import Path

# Allow importing publisher.py from sibling dir
sys.path.insert(0, str(Path(__file__).parent.parent))
from publisher import PublishResult


# Known post_create URL pattern
POST_CREATE_URL = "https://channels.weixin.qq.com/micro/content/cgi-bin/mmfinderassistant-bin/post/post_create"
PRECHECK_URL = "https://channels.weixin.qq.com/micro/content/cgi-bin/mmfinderassistant-bin/post/check_finder_comm_face"
AID = "12aeddd4-2ea0-4c10-8e5c-0f8cb0b61373"  # from observed network traffic
PAGE_URL = "https://channels.weixin.qq.com/micro/content/post/create"


def publish_via_api(*, title, description, video, topics=None, location=None, cookies=None, **kwargs) -> PublishResult:
    """
    Direct API publish. Requires pre-uploaded video (media already on CDN).
    cookies: dict of session cookies (or None to use browser session)
    Returns PublishResult with status ok/partial/fail.
    """
    # Step 1: Need media already on CDN - get it from browser session or passed in
    if "media" not in kwargs:
        return PublishResult("weixin", "fail", method="api", error="no_media_uploaded_yet")

    media = kwargs["media"]
    cps = kwargs.get("cps", {})
    client_id = kwargs.get("client_id")
    finder_id = kwargs.get("finder_id", "")

    body = {
        "objectType": 0,
        "longitude": location.get("longitude", 0) if location else 0,
        "latitude": location.get("latitude", 0) if location else 0,
        "feedLongitude": location.get("longitude", 0) if location else 0,
        "feedLatitude": location.get("latitude", 0) if location else 0,
        "originalFlag": 0,
        "topics": topics or [],
        "isFullPost": 1,
        "handleFlag": 2,
        "videoClipTaskId": cps.get("clipTicket", {}).get("draftId", ""),
        "traceInfo": cps.get("traceInfo", {}),
        "objectDesc": {
            "mpTitle": title,
            "description": description,
            "extReading": {"link": "", "title": ""},
            "mediaType": 4,
            "location": location or {},
            "topic": kwargs.get("topic_xml", {}),
            "event": {},
            "mentionedUser": [],
            "media": media,
            "shortTitle": [{"shortTitle": title}],
            "member": {"link": "", "title": ""},
        },
        "postFlag": 0,
        "mode": 1,
        "clientid": client_id or str(__import__("uuid").uuid4()),
        "timestamp": str(int(time.time() * 1000)),
        "_log_finder_uin": "",
        "_log_finder_id": finder_id,
        "rawKeyBuff": "",
        "pluginSessionId": None,
        "scene": 7,
        "reqScene": 7,
    }

    import secrets
    rid = f"{secrets.token_hex(4)}-{secrets.token_hex(4)}"
    url = f"{POST_CREATE_URL}?_aid={AID}&_rid={rid}&_pageUrl={__import__('urllib.parse').quote(PAGE_URL)}"

    # Use requests if available, else urllib
    try:
        import requests
        headers = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
        resp = requests.post(url, json=body, headers=headers, cookies=cookies or {}, timeout=30)
        result = resp.json()
    except ImportError:
        import urllib.request
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())

    if result.get("errCode") == 0:
        return PublishResult("weixin", "ok", method="api", data=result.get("data"))
    return PublishResult("weixin", "fail", method="api", error=f"errCode={result.get('errCode')} errMsg={result.get('errMsg')}", body_sent=body)


def publish_via_browser(*, title, description, video, topics=None, location=None, **kwargs) -> PublishResult:
    """
    Weixin 视频号: title max 14 chars (server enforces).
    Browser automation via Playwright + CDP.
    - Uploads video via DOM file input
    - Fills form fields
    - Triggers publish via Vue's handlePost
    - Captures response
    """
    title = (title or "")[:14]  # weixin 视频号 title cap (server enforces)
    from playwright.sync_api import sync_playwright

    CDP_URL = kwargs.get("cdp_url", "http://127.0.0.1:9222")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL, timeout=15000)
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if "channels.weixin" in pg.url), None)
        if not page:
            return PublishResult("weixin", "fail", method="cdp", error="no_weixin_tab_open")

        # Only navigate if not already on the create page
        if "platform/post/create" not in page.url:
            page.bring_to_front()
            page.set_viewport_size({"width": 1440, "height": 900})
            try:
                page.goto("https://channels.weixin.qq.com/platform/post/create", wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                return PublishResult("weixin", "fail", method="cdp", error=f"nav: {e}")
            time.sleep(10)

        # Use CDP for the wujie iframe
        cdp = ctx.new_cdp_session(page)
        cdp.send("Runtime.enable")
        cdp.send("DOM.enable")
        cdp.send("Page.enable")
        tree = cdp.send("Page.getFrameTree")
        frames = tree.get("frameTree", tree).get("childFrames", [])
        if not frames:
            return PublishResult("weixin", "fail", method="cdp", error="no_wujie_frame")
        micro_frame_id = frames[0].get("frame", {}).get("id")

        def micro_eval(expr, await_promise=False):
            return cdp.send("Runtime.evaluate", {
                "expression": f"(() => {{ return ({expr}); }})()",
                "returnByValue": True,
                "frameId": micro_frame_id,
                "awaitPromise": await_promise,
            }).get("result", {}).get("value")

        # Wait for app
        for _ in range(15):
            if micro_eval("!!document.querySelector('.finder-page')"):
                break
            time.sleep(1)

        # Upload via CDP DOM.performSearch (handles shadow DOM)
        try:
            search = cdp.send("DOM.performSearch", {"query": "input[type=file]", "includeUserAgentShadowDOM": True})
            count = search.get("resultCount", 0)
            if count == 0:
                return PublishResult("weixin", "fail", method="cdp", error="no_file_inputs_in_dom")
            results = cdp.send("DOM.getSearchResults", {"searchId": search["searchId"], "fromIndex": 0, "toIndex": min(5, count)})
            file_nid = results.get("nodeIds", [None])[0]
        except Exception as e:
            return PublishResult("weixin", "fail", method="cdp", error=f"dom_search: {e}")
        if not file_nid:
            return PublishResult("weixin", "fail", method="cdp", error="no_file_input")
        cdp.send("DOM.setFileInputFiles", {"nodeId": file_nid, "files": [video]})

        # Wait for upload
        for _ in range(60):
            state = micro_eval("(() => ({has: document.body.innerText.includes('个人主页卡片') || document.body.innerText.includes('封面预览')}))()")
            if state and state.get("has"):
                break
            time.sleep(2)

        # Fill form
        micro_eval(f"""(() => {{
            const inp = document.querySelector('input[placeholder*="短标题"]');
            if (inp) {{
                inp.focus();
                inp.select();
                document.execCommand('insertText', false, '{title}');
                inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
            }}
            const ce = document.querySelector('.input-editor');
            if (ce) {{
                ce.focus();
                document.execCommand('selectAll', false);
                document.execCommand('delete', false);
                document.execCommand('insertText', false, '{description}');
            }}
        }})()""")
        time.sleep(2)

        # Set mpTitle on Vue originState
        micro_eval(f"""(() => {{
            const app = document.querySelector('.finder-page');
            const root = app.__vue__;
            const seen = new Set();
            function findSU(node) {{
                if (!node || seen.has(node)) return null;
                seen.add(node);
                if (node.$options?.name === 'sU' && typeof node.handlePost === 'function') return node;
                if (node.$children) for (const c of node.$children) {{ const r = findSU(c); if (r) return r; }}
                return null;
            }}
            const su = findSU(root);
            if (!su) return 'no_su';
            const te = su.$children[0];
            const l = te.$refs.postCreate;
            l.$set(l.originState.postObjDesc, 'mpTitle', '{title}');
            return 'set';
        }})()""")

        # Hook response
        micro_eval("""(() => {
            window.__lastResp = null;
            const os = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.send = function(b) {
                if (this.__u && this.__u.includes('post_create')) {
                    this.addEventListener('readystatechange', () => {
                        if (this.readyState === 4) {
                            window.__lastResp = this.responseText;
                        }
                    });
                }
                return os.apply(this, arguments);
            };
        })()""")

        # Trigger
        micro_eval("""(() => {
            const app = document.querySelector('.finder-page');
            const root = app.__vue__;
            const seen = new Set();
            function findSU(node) {
                if (!node || seen.has(node)) return null;
                seen.add(node);
                if (node.$options?.name === 'sU' && typeof node.handlePost === 'function') return node;
                if (node.$children) for (const c of node.$children) { const r = findSU(c); if (r) return r; }
                return null;
            }
            const su = findSU(root);
            if (su) su.handlePost();
        })()""")

        time.sleep(20)
        resp = micro_eval("() => window.__lastResp")

        if resp and '"errCode":0' in resp:
            return PublishResult("weixin", "ok", method="cdp", response=resp)
        if resp and '300002' in resp:
            return PublishResult("weixin", "partial", method="cdp", error="errCode_300002_server_block", response=resp)
        return PublishResult("weixin", "fail", method="cdp", error="no_response_or_unknown", response=str(resp)[:200])
