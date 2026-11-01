"""One-time icon generation with Pillow; not a runtime dependency."""

from pathlib import Path

from PIL import Image


def main():
    root = Path(__file__).resolve().parents[1]
    folder = root / "app" / "static" / "icons"
    folder.mkdir(parents=True, exist_ok=True)
    with Image.open(root / "brand" / "orbit-1024.png") as source:
        source = source.convert("RGBA")
        for name, size in (("icon-192", 192), ("icon-512", 512), ("apple-touch-icon", 180)):
            source.resize((size, size), Image.Resampling.LANCZOS).save(
                folder / f"{name}.png", optimize=True
            )
        # The full mark fits inside the central 80%-diameter circle: 56% square.
        maskable = Image.new("RGBA", (512, 512), "#0E6E7A")
        mark = source.resize((286, 286), Image.Resampling.LANCZOS)
        maskable.alpha_composite(mark, (113, 113))
        maskable.convert("RGB").save(folder / "icon-maskable-512.png", optimize=True)


if __name__ == "__main__":
    main()
