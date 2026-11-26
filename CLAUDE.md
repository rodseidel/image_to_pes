# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Single-script tool (`image_to_pes.py`) that converts a raster image (JPG/PNG) into a Brother **PES v6** embroidery file for Wilcom Embroidery Studio / Brother machines. User-facing docs (`README.md`, `ROTEIRO_EXECUCAO.md`), CLI help and console messages are in **Brazilian Portuguese**; keep new user-facing text in Portuguese.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r dependencies.txt          # numpy, opencv-python-headless, shapely>=2, pyembroidery==1.5.1

python image_to_pes.py imagem4.jpeg      # reads entrada/imagem4.jpeg -> saida/imagem4.{pes,_vetor.svg,_preview.png}
python image_to_pes.py --help
```

There are no tests, linter or build step. To validate a change, run the script on the sample images in `entrada/` (write to a scratch dir with `-d` to avoid overwriting committed outputs in `saida/`) and inspect the `_preview.png`.

Input lookup: the positional `input` is first resolved as `<input-dir>/<input>` (default `entrada/`), then as a plain path.

## Architecture

`main()` runs a linear pipeline; each stage is a top-level function:

1. **Upscale** images whose longest side < `--min-size` (2000 px) by an integer factor `up`. Pixel-based params (halo radius) scale with `up`.
2. **Color** — `quantize()` (bilateral filter + k-means in Lab, fixed seed) → `background_label()` (most frequent label on image border) → `remove_halos()` (reassigns anti-aliased edge pixels that form a ring of a third color).
3. **Crop/scale** — `content_bbox()` of non-background pixels; `-w` is the width of the *design*, not the image. Scale is capped by `-H`.
4. **Vectorize** — `mask_to_polygons()` per color (OpenCV contours with hierarchy → shapely polygons with holes). Polygons are translated/scaled to **mm**, then grown by `--pull-comp` via `buffer` + `unary_union` (this can merge shapes that are close together). Colors are ordered light → dark so dark colors stitch on top.
5. **Digitize** — per polygon: edge-run underlay (`buffer(-0.4)`), cross-hatched tatami underlay at `angle+90`, then tatami fill (`tatami_blocks()` scanline rows split into stitches with staggered offsets). `order_blocks()` greedily orders blocks to minimize travel.
6. **Output** — `PatternBuilder` accumulates stitches in mm and converts to PES units (0.1 mm); moves longer than `--max-travel` become trim + jump. `pyembroidery.write_pes(..., {"version": 6})`, plus `write_svg()` and `render_preview()` (draws stitches with real thread width).

Units: image stages work in pixels, everything from step 4 on is in mm, and only `PatternBuilder`/`render_preview` deal with 0.1 mm PES units.

## Known limitation

Low-resolution sources or images with non-flat (gradient/photographed) backgrounds can make k-means classify anti-aliased edges as foreground, merging nearby glyphs. The workaround used for `entrada/imagem5.png` was to pre-process the image into a clean flat mask outside the script (thresholding darkness vs. a local background estimate) and run with `-c 2 --pull-comp 0.1`.
