"""Build and preview the themed Windows application icon."""

from pathlib import Path

from PIL import Image


source = Path("packaging/JARVIS-theme.png")
icon_path = Path("packaging/JARVIS.ico")
preview_path = Path(".test-tmp/JARVIS-preview.png")
preview_path.parent.mkdir(exist_ok=True)

with Image.open(source) as artwork:
    square = artwork.convert("RGBA")
    square.save(
        icon_path,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    square.resize((256, 256), Image.Resampling.LANCZOS).save(preview_path)

print(icon_path.resolve())
