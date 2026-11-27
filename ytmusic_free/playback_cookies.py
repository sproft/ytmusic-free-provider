"""Load one selected YouTube session without writing credentials to disk."""

from http.cookiejar import Cookie
from http.cookies import CookieError, SimpleCookie


def load_playback_cookies(ydl, cookie_header: str) -> None:
    """Scope the extractor's session to YouTube over HTTPS, never its media CDN."""
    if not cookie_header:
        return
    if "\r" in cookie_header or "\n" in cookie_header:
        raise ValueError("YouTube Cookie header must be a single line")
    cookies = SimpleCookie()
    try:
        cookies.load(cookie_header)
    except CookieError:
        raise ValueError("Invalid YouTube Cookie header") from None
    if not cookies or not cookies.get("__Secure-3PAPISID") or not cookies["__Secure-3PAPISID"].value:
        raise ValueError("YouTube Cookie header must contain __Secure-3PAPISID")
    for name, morsel in cookies.items():
        ydl.cookiejar.set_cookie(
            Cookie(
                version=0, name=name, value=morsel.value,
                port=None, port_specified=False,
                domain=".youtube.com", domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True, secure=True,
                expires=None, discard=True, comment=None, comment_url=None, rest={},
            )
        )
