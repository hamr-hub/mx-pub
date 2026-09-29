"""Extension framework.

Anything that wants to plug into mx-pub without editing the core can register
itself here:

- New platform publishers (`register_platform`)
- Custom content generators (`register_content_provider`)
- New hooks (via core/hooks.py)
- New telemetry sinks
"""
