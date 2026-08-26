"""Platform publisher modules.

Each module exposes:
- publish_via_api(title, description, video, topics, location, **kwargs) -> PublishResult
- publish_via_browser(title, description, video, topics, location, **kwargs) -> PublishResult

The orchestrator (publisher.py) tries API first, falls back to browser.
"""
