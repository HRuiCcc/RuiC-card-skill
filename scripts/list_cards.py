"""Build a static index page over a directory of generated cards.

A card project is whatever `run_pipeline.py` produces: a directory holding
`card-config.json` with `assets/`, `renders/` and `web/` beside it. Generating
cards one at a time leaves a shelf of unrelated folders and a handful of ports
to remember; this script turns that shelf into one page.

Scan one level deep, under `<root>/*/card-config.json`. Do not recurse: the
pipeline copies the config into `web/` and rewrites its asset paths, so a
recursive walk finds every card twice and links the second copy at a path that
does not serve a page.

The output is a single self-contained HTML file with no dependencies, in the
same spirit as the viewer bundle: no scripts, no web fonts, no embedded images,
just CSS and relative links.

Serve the library root itself, because each card's own `server.mjs` only serves
that one card:

    python3 -m http.server 4173 --directory ~/my-cards

Run: python3 list_cards.py --root ~/my-cards
"""
from pathlib import Path
import argparse
import html
import json
import os
import sys
from urllib.parse import quote

CONFIG_NAME = 'card-config.json'
INDEX_NAME = 'index.html'
# Preferred thumbnail, then the fallbacks a partially built card may still have.
THUMB_CANDIDATES = (
    ('renders', 'hero.png'),
    ('assets', 'subject.png'),
    ('assets', 'background.png'),
)
VIEWER_ENTRY = ('web', 'index.html')
# Fields read from the card root's config; anything missing is simply omitted.
TEXT_FIELDS = ('title', 'subtitle', 'technique', 'tagline', 'edition', 'collection', 'description')


def quote_path(path):
    """Percent-encode each path segment, keeping the separators literal.

    A directory may legitimately be called `教师节快乐` or `金木研 001`; quoting
    the whole string would also escape the slashes and break the link.
    """
    return '/'.join(quote(part) for part in path.split('/'))


def read_config(path):
    """Return the card's config, or None when the file is absent or malformed."""
    try:
        # utf-8-sig matches run_pipeline.py: the config may carry a BOM.
        raw = path.read_text(encoding='utf-8-sig')
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def pick_thumbnail(card_dir):
    """First existing thumbnail candidate, as a path relative to the card dir."""
    for parts in THUMB_CANDIDATES:
        candidate = card_dir.joinpath(*parts)
        if candidate.is_file():
            return '/'.join(parts)
    return None


def collect(root):
    """Scan `<root>/*/card-config.json` and return one record per usable card."""
    cards = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError as error:
        raise SystemExit('Cannot read card library root %s: %s' % (root, error))
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith('.'):
            continue
        config = read_config(entry / CONFIG_NAME)
        if config is None:
            continue
        # A card with no viewer page cannot be opened; keep it out of the shelf
        # rather than shipping a link that 404s.
        if not entry.joinpath(*VIEWER_ENTRY).is_file():
            continue
        text = {field: config.get(field) for field in TEXT_FIELDS}
        for field, value in list(text.items()):
            text[field] = value.strip() if isinstance(value, str) else None
        cards.append({
            'dir': entry,
            'slug': entry.name,
            'text': text,
            'thumb': pick_thumbnail(entry),
        })
    return cards


def _escape(value):
    return html.escape(value, quote=True) if value else ''


def relative_url(target, base):
    """`target` as a URL relative to the directory holding the index page.

    Every link is computed against the index file's own directory, not the
    library root: `--out` may place the page somewhere else entirely, and
    root-relative maths would then point at nothing.
    """
    return quote_path(os.path.relpath(target, base).replace(os.sep, '/'))


def render_card(card, base):
    """One gallery tile, with every path relative to the index page."""
    slug = card['slug']
    text = card['text']
    href = relative_url(card['dir'].joinpath(*VIEWER_ENTRY), base)
    title = text.get('title') or slug
    subtitle = text.get('subtitle') or ''
    edition = text.get('edition') or ''
    collection = text.get('collection') or ''

    thumb = ''
    if card['thumb']:
        src = relative_url(card['dir'] / card['thumb'], base)
        # aspect-ratio reserves the box before the image arrives, so a heavy
        # hero.png cannot reflow the grid while it loads.
        thumb = (
            '<a class="card" href="%s">'
            '<span class="frame"><img src="%s" alt="%s" loading="lazy" decoding="async" '
            'width="1024" height="1536"></span>'
            '</a>' % (href, src, _escape(title))
        )
    else:
        thumb = (
            '<a class="card" href="%s">'
            '<span class="frame frame-empty"><span class="placeholder">%s</span></span>'
            '</a>' % (href, _escape(title))
        )

    meta = []
    if subtitle:
        meta.append('<span class="subtitle">%s</span>' % _escape(subtitle))
    if edition:
        meta.append('<span class="edition">%s</span>' % _escape(edition))
    if collection:
        meta.append('<span class="collection">%s</span>' % _escape(collection))

    return (
        '<li class="item">%s'
        '<div class="meta"><h2><a href="%s">%s</a></h2>%s</div>'
        '</li>'
    ) % (thumb, href, _escape(title), ''.join(meta))


# Deliberately plain CSS: the skill's packaging audit rejects inline SVG and
# any embedded image payload, so the placeholder art is drawn with gradients.
STYLE = """
:root { color-scheme: dark light; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 40px 24px 72px;
  background: #0c0f16; color: #e8ecf5;
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
}
header { max-width: 1180px; margin: 0 auto 32px; }
h1 { margin: 0 0 6px; font-size: 26px; font-weight: 650; letter-spacing: .01em; }
.count { color: #8d97ad; font-size: 14px; }
.hint {
  max-width: 1180px; margin: 0 auto 32px; padding: 12px 16px;
  border: 1px solid #232a3a; border-radius: 10px; background: #121722;
  color: #9aa5bd; font-size: 13px;
}
.hint code {
  background: #1b2231; border-radius: 4px; padding: 1px 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px; color: #cfd8ea;
}
.grid {
  list-style: none; margin: 0 auto; padding: 0; max-width: 1180px;
  display: grid; gap: 26px 22px;
  grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
}
.item { display: flex; flex-direction: column; gap: 10px; }
.card { display: block; text-decoration: none; }
.frame {
  display: block; position: relative; aspect-ratio: 2 / 3; overflow: hidden;
  border-radius: 12px; border: 1px solid #232a3a; background: #121722;
  transition: border-color .16s ease, transform .16s ease;
}
.card:hover .frame, .card:focus-visible .frame {
  border-color: #3d6fd4; transform: translateY(-2px);
}
.frame img { display: block; width: 100%; height: 100%; object-fit: cover; }
.frame-empty {
  background:
    radial-gradient(120% 80% at 50% 12%, #1d2740 0%, #121722 62%),
    linear-gradient(160deg, #16203a 0%, #0f1420 100%);
}
.placeholder {
  position: absolute; inset: auto 12px 14px 12px;
  color: #6f7c96; font-size: 13px; text-align: center;
}
.meta { display: flex; flex-direction: column; gap: 3px; }
.meta h2 { margin: 0; font-size: 15px; font-weight: 600; line-height: 1.35; }
.meta h2 a { color: #e8ecf5; text-decoration: none; }
.meta h2 a:hover { color: #7fa8ff; }
.subtitle { color: #a9b3c9; font-size: 13px; }
.edition, .collection { color: #6f7c96; font-size: 12px; }
.empty { max-width: 1180px; margin: 0 auto; color: #8d97ad; }
@media (max-width: 420px) {
  body { padding: 26px 16px 56px; }
  h1 { font-size: 21px; }
  .grid { grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 20px 14px; }
}
"""


def render_page(cards, base, title):
    """The whole index page as one string."""
    if cards:
        items = '\n'.join('      ' + render_card(card, base) for card in cards)
        body = '    <ul class="grid">\n%s\n    </ul>' % items
    else:
        body = (
            '    <p class="empty">这个目录下还没有卡片。先用 '
            '<code>run_pipeline.py</code> 生成一张，再回来重跑本脚本。</p>'
        )
    count = '%d 张卡片' % len(cards)
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<style>%(style)s</style>
</head>
<body>
  <header>
    <h1>%(title)s</h1>
    <p class="count">%(count)s</p>
  </header>
  <p class="hint">
    点开任意一张进入它的查看器。本页要用<strong>卡库根目录</strong>起服务才能打开：
    <code>python3 -m http.server 4173 --directory .</code>
    ——单张卡自带的 <code>server.mjs</code> 只服务它自己，直接用它打开本页会 404。
  </p>
%(body)s
</body>
</html>
""" % {
        'title': html.escape(title),
        'style': STYLE,
        'count': html.escape(count),
        'body': body,
    }


def build(root, out=None, title=None):
    """Scan `root`, write the index beside it, and return a summary."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise SystemExit('Card library root is not a directory: %s' % root)
    cards = collect(root)
    target = Path(out).resolve() if out else root / INDEX_NAME
    # Links are relative to where the page will actually live.
    page = render_page(cards, target.parent, title or (root.name or 'Card library'))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding='utf-8')
    return {
        'root': str(root),
        'index': str(target),
        'cards': len(cards),
        'with_thumbnail': sum(1 for card in cards if card['thumb']),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Build a static index page over generated cards.')
    parser.add_argument('--root', required=True, help='card library root, holding one directory per card')
    parser.add_argument('--out', help='index path (default: <root>/index.html)')
    parser.add_argument('--title', help='page title (default: the root directory name)')
    args = parser.parse_args(argv)
    summary = build(args.root, args.out, args.title)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
