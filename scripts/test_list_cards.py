"""Regression tests for list_cards.py.

Builds throwaway card libraries on disk and asserts the generated index keeps
its promises: one tile per card, working relative links, the documented
fallback order, and no crash on the shapes a half-finished library can have.

Run: python3 test_list_cards.py
Exit code 0 = all assertions passed.
"""
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import list_cards  # noqa: E402


def make_card(root, slug, title='Card', *, renders=True, subject=True,
              background=True, web=True, config=True, raw_config=None, extra=None):
    """Lay out one card directory the way run_pipeline.py would."""
    card = root / slug
    (card / 'assets').mkdir(parents=True, exist_ok=True)
    if renders:
        (card / 'renders').mkdir(parents=True, exist_ok=True)
        (card / 'renders' / 'hero.png').write_bytes(b'\x89PNG\r\n\x1a\nhero')
    if subject:
        (card / 'assets' / 'subject.png').write_bytes(b'\x89PNG\r\n\x1a\nsubject')
    if background:
        (card / 'assets' / 'background.png').write_bytes(b'\x89PNG\r\n\x1a\nbg')
    if web:
        (card / 'web').mkdir(parents=True, exist_ok=True)
        (card / 'web' / 'index.html').write_text('<!DOCTYPE html>', encoding='utf-8')
    if config:
        data = raw_config if raw_config is not None else {
            'title': title, 'subtitle': '副标题', 'edition': 'No.001 / 001',
            'collection': '典藏',
        }
        if isinstance(data, str):
            (card / 'card-config.json').write_text(data, encoding='utf-8')
        else:
            (card / 'card-config.json').write_text(
                json.dumps(data, ensure_ascii=False), encoding='utf-8')
    if extra:
        for name, body in extra.items():
            target = card / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding='utf-8')
    return card


def link_targets(page):
    """Every href/src in the page, percent-decoded, in document order."""
    return [unquote(m) for m in re.findall(r'(?:href|src)="([^"]+)"', page)]


def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # 1) A normal library: two cards, both complete.
        make_card(root, 'knight', '骑士')
        make_card(root, 'mage', '法师')
        summary = list_cards.build(str(root))
        assert summary['cards'] == 2, summary
        assert summary['with_thumbnail'] == 2, summary
        page = (root / 'index.html').read_text(encoding='utf-8')
        assert '<meta charset="utf-8">' in page, 'page must declare UTF-8'
        assert '骑士' in page and '法师' in page, 'titles must appear'
        assert 'No.001 / 001' in page, 'edition must appear'

        # Every link resolves on disk, and every target is a real file.
        targets = link_targets(page)
        assert targets, 'page must contain links'
        for target in targets:
            assert not target.startswith('/'), f'absolute link would break: {target}'
            assert not target.startswith('http'), f'external link unexpected: {target}'
            resolved = (root / target).resolve()
            assert resolved.is_file(), f'link does not resolve: {target}'
        assert 'knight/web/index.html' in targets, targets
        assert 'knight/renders/hero.png' in targets, targets

        # 2) No renders/: fall back to the subject, then the background.
        only_subject = Path(td) / 'only-subject'
        only_subject.mkdir()
        make_card(only_subject, 'solo', '独', renders=False)
        page2 = (only_subject / 'index.html')
        list_cards.build(str(only_subject))
        text2 = page2.read_text(encoding='utf-8')
        assert 'solo/assets/subject.png' in link_targets(text2), 'subject fallback'

        bg_only = Path(td) / 'bg-only'
        bg_only.mkdir()
        make_card(bg_only, 'plain', '素', renders=False, subject=False)
        list_cards.build(str(bg_only))
        text3 = (bg_only / 'index.html').read_text(encoding='utf-8')
        assert 'plain/assets/background.png' in link_targets(text3), 'background fallback'

        # A card with no artwork at all still gets a tile, drawn in CSS.
        no_art = Path(td) / 'no-art'
        no_art.mkdir()
        make_card(no_art, 'bare', '空', renders=False, subject=False, background=False)
        list_cards.build(str(no_art))
        text4 = (no_art / 'index.html').read_text(encoding='utf-8')
        assert 'frame-empty' in text4, 'missing artwork must fall back to a CSS placeholder'
        assert '<img' not in text4, 'no image tag when there is no artwork'
        assert '<svg' not in text4.lower(), 'packaging audit rejects inline SVG'

        # 3) Missing web/index.html: the card is skipped, not linked into a 404.
        partial = Path(td) / 'partial'
        partial.mkdir()
        make_card(partial, 'good', '好的')
        make_card(partial, 'broken', '坏的', web=False)
        s = list_cards.build(str(partial))
        assert s['cards'] == 1, f'card without a viewer page must be skipped: {s}'
        t = (partial / 'index.html').read_text(encoding='utf-8')
        assert '好的' in t and '坏的' not in t, 'only the usable card is listed'

        # 4) An empty root still produces a valid page rather than crashing.
        blank = Path(td) / 'blank'
        blank.mkdir()
        s = list_cards.build(str(blank))
        assert s['cards'] == 0, s
        t = (blank / 'index.html').read_text(encoding='utf-8')
        assert '<html' in t and '</html>' in t, 'empty library still yields a page'

        # 5) Chinese and space-bearing directory names must stay clickable.
        cjk = Path(td) / 'cjk'
        cjk.mkdir()
        make_card(cjk, '教师节快乐', '教师节快乐')
        make_card(cjk, '金木研 001', '金木研')
        s = list_cards.build(str(cjk))
        assert s['cards'] == 2, s
        t = (cjk / 'index.html').read_text(encoding='utf-8')
        tg = link_targets(t)
        assert '教师节快乐/web/index.html' in tg, tg
        assert '金木研 001/web/index.html' in tg, f'space not preserved: {tg}'
        for target in tg:
            assert (cjk / target).resolve().is_file(), f'unresolvable: {target}'
        # The raw page must not contain a bare space inside a URL.
        for raw in re.findall(r'(?:href|src)="([^"]+)"', t):
            assert ' ' not in raw, f'unencoded space in URL: {raw}'

        # 6) A malformed config is skipped, and a BOM'd one is still read.
        bad = Path(td) / 'bad'
        bad.mkdir()
        make_card(bad, 'broken-json', raw_config='{ this is not json')
        make_card(bad, 'bom', raw_config=None)
        bom_path = bad / 'bom' / 'card-config.json'
        bom_path.write_text(json.dumps({'title': '带 BOM'}, ensure_ascii=False),
                            encoding='utf-8-sig')
        s = list_cards.build(str(bad))
        assert s['cards'] == 1, f'only the parseable config counts: {s}'
        t = (bad / 'index.html').read_text(encoding='utf-8')
        assert '带 BOM' in t, 'utf-8-sig config must be read'

        # 7) A JSON array (not an object) is not a config.
        arr = Path(td) / 'arr'
        arr.mkdir()
        make_card(arr, 'listy', raw_config='[1, 2, 3]')
        assert list_cards.build(str(arr))['cards'] == 0, 'non-object config rejected'

        # 8) Nesting is not followed: web/card-config.json must not become a card.
        nested = Path(td) / 'nested'
        nested.mkdir()
        make_card(nested, 'real', '真卡', extra={'web/card-config.json': json.dumps({'title': '副本'})})
        s = list_cards.build(str(nested))
        assert s['cards'] == 1, f'recursing would double-count the card: {s}'
        t = (nested / 'index.html').read_text(encoding='utf-8')
        assert '副本' not in t, 'the generated web/ copy must be ignored'
        assert t.count('真卡') >= 1, 'the real card is listed'

        # 9) Titles are escaped, not injected.
        esc = Path(td) / 'esc'
        esc.mkdir()
        make_card(esc, 'xss', '<img src=x onerror=alert(1)>')
        list_cards.build(str(esc))
        t = (esc / 'index.html').read_text(encoding='utf-8')
        # The raw page must not carry a live tag; the escaped form must be there.
        assert '<img src=x' not in t, 'unescaped title leaked into the page'
        assert '&lt;img src=x onerror=alert(1)&gt;' in t, 'escaped title expected'

        # 10) --out elsewhere, and a custom title.
        custom = Path(td) / 'custom'
        custom.mkdir()
        make_card(custom, 'one', '一')
        out = Path(td) / 'out' / 'shelf.html'
        s = list_cards.build(str(custom), out=str(out), title='我的卡库')
        assert Path(s['index']) == out.resolve(), s
        assert out.is_file(), 'custom --out must be written'
        t = out.read_text(encoding='utf-8')
        assert '我的卡库' in t, 'custom title must be used'
        # Relative links from a different directory still resolve.
        for target in link_targets(t):
            assert (out.parent / target).resolve().is_file(), f'broken from --out: {target}'

    print('ALL TESTS PASSED')


if __name__ == '__main__':
    main()
