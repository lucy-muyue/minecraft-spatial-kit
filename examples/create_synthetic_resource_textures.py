"""Regenerate the fictional textures for the synthetic mod resource example."""

from pathlib import Path

from PIL import Image, ImageDraw


ASSETS = Path(__file__).parent / "synthetic_mod_resource_pack/assets/spatial_demo/textures/block"


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    metal = Image.new("RGBA", (16, 16), (117, 64, 87, 255))
    draw = ImageDraw.Draw(metal)
    for y in range(0, 16, 4):
        draw.rectangle((0, y, 15, y + 1), fill=(176, 107, 133, 255))
    draw.rectangle((2, 2, 5, 5), fill=(211, 148, 170, 255))
    draw.rectangle((10, 10, 13, 13), fill=(77, 42, 73, 255))
    metal.save(ASSETS / "lilac_metal.png")

    crystal = Image.new("RGBA", (16, 16), (37, 91, 129, 255))
    draw = ImageDraw.Draw(crystal)
    draw.polygon([(8, 1), (13, 5), (12, 12), (8, 15), (3, 11), (2, 5)], fill=(77, 205, 214, 255))
    draw.polygon([(8, 2), (8, 14), (3, 10), (3, 5)], fill=(149, 240, 228, 255))
    draw.rectangle((7, 4, 8, 7), fill=(225, 255, 238, 255))
    crystal.save(ASSETS / "synthetic_crystal.png")

    copper = Image.new("RGBA", (16, 16), (132, 77, 49, 255))
    draw = ImageDraw.Draw(copper)
    draw.rectangle((0, 1, 15, 2), fill=(196, 132, 72, 255))
    draw.rectangle((2, 5, 5, 8), fill=(220, 167, 91, 255))
    draw.rectangle((10, 10, 13, 13), fill=(80, 45, 39, 255))
    copper.save(ASSETS / "copper.png")


if __name__ == "__main__":
    main()
