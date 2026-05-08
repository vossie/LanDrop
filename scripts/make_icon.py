#!/usr/bin/env python3
"""Generate brand/images/dassiedrop.ico from the brand PNG icon using Pillow.

Install Pillow first: pip install pillow

Usage:
    python scripts/make_icon.py
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "brand" / "images" / "dassiedrop-icon.png.png"
OUTPUT = REPO_ROOT / "brand" / "images" / "dassiedrop.ico"
SIZES = [16, 32, 48, 64, 128, 256]


def main() -> None:
    try:
        from PIL import Image
    except ImportError:
        print("Pillow is required: pip install pillow")
        sys.exit(1)

    if not SOURCE.exists():
        print(f"Source not found: {SOURCE}")
        sys.exit(1)

    print(f"Source : {SOURCE.relative_to(REPO_ROOT)}")
    img = Image.open(SOURCE).convert("RGBA")

    size_tuples = [(s, s) for s in SIZES]
    img.save(str(OUTPUT), format="ICO", sizes=size_tuples)
    print(f"Output : {OUTPUT.relative_to(REPO_ROOT)}  ({OUTPUT.stat().st_size:,} bytes)")
    print(f"Sizes  : {', '.join(f'{s}x{s}' for s in SIZES)}")


if __name__ == "__main__":
    main()
