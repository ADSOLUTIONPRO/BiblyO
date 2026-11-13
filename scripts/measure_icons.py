#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Mesure la ressemblance du sprite avec des jeux d'icônes, pour documenter une provenance sans la présumer.

Chaque icône est tracée dans Chromium, puis comparée à chaque icône du jeu : recouvrement des pixels tracés (IoU).
Une ressemblance n'établit pas une origine. Hors barrière de qualité : les jeux comparés se téléchargent à part.
Usage : `measure_icons.py <dossier de SVG Feather> <dossier de SVG Lucide> <sortie.json>`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SPRITE = ROOT / "src" / "biblyo" / "static" / "icons" / "sprite.svg"
MEASURE = r"""
async ({ours, sets}) => {
  const size = 48;
  const canvas = new OffscreenCanvas(size, size);
  const context = canvas.getContext('2d', {willReadFrequently: true});
  async function raster(inner) {
    const attributes = `width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="black"`
      + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" ${attributes}>${inner}</svg>`;
    const image = new Image();
    image.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
    await image.decode();
    context.clearRect(0, 0, size, size);
    context.drawImage(image, 0, 0);
    const data = context.getImageData(0, 0, size, size).data;
    const bits = new Uint8Array(size * size);
    for (let i = 0; i < size * size; i++) bits[i] = data[i * 4 + 3] > 96 ? 1 : 0;
    return bits;
  }
  function overlap(a, b) {
    let both = 0, either = 0;
    for (let k = 0; k < a.length; k++) { both += a[k] & b[k]; either += a[k] | b[k]; }
    return either ? both / either : 0;
  }
  const prepared = {};
  for (const [library, icons] of Object.entries(sets)) {
    prepared[library] = [];
    for (const [name, inner] of Object.entries(icons)) {
      try { prepared[library].push([name, await raster(inner)]); } catch (error) { /* SVG illisible : ignoré */ }
    }
  }
  const best = {};
  for (const [name, path] of Object.entries(ours)) {
    const mine = await raster(`<path d="${path}"/>`);
    best[name] = {};
    for (const [library, list] of Object.entries(prepared)) {
      let top = ['', 0];
      for (const [other, bits] of list) {
        const value = overlap(mine, bits);
        if (value > top[1]) top = [other, value];
      }
      best[name][library] = top;
    }
  }
  return best;
}
"""


def _inner_markup(path: Path) -> str:
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)
    return re.sub(r"(?s)^.*?<svg[^>]*>|</svg>\s*$", "", text)


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    feather, lucide, output = map(Path, sys.argv[1:])
    ours = dict(re.findall(r'<symbol id="(\w+)"[^>]*><path d="([^"]+)"', SPRITE.read_text(encoding="utf-8")))
    sets = {
        "Feather": {p.stem: _inner_markup(p) for p in sorted(feather.glob("*.svg"))},
        "Lucide": {p.stem: _inner_markup(p) for p in sorted(lucide.glob("*.svg"))},
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto("about:blank")
        best = page.evaluate(MEASURE, {"ours": ours, "sets": sets})
        browser.close()
    output.write_text(json.dumps(best, ensure_ascii=False, indent=1), encoding="utf-8")
    for name in sorted(best):
        values = " ".join(f"{library} {best[name][library][1]:.2f} {best[name][library][0]}" for library in sets)
        print(f"{name:10} {values}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
