"""Allowlist HTML cleaner for author-written HTML blocks (no scripts, event handlers or javascript: links)."""
from html import escape
from html.parser import HTMLParser

TAGS = {"a", "b", "strong", "i", "em", "u", "s", "p", "br", "hr", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "span", "div", "small", "sub", "sup", "blockquote", "code", "pre", "table", "thead", "tbody", "tr", "th", "td",
        "img", "figure", "figcaption", "mark", "abbr"}
VOID = {"br", "hr", "img"}
ATTRS = {"href", "title", "target", "rel", "src", "alt", "width", "height", "colspan", "rowspan", "align", "style", "class"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "template", "noscript", "svg", "math", "form", "textarea", "select"}
SAFE_URL = ("http://", "https://", "mailto:", "tel:", "/", "#")
BAD_CSS = ("expression", "url(", "javascript", "@import", "behavior")


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in TAGS:
            return
        kept = []
        for k, v in attrs:
            k, v = k.lower(), (v or "")
            if k not in ATTRS:
                continue
            if k in ("href", "src"):
                vv = "".join(v.split()).lower()
                if not vv.startswith(SAFE_URL) or (k == "src" and vv.startswith("mailto:")):
                    continue
            if k == "style" and any(b in v.lower() for b in BAD_CSS):
                continue
            if k == "target" and v not in ("_blank", "_self"):
                continue
            kept.append(f' {k}="{escape(v, quote=True)}"')
        if tag == "a" and any(a.startswith(' target="_blank"') for a in kept):
            kept = [a for a in kept if not a.startswith(" rel=")] + [' rel="noopener noreferrer"']
        self.out.append(f"<{tag}{''.join(kept)}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in TAGS or tag in VOID or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(escape(data, quote=False))


def clean_html(value):
    p = _Cleaner()
    p.feed(str(value or ""))
    p.close()
    return "".join(p.out) + "".join(f"</{t}>" for t in reversed(p.stack))
