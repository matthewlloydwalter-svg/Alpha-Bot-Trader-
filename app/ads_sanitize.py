"""
ads_sanitize.py — allowlist AdSense / trusted ad markup; block arbitrary XSS.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from html import escape

_ALLOWED_TAGS = {"div", "ins", "span", "script", "amp-ad", "iframe"}
_ALLOWED_ATTRS = {
    "class", "id", "style", "data-ad-client", "data-ad-slot", "data-ad-format",
    "data-full-width-responsive", "data-ad-layout", "data-ad-layout-key",
    "src", "async", "crossorigin", "width", "height", "frameborder",
    "scrolling", "referrerpolicy", "title", "aria-label",
}
_TRUSTED_SCRIPT_HOSTS = (
    "pagead2.googlesyndication.com",
    "www.googletagservices.com",
    "securepubads.g.doubleclick.net",
    "www.googleadservices.com",
    "ep2.adtrafficquality.google",
)
_TRUSTED_IFRAME_HOSTS = (
    "googleads.g.doubleclick.net",
    "tpc.googlesyndication.com",
    "www.google.com",
)


def _host_ok(src: str, hosts: tuple[str, ...]) -> bool:
    if not src:
        return False
    s = src.strip().lower()
    if s.startswith("//"):
        s = "https:" + s
    if not (s.startswith("https://") or s.startswith("http://")):
        return False
    try:
        from urllib.parse import urlparse
        host = urlparse(s).hostname or ""
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in hosts)


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip_depth = 0
        self._in_script = False
        self._script_buf: list[str] = []
        self._script_attrs: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skip_depth:
            self._skip_depth += 1
            return
        if tag not in _ALLOWED_TAGS:
            self._skip_depth = 1
            return
        cleaned = []
        for k, v in attrs:
            k = (k or "").lower()
            if k.startswith("on"):
                continue
            if k not in _ALLOWED_ATTRS:
                continue
            cleaned.append((k, v))
        if tag == "script":
            src = next((v for k, v in cleaned if k == "src"), None)
            if src and not _host_ok(src, _TRUSTED_SCRIPT_HOSTS):
                self._skip_depth = 1
                return
            # Inline scripts only allowed if they look like adsbygoogle push
            self._in_script = True
            self._script_buf = []
            self._script_attrs = cleaned
            return
        if tag == "iframe":
            src = next((v for k, v in cleaned if k == "src"), None)
            if not src or not _host_ok(src, _TRUSTED_IFRAME_HOSTS):
                self._skip_depth = 1
                return
        attr_s = "".join(
            f' {k}="{escape(v, quote=True)}"' if v is not None else f" {k}"
            for k, v in cleaned
        )
        self.out.append(f"<{tag}{attr_s}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "script" and self._in_script:
            body = "".join(self._script_buf)
            src = next((v for k, v in self._script_attrs if k == "src"), None)
            # Allow empty or adsbygoogle bootstrap inline scripts only
            ok_inline = (not src) and (
                not body.strip()
                or "(adsbygoogle" in body
                or "adsbygoogle" in body
            )
            if src or ok_inline:
                attr_s = "".join(
                    f' {k}="{escape(v, quote=True)}"' if v is not None else f" {k}"
                    for k, v in self._script_attrs
                )
                self.out.append(f"<script{attr_s}>{body}</script>")
            self._in_script = False
            self._script_buf = []
            self._script_attrs = []
            return
        if tag in _ALLOWED_TAGS and tag != "script":
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_script:
            self._script_buf.append(data)
            return
        self.out.append(escape(data))

    def handle_entityref(self, name):
        if self._skip_depth or self._in_script:
            if self._in_script:
                self._script_buf.append(f"&{name};")
            return
        self.out.append(f"&{name};")

    def handle_charref(self, name):
        if self._skip_depth or self._in_script:
            if self._in_script:
                self._script_buf.append(f"&#{name};")
            return
        self.out.append(f"&#{name};")


def sanitize_ad_snippet(raw: str) -> str:
    """Return a sanitized ad snippet safe for sidebar injection."""
    if not raw or not raw.strip():
        return ""
    # Reject obvious javascript: URLs
    if re.search(r"javascript\s*:", raw, re.I):
        raise ValueError("Ad snippet contains disallowed javascript: URLs.")
    parser = _Sanitizer()
    try:
        parser.feed(raw)
        parser.close()
    except Exception as e:
        raise ValueError(f"Could not parse ad snippet: {e}") from e
    return "".join(parser.out).strip()
