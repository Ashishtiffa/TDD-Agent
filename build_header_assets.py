"""Regenerate header logo PNGs from assets/page_header_reference.png."""
from pathlib import Path

from PIL import Image

ASSETS = Path(__file__).parent / 'assets'
REF = ASSETS / 'page_header_reference.png'


def main():
    if not REF.exists():
        print(f'Missing {REF}')
        return
    img = Image.open(REF)
    w, h = img.size
    band_h = int(h * 0.38)
    hitachi = img.crop((0, 0, int(w * 0.55), band_h))
    band = img.crop((0, 0, w, band_h))
    hitachi.save(ASSETS / 'hitachi_solutions_logo.png')
    band.save(ASSETS / 'header_band.png')
    print(f'Saved hitachi {hitachi.size}, band {band.size}')


if __name__ == '__main__':
    main()
