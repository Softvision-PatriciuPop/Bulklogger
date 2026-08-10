"""Regenerate the app icons from the source artwork.

    pip install -r requirements-dev.txt
    python make_icons.py

Produces, all committed so a normal build needs no image library:
    bulklogger.ico   Windows exe resource and title bar
    bulklogger.icns  macOS .app bundle
    bulklogger.png   Tk iconphoto on macOS and Linux
"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "documentation" / "4320180.png"

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
ICNS_SIZES = [(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512),
              (1024, 1024)]


def main():
    art = Image.open(SOURCE).convert("RGBA")
    print(f"source {SOURCE.name}: {art.width}x{art.height}")

    art.save(ROOT / "bulklogger.ico", format="ICO", sizes=ICO_SIZES)
    print(f"  bulklogger.ico   {len(ICO_SIZES)} sizes")

    art.resize((256, 256), Image.LANCZOS).save(ROOT / "bulklogger.png", format="PNG")
    print("  bulklogger.png   256x256")

    # ICNS wants a square master at least as large as the biggest member.
    art.resize((1024, 1024), Image.LANCZOS).save(
        ROOT / "bulklogger.icns", format="ICNS", sizes=ICNS_SIZES)
    print(f"  bulklogger.icns  {len(ICNS_SIZES)} sizes")


if __name__ == "__main__":
    main()
