#!/usr/bin/env python3
"""Serve a repository Markdown document as a styled page.

The document is read from disk per request rather than baked into a string constant. The
alternative — hand-converting it to HTML the way `HOW_PAGE` is written — would put a second copy
of 4,000 words inside `harness.py`, and the two would drift the moment anyone edited the Markdown.
This file is the whole reason a reader can trust that the page and the repository agree.

Only the subset these documents actually use is implemented: ATX headings, fenced code, pipe
tables, blockquotes, bullet and numbered lists, and inline code/bold/italic/links. This is
deliberately not a Markdown implementation — anything unrecognized is emitted as a paragraph, so
an unsupported construct degrades into plain text instead of vanishing or breaking the page.
"""
import html, os, re

ROOT = os.path.dirname(os.path.abspath(__file__))

_CODE_SPAN = re.compile(r"`([^`]+)`")
_HEADING = re.compile(r"(#{1,6})\s+(.*)")
_BULLET = re.compile(r"[-*]\s+(.*)")
_NUMBERED = re.compile(r"\d+\.\s+(.*)")
_BLOCK_START = re.compile(r"(```|#{1,6}\s|\||>\s|[-*]\s|\d+\.\s)")


def _slug(text):
    """A stable anchor. Inline markers are stripped first so `#the-life-of-a-point-query` does not
    become `#the-life-of-a-code-point-code-query` the day someone adds backticks to a heading."""
    plain = _CODE_SPAN.sub(r"\1", text).replace("*", "")
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", plain.lower())).strip("-")


def _inline(text):
    """Escape, then format everything OUTSIDE code spans. Splitting on backticks first is what
    keeps `**kwargs` in a code span from turning into bold."""
    out = []
    for i, part in enumerate(_CODE_SPAN.split(text)):
        esc = html.escape(part, quote=False)
        if i % 2:                                    # odd chunks are code-span contents
            out.append("<code>" + esc + "</code>")
            continue
        esc = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", esc)
        esc = re.sub(r"(?<!\*)\*([^*\s][^*]*)\*(?!\*)", r"<em>\1</em>", esc)
        esc = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', esc)
        out.append(esc)
    return "".join(out)


def _cells(row):
    return [c.strip() for c in row.strip().strip("|").split("|")]


def render(md):
    """Markdown -> (body_html, [(anchor, heading_text)] for every h2)."""
    lines = md.split("\n")
    out, toc, i = [], [], 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("```"):
            lang = line[3:].strip()
            i += 1
            buf = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1                                    # consume the closing fence
            cls = ' class="lang-%s"' % lang if lang.isalnum() else ""
            out.append("<pre%s>%s</pre>" % (cls, html.escape("\n".join(buf), quote=False)))
            continue

        m = _HEADING.match(line)
        if m:
            level, text = len(m.group(1)), m.group(2).strip()
            anchor = _slug(text)
            if level == 2:
                toc.append((anchor, text))
            out.append('<h%d id="%s">%s</h%d>' % (level, anchor, _inline(text), level))
            i += 1
            continue

        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(lines[i])
                i += 1
            # row 1 is the header and row 2 the |---| separator, which is layout, not content
            body = rows[2:] if len(rows) > 1 and set(rows[1]) <= set("|-: ") else rows[1:]
            head = "".join("<th>%s</th>" % _inline(c) for c in _cells(rows[0]))
            trs = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(c) for c in _cells(r))
                          for r in body)
            out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (head, trs))
            continue

        if line.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(lines[i].lstrip(">").strip())
                i += 1
            out.append("<blockquote>%s</blockquote>" % _inline(" ".join(buf).strip()))
            continue

        for pattern, tag in ((_BULLET, "ul"), (_NUMBERED, "ol")):
            if pattern.fullmatch(line.strip()) or (line[:1] in "-*0123456789" and pattern.match(line)):
                items = []
                while i < len(lines) and pattern.match(lines[i].strip()):
                    item = [pattern.match(lines[i].strip()).group(1)]
                    i += 1
                    # a wrapped continuation line is indented and starts no new block
                    while (i < len(lines) and lines[i].startswith("  ")
                           and lines[i].strip() and not _BLOCK_START.match(lines[i].strip())):
                        item.append(lines[i].strip())
                        i += 1
                    items.append(" ".join(item))
                out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % _inline(x)
                                                         for x in items), tag))
                break
        else:
            if not line.strip():
                i += 1
                continue
            buf = []
            while i < len(lines) and lines[i].strip() and not _BLOCK_START.match(lines[i]):
                buf.append(lines[i].strip())
                i += 1
            if buf:
                out.append("<p>%s</p>" % _inline(" ".join(buf)))
            else:                                     # never spin: emit the line and move on
                out.append("<p>%s</p>" % _inline(line.strip()))
                i += 1
    return "\n".join(out), toc


_CSS = """
 body{font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:820px;
   margin:40px auto;padding:0 20px;color:#1a1a1a}
 h1{font-size:1.6em;margin:0 0 6px} h2{font-size:1.15em;margin:34px 0 8px;padding-top:6px}
 h3{font-size:1em;margin:22px 0 6px;color:#3c4043}
 p{margin:10px 0} a{color:#1a73e8;text-decoration:none} a:hover{text-decoration:underline}
 .sub{color:#5f6368;margin-bottom:22px}
 pre{background:#0d1117;color:#c9d1d9;padding:14px 16px;border-radius:10px;overflow-x:auto;
   font-size:.8em;line-height:1.45}
 blockquote{margin:14px 0;padding:8px 16px;border-left:3px solid #1a73e8;background:#f8f9fa;
   color:#3c4043}
 table{border-collapse:collapse;width:100%;margin:12px 0;font-size:.92em;display:block;
   overflow-x:auto}
 th,td{text-align:left;padding:7px 12px 7px 0;border-bottom:1px solid #e8eaed;vertical-align:top}
 th{color:#5f6368;font-weight:600}
 code{background:#f1f3f4;border-radius:4px;padding:1px 5px;font-size:.88em}
 pre code{background:none;padding:0}
 li{margin:4px 0}
 /* The ASCII decision graphs run to ~131 columns — wider than a comfortable prose measure. Rather
    than shrink every code block to suit the widest one, let code break out of the text column when
    the viewport can afford it. Prose keeps its measure; the diagrams stop scrolling. */
 @media(min-width:1040px){pre{width:calc(100% + 120px);margin-left:-60px}}
 .toc{background:#f8f9fa;border:1px solid #e8eaed;border-radius:10px;padding:14px 18px;margin:22px 0}
 .toc p{margin:0 0 8px;color:#5f6368;font-size:.85em;text-transform:uppercase;letter-spacing:.04em}
 .toc ol{margin:0;padding-left:20px;columns:2;column-gap:28px} .toc li{margin:3px 0;font-size:.92em}
 @media(max-width:640px){.toc ol{columns:1}}
"""


def page(title, body, toc, subtitle="", back=("./", "‹ back")):
    """The document wrapped in the same chrome the rest of the site uses."""
    nav = '<a href="%s">%s</a>' % back
    sub = '<p class="sub">%s %s</p>' % (_inline(subtitle), nav) if subtitle else \
          '<p class="sub">%s</p>' % nav
    contents = ""
    if len(toc) > 2:
        contents = ('<div class="toc"><p>Contents</p><ol>%s</ol></div>'
                    % "".join('<li><a href="#%s">%s</a></li>' % (a, html.escape(t, quote=False))
                              for a, t in toc))
    # The document opens with its own <h1>. The chrome belongs UNDER that title, not above it, so
    # split the heading off and reassemble: title, subtitle, contents, then the document.
    head, sep, rest = body.partition("</h1>")
    if sep:
        body = head + sep + "\n" + sub + "\n" + contents + "\n" + rest
    else:
        body = sub + "\n" + contents + "\n" + body
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>%s</title><style>%s</style></head><body>\n%s\n</body></html>"
            % (html.escape(title, quote=False), _CSS, body))


def markdown_page(filename, title, subtitle="", back=("./", "‹ back")):
    """The rendered document, or None when the file is absent.

    Returning None rather than raising matters: these documents are not always present in a
    deployment (a fresh clone, a partial rsync), and a missing document should be a clean 404
    rather than a traceback on a public endpoint.
    """
    path = os.path.join(ROOT, filename)
    try:
        with open(path, encoding="utf-8") as fh:
            md = fh.read()
    except OSError:
        return None
    # The document supplies its own H1; the page chrome must not print a second one.
    body, toc = render(md)
    return page(title, body, toc, subtitle, back)
