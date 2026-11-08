"""Assemble learning-guide.html from the files in this folder, in name order."""
import io, pathlib
here = pathlib.Path(__file__).parent
parts = sorted(p for p in here.glob("*.html"))
out = "".join(io.open(p, encoding="utf-8").read() for p in parts)
(here.parent / "learning-guide.html").write_text(out, encoding="utf-8", newline="\n")
print(f"built learning-guide.html from {len(parts)} parts, {len(out):,} characters")
