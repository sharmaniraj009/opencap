"""mkdocs hook (mkdocs.yml `hooks:`): links to repository files outside docs/ go to GitHub (#9).

The site includes README.md and CONTRIBUTING.md and serves docs/, and those files link to
src/, deploy/, engine/, plan/, ROADMAP.md, LICENSE and so on. Those files are not part of the
site, so a relative link to them would 404 there. This hook rewrites such a link to the file on
GitHub (`blob/main/...`, or `tree/main/...` for a directory), but **only when the target exists
in the repository**. A link to a missing file is left alone, so `mkdocs build --strict` still
fails on it as on any other broken link. Links inside docs/ are left to mkdocs, except that a
link to a docs directory becomes a link to its README.md or index.md, as GitHub shows it.

Same-page anchors in included text: README.md is split over several pages, so its
`[see](#deployment)` may point at a heading that is now on another page. The hook reads the
include directives, works out on which page each heading of an included file ends up, and
points such an anchor there. A heading of that file that is on no page of the site links to
the file on GitHub. Any other unknown anchor is left alone and fails the strict build.

It runs after the include-markdown plugin (mkdocs runs hooks after plugins), so links that
plugin has already rebased onto the including page are rewritten too.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from markdown.extensions.toc import slugify

REPO_URL = "https://github.com/sharmaniraj009/opencap"
BRANCH = "main"

INLINE = re.compile(r"(?<!!)(\]\()([^()\s]+)(\))")             # [text](target), not images
REFDEF = re.compile(r"^(\s{0,3}\[[^\]]+\]:\s*)(\S+)", re.M)    # [label]: target
FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.M | re.S)   # fenced code blocks
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
INCLUDE = re.compile(r'\{%\s*include-markdown\s+"([^"]+)"(.*?)%\}', re.S)
INCLUDE_ARG = re.compile(r'\b(start|end)="([^"]*)"')

# Filled by on_files: for each heading slug of an included file outside docs/, the page it is
# on, and whether the heading itself is there (False: it was the include's start marker, so
# only the section's body is on that page).
_anchor_page: dict[str, tuple[str, bool]] = {}
# Headings of included files that are on no page: slug -> GitHub URL of that heading.
_anchor_elsewhere: dict[str, str] = {}


def _slug(heading: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)      # [text](link) -> text
    return slugify(text.replace("`", "").replace("*", ""), "-")


def _headings(text: str) -> list[str]:
    return [_slug(h) for h in HEADING.findall(FENCE.sub("", text))]


def on_files(files, config):
    _anchor_page.clear()
    _anchor_elsewhere.clear()
    docs_dir = Path(config["docs_dir"]).resolve()
    included: dict[Path, list[tuple[str, int, int]]] = {}
    for f in files.documentation_pages():
        src = Path(f.abs_src_path)
        text = src.read_text(encoding="utf-8")
        own = set(_headings(INCLUDE.sub("", text)))                # headings written on the page
        for m in INCLUDE.finditer(text):
            inc = (src.parent / m.group(1)).resolve()
            if inc.is_relative_to(docs_dir) or not inc.is_file():
                continue
            args = dict(INCLUDE_ARG.findall(m.group(2)))
            body = inc.read_text(encoding="utf-8")
            start = body.find(args["start"]) if "start" in args else 0
            if start < 0:
                continue
            after = start + len(args.get("start", ""))
            end = body.find(args["end"], after) if "end" in args else -1
            end = len(body) if end < 0 else end
            included.setdefault(inc, []).append((f.src_uri, start, end))
            for i, slug in enumerate(_headings(body[start:end])):
                is_marker = i == 0 and "start" in args
                _anchor_page.setdefault(slug, (f.src_uri, not is_marker or slug in own))
    root = docs_dir.parent
    for inc in included:
        for slug in _headings(inc.read_text(encoding="utf-8")):
            if slug not in _anchor_page:
                _anchor_elsewhere[slug] = f"{REPO_URL}/blob/{BRANCH}/{inc.relative_to(root).as_posix()}#{slug}"
    return files


def _same_page_anchor(target: str, page_uri: str, page_slugs: set[str]) -> str:
    slug = target[1:]
    if not slug or slug in page_slugs:
        return target
    if slug in _anchor_page:
        uri, heading_there = _anchor_page[slug]
        if uri == page_uri:
            return target
        rel = os.path.relpath(uri, os.path.dirname(page_uri) or ".").replace(os.sep, "/")
        return rel + (target if heading_there else "")
    return _anchor_elsewhere.get(slug, target)


def _rewrite(target: str, src_dir: Path, docs_dir: Path, root: Path) -> str:
    if SCHEME.match(target) or target.startswith(("#", "/")):
        return target
    path, sep, anchor = target.partition("#")
    if not path:
        return target
    resolved = (src_dir / path).resolve()
    if resolved.is_relative_to(docs_dir):
        # A link to a docs directory (`../adr/`) means its README.md or index.md, as on GitHub.
        if resolved.is_dir():
            for index in ("index.md", "README.md"):
                if (resolved / index).is_file():
                    return f"{path.rstrip('/')}/{index}{sep}{anchor}"
        return target
    if not resolved.is_relative_to(root) or not resolved.exists():
        return target
    kind = "tree" if resolved.is_dir() else "blob"
    return f"{REPO_URL}/{kind}/{BRANCH}/{resolved.relative_to(root).as_posix()}{sep}{anchor}"


def on_page_markdown(markdown: str, page, config, files) -> str:
    docs_dir = Path(config["docs_dir"]).resolve()
    root = docs_dir.parent
    src_dir = Path(page.file.abs_src_path).parent
    page_slugs = set(_headings(markdown))

    def fix_inline(m: re.Match) -> str:
        target = m.group(2)
        if target.startswith("#"):
            return m.group(1) + _same_page_anchor(target, page.file.src_uri, page_slugs) + m.group(3)
        return m.group(1) + _rewrite(target, src_dir, docs_dir, root) + m.group(3)

    def fix_refdef(m: re.Match) -> str:
        return m.group(1) + _rewrite(m.group(2), src_dir, docs_dir, root)

    out, pos = [], 0
    for fence in FENCE.finditer(markdown):                 # leave code blocks untouched
        text = markdown[pos:fence.start()]
        out.append(REFDEF.sub(fix_refdef, INLINE.sub(fix_inline, text)))
        out.append(fence.group(0))
        pos = fence.end()
    out.append(REFDEF.sub(fix_refdef, INLINE.sub(fix_inline, markdown[pos:])))
    return "".join(out)
