"""The whole page. One function, one string, no framework and no build step —
grouplink.rebuild commits whatever this returns as site/index.html.

Visual foundations come from Render's brand system: semantic color tokens in
:root with a dark override, PP Neue Montreal for prose, square corners,
1px hairlines, purple reserved for links and focus.

The page carries its own Content-Security-Policy, with the inline style and
script blocks allowed by hash. Adding either one anywhere but STYLES or
ICON_FALLBACK_SCRIPT will be blocked by the browser. The policy is a meta tag
rather than a render.yaml header so the hashes cannot drift from the content
they cover.

The module is page.py rather than render.py, because `render` is the SDK's
import name.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import date

from grouplink.icons import ICON_NAMES, IconName
from grouplink.jsurl import is_mailto_url, normalize_mailto
from grouplink.jsurl import parse as parse_url


@dataclass(frozen=True)
class LinkCard:
    """One row of the links list."""

    #: Display text. Comes from Notion, not from the scrape.
    title: str
    #: Final href, exactly as the Notion row gives it.
    url: str
    #: Scraped og:title or meta description; may be empty.
    description: str
    #: `<origin>/favicon.ico`, hidden on error.
    icon_url: str
    #: Which file under site/assets/link-icons the card draws in its left column.
    icon: IconName


@dataclass(frozen=True)
class SocialLink:
    label: str
    url: str


@dataclass(frozen=True)
class PageModel:
    name: str
    #: Metadata only. The page does not show it.
    tagline: str
    cards: list[LinkCard] = field(default_factory=list)


def escape_html(value: str) -> str:
    """The five replacements the TypeScript build makes, in the same order.

    Not `html.escape`, which emits `&#x27;` for an apostrophe where the
    TypeScript `escapeHtml` emits `&#39;`.
    """
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def safe_url(value: str) -> str:
    """Allow only http(s) and mailto: hrefs into the document.

    Anything else — javascript:, data:, a malformed string from Notion —
    collapses to "#".
    """
    if is_mailto_url(value):
        return normalize_mailto(value)
    parsed = parse_url(value)
    if parsed is None or parsed.scheme not in ("http", "https"):
        return "#"
    return parsed.href()


# Per-row entrance delays. They are generated rather than set inline, because
# the policy allows the <style> block by hash and no style attribute at all.
ROW_STAGGER = "\n".join(
    f".card:nth-child({i + 1}) {{ animation-delay: {i * 30}ms; }}" for i in range(20)
)

# One mask rule per icon file. Generated into the <style> block for the same
# reason ROW_STAGGER is.
ICON_MASKS = "\n".join(
    f".card__mark--{name} {{ --mark: url('/assets/link-icons/{name}.png'); }}"
    for name in ICON_NAMES
)

_STYLES_HEAD = """
@font-face {
  font-family: 'Roobert';
  src: url('/assets/fonts/RoobertVF.woff2') format('woff2-variations');
  font-weight: 300 500;
  font-display: swap;
}
@font-face {
  font-family: 'PP Neue Montreal';
  src: url('/assets/fonts/PPNeueMontreal-Variable.woff2') format('woff2-variations');
  font-weight: 300 500;
  font-display: swap;
}
@font-face {
  font-family: 'PP Neue Montreal Mono';
  src: url('/assets/fonts/PPNeueMontrealMono-Medium.woff2') format('woff2');
  font-weight: 500;
  font-display: swap;
}

:root {
  --bg: #ffffff;
  --bg-secondary: #e3e3e3;
  --border: #e3e3e3;
  --text: #0d0d0d;
  --text-secondary: #4d4d4d;
  --text-faint: #6b6b6b;
  --link: #8a05ff;
  --link-hover: #48008c;
  --link-bg: #e7dbff;
  --accent: #8a05ff;
  --accent-strong: #48008c;
  --row-hover: rgba(0, 0, 0, 0.03);

  --font-brand: 'Roobert', 'Manrope', ui-sans-serif, system-ui, sans-serif;
  --font-default: 'PP Neue Montreal', 'Manrope', ui-sans-serif, system-ui, sans-serif;
  --font-mono: 'PP Neue Montreal Mono', 'Roboto Mono', ui-monospace, SFMono-Regular, Menlo, monospace;

  --ease: cubic-bezier(0.9, 0.1, 0.1, 0.9);

  color-scheme: light dark;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d0d0d;
    --bg-secondary: #141414;
    --border: #272727;
    --text: #ffffff;
    --text-secondary: #c7c7c7;
    --text-faint: #b3b3b3;
    --link: #d1b8ff;
    --link-hover: #e7dbff;
    --link-bg: #48008c;
    --accent: #8a05ff;
    --accent-strong: #c29eff;
    --row-hover: rgba(255, 255, 255, 0.04);
  }
}

*, *::before, *::after { box-sizing: border-box; }

html { -webkit-text-size-adjust: 100%; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-brand);
  font-weight: 400;
  font-size: 16px;
  line-height: 24px;
  letter-spacing: 0.01em;
  -webkit-font-smoothing: antialiased;
}

.page {
  max-width: 640px;
  margin: 0 auto;
  padding: 96px 32px 64px;
  display: flex;
  flex-direction: column;
  gap: 40px;
}

.masthead {
  animation: row-fade 350ms var(--ease) both;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 16px;
  /* Adds to the .page gap, so the links start further below the socials. */
  margin-bottom: 48px;
}

.name { margin: 0; }

.logo-link {
  display: block;
  color: var(--text);
}
.logo-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; }

/*
 * The logo file is solid white, so an <img> would vanish on the light
 * background. It is a mask instead, painted with the text color.
 */
.logo {
  display: block;
  width: 240px;
  height: 46px;
  background-color: currentColor;
  -webkit-mask: url('/assets/render-logo-white.png') center / contain no-repeat;
  mask: url('/assets/render-logo-white.png') center / contain no-repeat;
}

.links { border-top: 1px solid var(--border); }

.card {
  display: grid;
  grid-template-columns: 32px 1fr;
  align-items: start;
  gap: 16px;
  padding: 18px 12px;
  border-bottom: 1px solid var(--border);
  text-decoration: none;
  color: inherit;
  min-height: 56px;
  animation: row-fade 350ms var(--ease) both;
}
"""

_STYLES_MID = """

.card:hover,
.card:focus-visible { background: var(--row-hover); }
.card:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

/*
 * The icon files are dark artwork on transparency, so an <img> would disappear
 * against the dark background. Each one is a mask instead, painted with the
 * faint text color. The top margin sits the 20px square on the title's cap line.
 */
.card__mark {
  width: 20px;
  height: 20px;
  margin-top: 3px;
  background-color: var(--text-faint);
  -webkit-mask: var(--mark) center / contain no-repeat;
  mask: var(--mark) center / contain no-repeat;
}
"""

_STYLES_TAIL = """

.card__body { display: block; }

/*
 * The signature link hover: a purple underline that wipes in left to right and
 * retreats the way it came. It is a background rather than a border or a
 * pseudo-element, so it follows the title onto a second line. The rest position
 * is the right edge, which is where the wipe retreats to; the jump back is
 * invisible because the underline has no width by then.
 */
.card__title {
  display: inline;
  font-size: 17.6px;
  line-height: 26.4px;
  background-image: linear-gradient(var(--link), var(--link));
  background-repeat: no-repeat;
  background-position: right bottom;
  background-size: 0% 1px;
  transition: background-size 200ms var(--ease);
}
.card:hover .card__title,
.card:focus-visible .card__title {
  background-position: left bottom;
  background-size: 100% 1px;
}
.card__desc {
  margin: 4px 0 0;
  font-size: 15.4px;
  line-height: 22px;
  color: var(--text-faint);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.card__meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
}

.card__icon { width: 14px; height: 14px; display: block; flex: none; }
/* Collapse a favicon that 404s, so the target line closes the gap. */
.card__icon--broken { display: none; }

.card__target {
  font-family: var(--font-mono);
  font-weight: 500;
  font-size: 12.1px;
  line-height: 15.4px;
  letter-spacing: 0;
  color: var(--text-faint);
  overflow-wrap: anywhere;
}

.footer {
  font-family: var(--font-mono);
  font-size: 12.1px;
  line-height: 15.4px;
  letter-spacing: 0.02em;
  color: var(--text-faint);
}

@keyframes row-fade {
  from { opacity: 0; }
  to { opacity: 1; }
}

.socials {
  display: flex;
  justify-content: center;
  flex-wrap: wrap;
  gap: 20px;
  /* Adds to the .masthead gap, so the icons clear the logo. */
  margin-top: 28px;
}

.social {
  display: block;
  color: var(--text);
  text-decoration: none;
}
.social:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; }

/*
 * The icon files are solid white, so an <img> would vanish on the light
 * background. Each one is a mask instead, painted with the text color.
 */
.social__icon {
  display: block;
  width: 24px;
  height: 24px;
  background-color: currentColor;
  -webkit-mask: var(--icon) center / contain no-repeat;
  mask: var(--icon) center / contain no-repeat;
  /*
   * Every icon fills its 24x24 viewBox, but the artwork inside leaves a
   * different amount of empty space below it. --nudge is that space, so the
   * drawn bottoms line up across the row.
   */
  transform: translateY(var(--nudge, 0));
}

.social__icon--youtube { --icon: url('/assets/icons/youtube.svg'); --nudge: 3.5px; }
.social__icon--linkedin { --icon: url('/assets/icons/linkedin.svg'); }
.social__icon--x { --icon: url('/assets/icons/x.svg'); }
.social__icon--github { --icon: url('/assets/icons/github.svg'); --nudge: 0.3px; }
.social__icon--discord { --icon: url('/assets/icons/discord.svg'); --nudge: 2.9px; }

@media (max-width: 767px) {
  .page { padding: 48px 16px 40px; gap: 32px; }
  .masthead { margin-bottom: 28px; }
  .socials { margin-top: 20px; }
  .logo { width: 190px; height: 36px; }
  .card { grid-template-columns: 24px 1fr; gap: 12px; padding: 16px 4px; min-height: 44px; }
}

@media (prefers-reduced-motion: reduce) {
  .masthead, .card { animation: none; }
  .card__title { transition: none; }
}
"""

STYLES = _STYLES_HEAD + ROW_STAGGER + _STYLES_MID + ICON_MASKS + _STYLES_TAIL

# A favicon that 404s leaves a broken-image glyph, and no CSS selector matches a
# failed image. `error` does not bubble, so the listener runs in the capture phase.
# It adds a class instead of writing el.style, which the policy would have to allow.
ICON_FALLBACK_SCRIPT = """
addEventListener('error', function (event) {
  var el = event.target;
  if (el instanceof HTMLImageElement && el.classList.contains('card__icon')) {
    el.classList.add('card__icon--broken');
  }
}, true);
"""


def _sha256_source(content: str) -> str:
    """CSP source expression for an inline block, so the policy allows it by hash."""
    digest = hashlib.sha256(content.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


CSP = "; ".join(
    [
        "default-src 'none'",
        # 'self' covers the logo and any other asset the site ships, so the mark still
        # renders over plain http on localhost. Favicons come from whatever origin the
        # link points at, over TLS only.
        "img-src 'self' https:",
        f"style-src {_sha256_source(STYLES)}",
        f"script-src {_sha256_source(ICON_FALLBACK_SCRIPT)}",
        "font-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
    ]
)

# What the row shows as its mono metadata line: host without www, plus the path.
# The path is what tells two rows on the same site apart, so it earns its place.
MAX_TARGET = 44


def _display_target(url: str) -> str:
    """Empty when the URL will not parse.

    A mailto: link shows its recipients and stops. Any headers after the `?` stay in
    the href but not on the line. A link with headers and no recipient shows `mailto`.
    """
    if is_mailto_url(url):
        recipients = normalize_mailto(url).split("?", 1)[0]
        if recipients == "mailto:":
            return "mailto"
        return _truncate(recipients)
    parsed = parse_url(url)
    if parsed is None:
        return ""
    path = parsed.path[:-1] if parsed.path.endswith("/") else parsed.path
    hostname = parsed.hostname
    if hostname.startswith("www."):
        hostname = hostname[len("www.") :]
    return _truncate(hostname + path)


def _truncate(target: str) -> str:
    if len(target) > MAX_TARGET:
        return target[: MAX_TARGET - 1] + "\u2026"
    return target


def _render_card(card: LinkCard) -> str:
    href = escape_html(safe_url(card.url))
    title = escape_html(card.title)
    desc = (
        f'<span class="card__desc">{escape_html(card.description)}</span>'
        if card.description
        else ""
    )
    favicon = (
        f'<img class="card__icon" src="{escape_html(safe_url(card.icon_url))}" '
        'alt="" loading="lazy">'
        if card.icon_url
        else ""
    )
    target = _display_target(card.url)
    meta = (
        f'<span class="card__meta">{favicon}'
        f'<span class="card__target">{escape_html(target)}</span></span>'
        if target
        else ""
    )
    return f"""      <a class="card" href="{href}">
        <span class="card__mark card__mark--{escape_html(card.icon)}" aria-hidden="true"></span>
        <span class="card__body">
          <span class="card__title">{title}</span>
          {desc}
          {meta}
        </span>
      </a>"""


# Where the wordmark in the masthead links.
LOGO_HREF = "https://dashboard.render.com/"

# The masthead's icon row. It is the same on every page and does not come from
# Notion. Each label needs a matching file under site/assets/icons and a
# .social__icon--<label> rule in STYLES.
SOCIALS: list[SocialLink] = [
    SocialLink("YouTube", "https://www.youtube.com/@render-inc"),
    SocialLink("LinkedIn", "https://www.linkedin.com/company/renderco"),
    SocialLink("X", "https://x.com/render"),
    SocialLink("GitHub", "https://github.com/render-oss/sdk"),
    SocialLink("Discord", "https://render.com/discord"),
]


def _render_social(social: SocialLink) -> str:
    href = escape_html(safe_url(social.url))
    icon = escape_html(social.label.strip().lower())
    label = escape_html(social.label)
    return (
        f'        <a class="social" href="{href}" aria-label="{label}">'
        f'<span class="social__icon social__icon--{icon}"></span></a>'
    )


def _one_line(tagline: str) -> str:
    """Collapse every run of whitespace around a line break into a single space."""
    out: list[str] = []
    i = 0
    while i < len(tagline):
        char = tagline[i]
        if char in " \t\r\n\f\v":
            run_end = i
            while run_end < len(tagline) and tagline[run_end] in " \t\r\n\f\v":
                run_end += 1
            run = tagline[i:run_end]
            if "\n" in run:
                out.append(" ")
                i = run_end
                continue
            out.append(run)
            i = run_end
            continue
        out.append(char)
        i += 1
    return "".join(out).strip()


def render_page(model: PageModel, year: int | None = None) -> str:
    """`year` is the footer's copyright year, read from the clock when it is None.

    Taking it as an argument keeps the golden test stable across a new year.
    """
    footer_year = date.today().year if year is None else year
    tagline_text = _one_line(model.tagline)
    cards = "\n".join(_render_card(card) for card in model.cards)
    socials = "\n".join(_render_social(social) for social in SOCIALS)
    socials_block = f"""      <nav class="socials" aria-label="Social">
{socials}
      </nav>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{CSP}">
<title>Render Links</title>
<meta name="description" content="{escape_html(tagline_text)}">
<meta property="og:title" content="{escape_html(model.name)} — links">
<meta property="og:description" content="{escape_html(tagline_text)}">
<meta property="og:type" content="website">
<link rel="icon" href="/assets/render-logomark-black.svg" media="(prefers-color-scheme: light)">
<link rel="icon" href="/assets/render-logomark-white.svg" media="(prefers-color-scheme: dark)">
<style>{STYLES}</style>
</head>
<body>
  <main class="page">
    <header class="masthead">
      <h1 class="name"><a class="logo-link" href="{LOGO_HREF}" aria-label="{escape_html(model.name)}"><span class="logo"></span></a></h1>
{socials_block}
    </header>

    <section>
      <div class="links">
{cards}
      </div>
    </section>

    <footer class="footer">&copy; {footer_year} render.com</footer>
  </main>
<script>{ICON_FALLBACK_SCRIPT}</script>
</body>
</html>
"""
