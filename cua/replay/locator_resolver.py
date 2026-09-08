"""Locator resolution is implemented on the WebSurface itself (it needs
Playwright's Page). This module is kept as a documented seam for
alternative surfaces (Desktop/Vision) which would resolve very differently
but implement the same primary→fallbacks tier walk.
"""
