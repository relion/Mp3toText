"""Build DOCUMENTATION.html from DOCUMENTATION.md (Mermaid diagrams rendered in the browser).

Usage:  python docs/build_html.py      (requires: pip install markdown)
"""

import html
import re
from pathlib import Path

import markdown
from markdown.extensions.toc import slugify_unicode

DOCS = Path(__file__).resolve().parent
SRC = DOCS / "DOCUMENTATION.md"
OUT = DOCS / "DOCUMENTATION.html"

TEMPLATE = """<!doctype html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mp3toText – תיעוד</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Heebo:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #f7f8fa; --surface: #ffffff; --text: #1f2430; --muted: #5d6576;
  --border: #e1e4ea; --accent: #2f6fd1; --accent-soft: #e8f0fc;
  --code-bg: #f1f3f6; --th-bg: #f3f5f8;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14171c; --surface: #1c2027; --text: #e4e7ec; --muted: #9aa3b2;
    --border: #2d333d; --accent: #6ea0ec; --accent-soft: #1f2b3d;
    --code-bg: #242a33; --th-bg: #232832;
  }
}
:root[data-theme="dark"] {
  --bg: #14171c; --surface: #1c2027; --text: #e4e7ec; --muted: #9aa3b2;
  --border: #2d333d; --accent: #6ea0ec; --accent-soft: #1f2b3d;
  --code-bg: #242a33; --th-bg: #232832;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.7 "Heebo", "Segoe UI", Arial, sans-serif;
}
.layout { display: grid; grid-template-columns: 260px minmax(0, 1fr); max-width: 1280px; margin: 0 auto; }
nav.toc {
  position: sticky; top: 0; align-self: start; height: 100vh; overflow-y: auto;
  padding: 28px 20px; border-inline-end: 1px solid var(--border);
}
nav.toc .brand { font-weight: 700; font-size: 18px; margin-bottom: 14px; }
nav.toc ul { list-style: none; margin: 0; padding: 0; }
nav.toc ul ul { padding-inline-start: 14px; font-size: 14px; }
nav.toc a { display: block; padding: 3px 8px; border-radius: 6px; color: var(--muted); text-decoration: none; }
nav.toc a:hover, nav.toc a.active { background: var(--accent-soft); color: var(--accent); }
main { padding: 32px 48px 80px; min-width: 0; }
h1 { font-size: 32px; margin: 0 0 8px; }
h2 { font-size: 24px; margin-top: 48px; padding-bottom: 6px; border-bottom: 2px solid var(--border); }
h3 { font-size: 19px; margin-top: 32px; }
a { color: var(--accent); }
hr { border: 0; border-top: 1px solid var(--border); margin: 32px 0; }
code {
  font-family: Consolas, "Cascadia Mono", monospace; font-size: 0.9em;
  background: var(--code-bg); padding: 1px 5px; border-radius: 4px; direction: ltr; unicode-bidi: isolate;
}
pre { background: var(--code-bg); padding: 14px 16px; border-radius: 8px; overflow-x: auto; direction: ltr; text-align: left; }
pre code { background: none; padding: 0; }
.table-wrap { overflow-x: auto; margin: 16px 0; }
table { border-collapse: collapse; width: 100%; background: var(--surface); font-size: 15px; }
th, td { border: 1px solid var(--border); padding: 7px 12px; text-align: start; vertical-align: top; }
th { background: var(--th-bg); font-weight: 500; }
img { max-width: 100%; height: auto; border: 1px solid var(--border); border-radius: 8px;
      box-shadow: 0 4px 18px rgba(0,0,0,.08); }
.diagram {
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 16px; margin: 18px 0; overflow-x: auto;
}
/* labels carry their own RTL isolates; keep the diagram base LTR like GitHub */
pre.mermaid { direction: ltr; background: none; padding: 0; margin: 0; text-align: center; }
.theme-toggle {
  margin-top: 18px; font: inherit; font-size: 13px; color: var(--muted); cursor: pointer;
  background: none; border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px;
}
@media (max-width: 860px) {
  .layout { display: block; }
  nav.toc { position: static; height: auto; border: 0; border-bottom: 1px solid var(--border); padding: 16px; }
  nav.toc ul ul { display: none; }
  main { padding: 20px 16px 60px; }
  h1 { font-size: 26px; }
}
</style>
</head>
<body>
<div class="layout">
  <nav class="toc">
    <div class="brand">🎵 Mp3toText</div>
    __TOC__
    <button class="theme-toggle" id="themeToggle" type="button">מצב בהיר / כהה</button>
  </nav>
  <main>
__BODY__
  </main>
</div>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<script>
(function () {
  const root = document.documentElement;
  try { const t = localStorage.getItem("theme"); if (t) root.dataset.theme = t; } catch (e) {}
  const isDark = () => root.dataset.theme ? root.dataset.theme === "dark"
                                          : matchMedia("(prefers-color-scheme: dark)").matches;
  const sources = [...document.querySelectorAll("pre.mermaid")].map(el => el.textContent);

  async function draw() {
    if (!window.mermaid) return;  // offline: diagrams stay as source text
    mermaid.initialize({ startOnLoad: false, theme: isDark() ? "dark" : "default", securityLevel: "strict" });
    document.querySelectorAll("pre.mermaid").forEach((el, i) => {
      el.removeAttribute("data-processed"); el.textContent = sources[i];
    });
    await mermaid.run({ querySelector: "pre.mermaid" });
  }
  draw();

  document.getElementById("themeToggle").addEventListener("click", () => {
    root.dataset.theme = isDark() ? "light" : "dark";
    try { localStorage.setItem("theme", root.dataset.theme); } catch (e) {}
    draw();
  });

  // highlight the current section in the sidebar
  const links = [...document.querySelectorAll("nav.toc a")];
  const obs = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (!e.isIntersecting) return;
      links.forEach(a => a.classList.toggle("active", a.getAttribute("href") === "#" + e.target.id));
    });
  }, { rootMargin: "0px 0px -70% 0px" });
  document.querySelectorAll("main h2[id], main h3[id]").forEach(h => obs.observe(h));
})();
</script>
</body>
</html>
"""


def build():
    src = SRC.read_text(encoding="utf-8")

    # pull Mermaid blocks out before Markdown processing, put them back as <pre class="mermaid">
    diagrams = []

    def stash(m):
        diagrams.append(m.group(1))
        return f"\n\nMERMAID_PLACEHOLDER_{len(diagrams) - 1}\n\n"

    src = re.sub(r"```mermaid\n(.*?)```", stash, src, flags=re.S)

    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
        extension_configs={"toc": {"slugify": slugify_unicode, "toc_depth": "2-3"}},
    )
    body = md.convert(src)

    for i, code in enumerate(diagrams):
        block = f'<div class="diagram"><pre class="mermaid">{html.escape(code)}</pre></div>'
        body = body.replace(f"<p>MERMAID_PLACEHOLDER_{i}</p>", block)

    body = re.sub(r"(<table>.*?</table>)", r'<div class="table-wrap">\1</div>', body, flags=re.S)
    # the sidebar replaces the in-document table of contents
    body = re.sub(r"<p><strong>תוכן העניינים</strong></p>\s*<ol>.*?</ol>\s*", "", body, count=1, flags=re.S)

    # sidebar: the h2/h3 entries under the document title
    toc = md.toc
    m = re.search(r"<ul>\s*<li><a[^>]*>[^<]*</a>\s*(<ul>.*</ul>)\s*</li>\s*</ul>", toc, flags=re.S)
    if m:
        toc = m.group(1)
    # TOC entries lose the <code> styling of headings; isolate code-like runs such as
    # "main()" or "TranscribeWorker(QThread)" so RTL layout doesn't flip their brackets
    toc = re.sub(r">([^<]+)</a>",
                 lambda a: ">" + re.sub(r"[A-Za-z_][\w.]*(?:\([\w.]*\))?",
                                        lambda w: f'<bdi dir="ltr">{w.group(0)}</bdi>', a.group(1)) + "</a>",
                 toc)

    OUT.write_text(TEMPLATE.replace("__TOC__", toc).replace("__BODY__", body), encoding="utf-8")
    print(f"wrote {OUT} ({len(diagrams)} diagrams)")


if __name__ == "__main__":
    build()
