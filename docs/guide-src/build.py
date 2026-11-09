"""Assemble learning-guide.html from the files in this folder, in name order."""

import pathlib

here = pathlib.Path(__file__).parent
parts = sorted(here.glob("*.html"))
out = "".join(p.read_text(encoding="utf-8") for p in parts)
(here.parent / "learning-guide.html").write_text(out, encoding="utf-8", newline="\n")
print(f"built learning-guide.html from {len(parts)} parts, {len(out):,} characters")
