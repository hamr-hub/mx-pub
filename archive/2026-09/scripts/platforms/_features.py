"""Shared platform-feature helpers: 话题标签 / POI 位置 / 合集分类.

Each helper is a no-op if the platform doesn't expose the feature
or the input list is empty. Failures are swallowed to avoid breaking
the main publish flow.
"""
import time


def try_add_topics_douyin(page, topics: list) -> bool:
    """抖音: 描述下方有'添加话题' input. 触发 1 次,塞前 3 个话题."""
    if not topics:
        return False
    try:
        # 抖音 话题 input 通常 placeholder 包含 '话题' 或 class 包含 'topic'
        candidates = [
            'input[placeholder*="话题"]',
            'input[placeholder*="添加话题"]',
            'input.semi-input[placeholder*="话题"]',
            'input[class*="topic"]',
        ]
        for sel in candidates:
            loc = page.locator(sel).first
            if loc.count() > 0:
                for t in topics[:3]:
                    tag = t if t.startswith("#") else "#" + t
                    loc.fill(tag, timeout=5000)
                    time.sleep(0.3)
                return True
    except Exception:
        pass
    return False


def try_add_topics_kuaishou(page, topics: list) -> bool:
    """快手: 在描述末尾追加 '#tag' 触发智能话题识别."""
    if not topics:
        return False
    try:
        # 快手通常在 description 末尾追加 #tag 即可
        desc_sel = 'textarea[placeholder*="描述"]'
        loc = page.locator(desc_sel).first
        if loc.count() > 0:
            cur = loc.input_value() or ""
            tag_text = " " + " ".join(t if t.startswith("#") else "#" + t for t in topics[:5])
            if not any(t in cur for t in topics):
                loc.fill((cur + tag_text)[:500], timeout=5000)
            return True
    except Exception:
        pass
    return False


def try_add_poi(page, location: str) -> bool:
    """通用 POI 位置填入: 找 'location' / '位置' / '添加位置' input."""
    if not location:
        return False
    try:
        candidates = [
            'input[placeholder*="位置"]',
            'input[placeholder*="添加位置"]',
            'input[placeholder*="地点"]',
            'input[placeholder*="location"]',
        ]
        for sel in candidates:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.fill(location, timeout=5000)
                return True
    except Exception:
        pass
    return False


def try_add_collection_douyin(page, collection_name: str) -> bool:
    """抖音: 选合集. 合集 tab → search → pick first match."""
    if not collection_name:
        return False
    try:
        # 抖音 合集入口: 页面顶部 tab "合集"/"添加到合集"
        triggers = ['text="合集"', 'text="添加到合集"', 'button:has-text("合集")']
        for trig in triggers:
            btn = page.locator(trig).first
            if btn.count() > 0:
                btn.click(timeout=5000)
                time.sleep(1)
                # 找搜索框
                search = page.locator('input[placeholder*="搜索合集"]').first
                if search.count() > 0:
                    search.fill(collection_name, timeout=5000)
                    time.sleep(1)
                    # 点第一个结果
                    first = page.locator('[class*="collection-item"], li[class*="item"]').first
                    if first.count() > 0:
                        first.click(timeout=5000)
                        return True
    except Exception:
        pass
    return False
