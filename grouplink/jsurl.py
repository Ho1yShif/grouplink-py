"""A subset of the WHATWG URL parser, so the Python pages match the TypeScript ones.

`safe_url` and `favicon_url` in the TypeScript build run every href through the JS
`URL` constructor, which normalizes the string: a bare origin gains a trailing
slash, the host lowercases, a default port drops, and some characters get
percent-encoded. `urllib.parse` does none of that, so a port of those two functions
built on `urllib.parse` would commit a different `site/index.html` on the first run.

Only the special schemes are parsed. Everything else returns None, which is what
both callers want: `safe_url` collapses a non-http(s) string to "#", and
`favicon_url` returns "" for a base the JS constructor would have thrown on.
"""

from __future__ import annotations

from dataclasses import dataclass

SPECIAL_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21, "file": None}

# The WHATWG percent-encode sets. C0 controls and everything above U+007E are in
# every set; these are the extra ASCII characters each one adds.
_C0_OR_ABOVE = {chr(c) for c in range(0x20)} | {chr(0x7F)}
_FRAGMENT_SET = _C0_OR_ABOVE | set(' "<>`')
_QUERY_SET = _C0_OR_ABOVE | set(' "#<>')
_SPECIAL_QUERY_SET = _QUERY_SET | {"'"}
_PATH_SET = _QUERY_SET | set("?`{}")

# Stripped from both ends before parsing; removed everywhere inside.
_C0_AND_SPACE = "".join(chr(c) for c in range(0x21))
_TAB_AND_NEWLINE = str.maketrans("", "", "\t\n\r")


def _percent_encode(value: str, encode_set: set[str]) -> str:
    out = []
    for char in value:
        if char in encode_set or ord(char) > 0x7E:
            out.append("".join(f"%{byte:02X}" for byte in char.encode("utf-8")))
        else:
            out.append(char)
    return "".join(out)


def _parse_host(host: str) -> str | None:
    """Lowercase and IDNA-encode. None means the host is one the JS parser rejects."""
    if not host:
        return None
    if any(char in host for char in "\x00\t\n\r #/:<>?@[\\]^|"):
        return None
    if host.isascii():
        return host.lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def _normalize_path(segments: list[str]) -> list[str]:
    out: list[str] = []
    for segment in segments:
        if segment in (".", "%2e", "%2E"):
            continue
        if segment.lower() in ("..", ".%2e", "%2e.", "%2e%2e"):
            if out:
                out.pop()
            continue
        out.append(segment)
    return out


@dataclass(frozen=True)
class ParsedUrl:
    scheme: str
    userinfo: str
    hostname: str
    port: int | None
    path: str
    query: str | None
    fragment: str | None

    @property
    def origin(self) -> str:
        host = self.hostname
        if self.port is not None:
            host = f"{host}:{self.port}"
        return f"{self.scheme}://{host}"

    @property
    def authority(self) -> str:
        prefix = f"{self.userinfo}@" if self.userinfo else ""
        host = self.hostname
        if self.port is not None:
            host = f"{host}:{self.port}"
        return prefix + host

    def href(self) -> str:
        out = f"{self.scheme}://{self.authority}{self.path}"
        if self.query is not None:
            out += f"?{self.query}"
        if self.fragment is not None:
            out += f"#{self.fragment}"
        return out


def parse(value: str) -> ParsedUrl | None:
    """Parse an absolute URL with a special scheme, or return None."""
    raw = value.strip(_C0_AND_SPACE).translate(_TAB_AND_NEWLINE)

    scheme, colon, rest = raw.partition(":")
    if not colon or not scheme:
        return None
    scheme = scheme.lower()
    if not scheme[0].isalpha() or not all(c.isalnum() or c in "+-." for c in scheme):
        return None
    if scheme not in SPECIAL_PORTS:
        return None

    # A special scheme swallows any number of leading slashes, so `http:example.com`
    # and `http:///example.com` both parse the same way the constructor parses them.
    rest = rest.lstrip("/\\")

    authority = rest
    remainder = ""
    for i, char in enumerate(rest):
        if char in "/\\?#":
            authority, remainder = rest[:i], rest[i:]
            break

    userinfo = ""
    if "@" in authority:
        userinfo, _, authority = authority.rpartition("@")

    port: int | None = None
    host = authority
    if ":" in authority and not authority.startswith("["):
        host, _, port_text = authority.partition(":")
        if port_text:
            if not port_text.isdigit():
                return None
            port = int(port_text)

    hostname = _parse_host(host)
    if hostname is None:
        return None
    if port is not None and port == SPECIAL_PORTS[scheme]:
        port = None

    fragment: str | None = None
    if "#" in remainder:
        remainder, _, raw_fragment = remainder.partition("#")
        fragment = _percent_encode(raw_fragment, _FRAGMENT_SET)

    query: str | None = None
    if "?" in remainder:
        remainder, _, raw_query = remainder.partition("?")
        query = _percent_encode(raw_query, _SPECIAL_QUERY_SET)

    raw_path = remainder.replace("\\", "/")
    segments = _normalize_path(raw_path.split("/")[1:]) if raw_path else []
    path = "/" + "/".join(_percent_encode(segment, _PATH_SET) for segment in segments)

    return ParsedUrl(scheme, userinfo, hostname, port, path, query, fragment)
