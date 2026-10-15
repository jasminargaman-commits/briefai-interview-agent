"""Copies the web chat into the site repo: web/ here stays the one source.

    uv run python scripts/sync_site.py path/to/site/repo

web/app.js → dist/briefai.js and web/style.css → dist/briefai.css in the site,
and stamps dist/briefai.html with ?v=<content hash> so browsers fetch the new
files after a change instead of a cached copy. The page itself (head, header,
footer) is the site's own dist/briefai.html; its body must keep the ids
app.js looks for (stage, progress, steps, answers…)."""

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = {"app.js": "briefai.js", "style.css": "briefai.css"}


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/sync_site.py path/to/site/repo")
    site = Path(sys.argv[1])
    dist = site / "dist"
    page = dist / "briefai.html"
    if not page.exists():
        sys.exit(f"{page} not found — is this the site repo?")
    html = page.read_text(encoding="utf-8")
    for source, target in FILES.items():
        data = (ROOT / "web" / source).read_bytes()
        (dist / target).write_bytes(data)
        version = hashlib.sha256(data).hexdigest()[:10]
        html, n = re.subn(rf'/{re.escape(target)}(\?v=[0-9a-f]*)?"', f'/{target}?v={version}"', html)
        if n != 1:
            sys.exit(f"briefai.html should reference /{target} exactly once (found {n})")
        print(f"{source} → dist/{target} (v={version})")
    page.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
