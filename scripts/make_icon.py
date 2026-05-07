#!/usr/bin/env python3
"""Generate brand/dassiedrop.ico from the brand PNG icon.

Requires macOS (uses sips for lossless PNG resizing).
Run once and commit the output; CI uses the committed file directly.

Usage:
    python scripts/make_icon.py
"""
import io
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "brand" / "images" / "dassiedrop-icon.png.png"
OUTPUT = REPO_ROOT / "brand" / "dassiedrop.ico"
SIZES = [16, 32, 48, 64, 128, 256]


def resize_png(source: Path, size: int) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(
            [
                "sips",
                "--resampleHeightWidth", str(size), str(size),
                str(source),
                "--out", str(tmp_path),
            ],
            check=True,
            capture_output=True,
        )
        return tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)


def make_ico(images: list[tuple[int, bytes]]) -> bytes:
    count = len(images)
    data_offset = 6 + 16 * count

    buf = io.BytesIO()
    buf.write(struct.pack("<HHH", 0, 1, count))  # ICONDIR header

    offset = data_offset
    for size, data in images:
        w = 0 if size == 256 else size  # 0 encodes 256 in the ICO spec
        h = 0 if size == 256 else size
        buf.write(struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset))
        offset += len(data)

    for _, data in images:
        buf.write(data)

    return buf.getvalue()


def main() -> None:
    if sys.platform != "darwin":
        print("This script requires macOS (uses sips). Commit the generated .ico instead.")
        sys.exit(1)

    if not SOURCE.exists():
        print(f"Source not found: {SOURCE}")
        sys.exit(1)

    print(f"Source : {SOURCE.relative_to(REPO_ROOT)}")
    images = []
    for size in SIZES:
        data = resize_png(SOURCE, size)
        images.append((size, data))
        print(f"  {size:>3}x{size:<3}  {len(data):>7,} bytes")

    ico = make_ico(images)
    OUTPUT.write_bytes(ico)
    print(f"Output : {OUTPUT.relative_to(REPO_ROOT)}  ({len(ico):,} bytes total)")


if __name__ == "__main__":
    main()
