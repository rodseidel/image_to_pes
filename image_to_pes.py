#!/usr/bin/env python3
"""
image_to_pes.py - Converte uma imagem raster (JPG/PNG) em arquivo de bordado .PES
(Brother PES v6, abre no Wilcom Embroidery Studio).

Pipeline:
  1. Quantização de cores (k-means no espaço Lab) -> N cores "chapadas".
  2. Remoção do fundo (cor dominante na borda da imagem).
  3. Vetorização: cada cor vira um conjunto de polígonos (contorno externo + furos),
     suavizados e simplificados. Salvo também como SVG (útil para conferência ou
     para importar direto no Wilcom e usar o auto-digitize dele).
  4. Digitalização: cada polígono recebe pontos de preenchimento tatami
     (underlay de contorno + tatami cruzado, contorno em ponto corrido opcional).
  5. Escrita do .PES (pyembroidery) + PNG de pré-visualização dos pontos.

Uso básico:
  python image_to_pes.py <caminho/da/imagem.jpg>
  -> gera ./saida/<nome_da_imagem>.pes (+ _vetor.svg e _preview.png)
"""

import argparse
import math
import os
import sys

import cv2
import numpy as np
import pyembroidery
from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep


# --------------------------------------------------------------------------- #
# 1-2. Quantização de cores e detecção de fundo
# --------------------------------------------------------------------------- #
def quantize(img_bgr, n_colors, seed=42):
    """Retorna (labels HxW, centros BGR [n,3], imagem Lab, centros Lab [n,3])."""
    smooth = cv2.bilateralFilter(img_bgr, 7, 50, 50)
    lab = cv2.cvtColor(smooth, cv2.COLOR_BGR2LAB)
    data = lab.reshape(-1, 3).astype(np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 0.5)
    cv2.setRNGSeed(seed)
    _, labels, centers = cv2.kmeans(
        data, n_colors, None, criteria, 5, cv2.KMEANS_PP_CENTERS
    )
    labels = labels.reshape(img_bgr.shape[:2])
    centers_lab = np.clip(centers, 0, 255).astype(np.uint8).reshape(1, -1, 3)
    centers_bgr = cv2.cvtColor(centers_lab, cv2.COLOR_LAB2BGR).reshape(-1, 3)
    return labels, centers_bgr, lab, centers_lab.reshape(-1, 3)


def remove_halos(labels, lab, centers_lab, bg, radius_px):
    """
    Corrige pixels serrilhados de borda: um pixel na borda entre a cor D e o
    fundo tem cor intermediária (mistura D + fundo) e o k-means pode jogá-lo
    numa terceira cor C, criando um "halo" de C em volta de D. Se a cor do
    pixel é melhor explicada como mistura fundo->D do que pela cor C, e ele
    está a até `radius_px` de D, o pixel volta para D.
    """
    if bg is None:
        return labels, 0
    fg = [int(c) for c in np.unique(labels) if c != bg]
    if len(fg) < 2:
        return labels, 0
    lab = lab.astype(np.float32)
    cen = centers_lab.astype(np.float32)
    b = cen[bg]
    d_assigned = np.linalg.norm(lab - cen[labels], axis=2)
    best_res = np.full(labels.shape, np.inf, np.float32)
    best_d = np.full(labels.shape, -1, np.int32)
    for d in fg:
        seg = cen[d] - b
        t = np.clip(((lab - b) @ seg) / max(float(seg @ seg), 1e-6), 0.0, 1.0)
        res = np.linalg.norm(lab - (b + t[..., None] * seg), axis=2)
        near = cv2.distanceTransform(np.where(labels == d, 0, 255).astype(np.uint8),
                                     cv2.DIST_L2, 3) <= radius_px
        cand = near & (labels != d) & (labels != bg) & (res < d_assigned) & (res < best_res)
        best_res[cand] = res[cand]
        best_d[cand] = d
    fix = best_d >= 0
    out = labels.copy()
    out[fix] = best_d[fix]
    return out, int(fix.sum())


def background_label(labels):
    """Cor mais frequente na borda da imagem = fundo."""
    border = np.concatenate(
        [labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]]
    )
    return int(np.bincount(border.ravel()).argmax())


# --------------------------------------------------------------------------- #
# 3. Vetorização
# --------------------------------------------------------------------------- #
def mask_to_polygons(mask, smooth_px, epsilon_px, min_area_px):
    """Máscara binária -> lista de shapely Polygons (coordenadas em pixels)."""
    mask = mask.astype(np.uint8) * 255
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    if smooth_px > 0:
        k = int(smooth_px) * 2 + 1
        mask = cv2.GaussianBlur(mask, (k, k), 0)
        _, mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)

    found = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    contours, hierarchy = found[-2], found[-1]
    if hierarchy is None:
        return []
    hierarchy = hierarchy[0]

    def simplify(c):
        c = cv2.approxPolyDP(c, epsilon_px, True)
        return [(float(p[0][0]), float(p[0][1])) for p in c]

    polys = []
    for i, cnt in enumerate(contours):
        if hierarchy[i][3] != -1:  # é um furo; tratado pelo pai
            continue
        if cv2.contourArea(cnt) < min_area_px:
            continue
        shell = simplify(cnt)
        if len(shell) < 3:
            continue
        holes = []
        child = hierarchy[i][2]
        while child != -1:
            if cv2.contourArea(contours[child]) >= min_area_px:
                h = simplify(contours[child])
                if len(h) >= 3:
                    holes.append(h)
            child = hierarchy[child][0]
        poly = Polygon(shell, holes).buffer(0)
        if poly.is_empty:
            continue
        polys.extend(poly.geoms if isinstance(poly, MultiPolygon) else [poly])
    return polys


def content_bbox(mask):
    """Retângulo (x, y, w, h) que envolve o desenho, ignorando ruído/manchas pequenas."""
    m = cv2.morphologyEx(mask.astype(np.uint8) * 255, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    min_px = max(20, mask.size * 1e-5)
    boxes = [stats[i] for i in range(1, n) if stats[i][cv2.CC_STAT_AREA] >= min_px]
    if not boxes:
        return 0, 0, mask.shape[1], mask.shape[0]
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return int(x0), int(y0), int(x1 - x0), int(y1 - y0)


def write_svg(path, layers, width_mm, height_mm):
    """layers: lista de (hex_color, [Polygon em mm])."""
    def ring_d(coords):
        pts = list(coords)
        return "M " + " L ".join("%.3f %.3f" % (x, y) for x, y in pts) + " Z"

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="%.2fmm" height="%.2fmm" '
        'viewBox="0 0 %.3f %.3f">' % (width_mm, height_mm, width_mm, height_mm),
    ]
    for color, polys in layers:
        out.append('<g fill="%s" fill-rule="evenodd" stroke="none">' % color)
        for p in polys:
            d = ring_d(p.exterior.coords)
            for hole in p.interiors:
                d += " " + ring_d(hole.coords)
            out.append('<path d="%s"/>' % d)
        out.append("</g>")
    out.append("</svg>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


# --------------------------------------------------------------------------- #
# 4. Digitalização (geração de pontos)
# --------------------------------------------------------------------------- #
def _row_segments(poly, y, minx, maxx):
    line = LineString([(minx - 1, y), (maxx + 1, y)])
    inter = poly.intersection(line)
    segs = []
    geoms = getattr(inter, "geoms", [inter])
    for g in geoms:
        if g.geom_type == "LineString" and g.length > 1e-6:
            xs = [c[0] for c in g.coords]
            segs.append((min(xs), max(xs)))
    segs.sort()
    return segs


def _split_row(x0, x1, y, stitch_len, offset):
    """Pontos de uma linha de tatami de x0 a x1 (direção preservada)."""
    lo, hi = min(x0, x1), max(x0, x1)
    xs = [lo]
    # Pontos intermediários alinhados a uma grade deslocada (padrão tatami)
    k = math.floor((lo - offset) / stitch_len) + 1
    x = offset + k * stitch_len
    while x < hi - stitch_len * 0.3:
        if x > lo + stitch_len * 0.3:
            xs.append(x)
        x += stitch_len
    xs.append(hi)
    if x0 > x1:
        xs.reverse()
    return [(xv, y) for xv in xs]


def tatami_blocks(poly, angle_deg, spacing, stitch_len):
    """
    Gera blocos de preenchimento tatami para um polígono.
    Cada bloco é uma lista de pontos (mm) costurável sem saltos (zig-zag
    contínuo). Retorna lista de blocos.
    """
    if poly.is_empty or poly.area < spacing * spacing:
        return []
    origin = (0, 0)
    rp = affinity.rotate(poly, -angle_deg, origin=origin)
    minx, miny, maxx, maxy = rp.bounds
    # tolerância para a linha de ligação entre fileiras "raspar" a borda
    rp_tol = prep(rp.buffer(spacing * 0.5))

    def link_ok(block, y, x0, x1):
        """A ligação do fim da última fileira ao início da nova fica dentro da forma?"""
        py, px0, px1 = block["rows"][-1]
        if len(block["rows"]) % 2 == 1:  # última fileira foi esq->dir: termina em px1
            a, b = (px1, py), (x1, y)
        else:
            a, b = (px0, py), (x0, y)
        if math.hypot(a[0] - b[0], a[1] - b[1]) > stitch_len:
            return False
        return rp_tol.covers(LineString([a, b]))

    # Agrupa segmentos de linhas consecutivas que se sobrepõem em "blocos"
    blocks = []  # cada bloco: {"rows": [(y, x0, x1)], "last": row_idx}
    open_blocks = []
    row_idx = 0
    y = miny + spacing / 2.0
    while y < maxy:
        segs = _row_segments(rp, y, minx, maxx)
        new_open = []
        used = set()
        for (x0, x1) in segs:
            target = None
            for bi, b in enumerate(open_blocks):
                if bi in used:
                    continue
                _, px0, px1 = b["rows"][-1]
                # sobreposição com a linha anterior + ligação interna à forma
                if x0 <= px1 and x1 >= px0 and link_ok(b, y, x0, x1):
                    target = bi
                    break
            if target is None:
                b = {"rows": []}
                blocks.append(b)
            else:
                used.add(target)
                b = open_blocks[target]
            b["rows"].append((y, x0, x1))
            new_open.append(b)
        open_blocks = new_open
        row_idx += 1
        y += spacing

    result = []
    for b in blocks:
        pts = []
        for i, (yy, x0, x1) in enumerate(b["rows"]):
            # deslocamento tatami: 1/3 do comprimento por linha
            offset = (i % 3) * stitch_len / 3.0
            if i % 2 == 0:
                pts.extend(_split_row(x0, x1, yy, stitch_len, offset))
            else:
                pts.extend(_split_row(x1, x0, yy, stitch_len, offset))
        if len(pts) >= 2:
            # volta ao sistema original
            ca, sa = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
            pts = [(x * ca - y * sa, x * sa + y * ca) for x, y in pts]
            result.append(pts)
    return result


def run_ring(coords, stitch_len):
    """Ponto corrido ao longo de um anel (contorno)."""
    line = LineString(coords)
    n = max(2, int(math.ceil(line.length / stitch_len)))
    return [line.interpolate(i / n, normalized=True).coords[0] for i in range(n + 1)]


def ring_from(pts, cur):
    """Reordena um anel fechado para começar no vértice mais próximo de cur."""
    ring = pts[:-1] if len(pts) > 1 and pts[0] == pts[-1] else list(pts)
    if not ring:
        return []
    i = min(range(len(ring)), key=lambda k: (ring[k][0] - cur[0]) ** 2 + (ring[k][1] - cur[1]) ** 2)
    ring = ring[i:] + ring[:i]
    return ring + [ring[0]]


def render_preview(pattern, path, px_per_mm=10.0, thread_mm=0.4):
    """Prévia realista: cada ponto desenhado com a espessura de uma linha de bordado."""
    sts = [s for s in pattern.stitches if (s[2] & pyembroidery.COMMAND_MASK) == pyembroidery.STITCH]
    xs = [s[0] for s in sts]
    ys = [s[1] for s in sts]
    if not xs:
        return
    k = px_per_mm / 10.0  # unidades PES (0,1 mm) -> px
    pad = 20
    minx, miny = min(xs), min(ys)
    w = int((max(xs) - minx) * k) + 2 * pad
    h = int((max(ys) - miny) * k) + 2 * pad
    img = np.full((h, w, 3), 255, np.uint8)
    th = max(1, int(round(thread_mm * px_per_mm)))
    colors = []
    for t in pattern.threadlist:
        hx = t.hex_color().lstrip("#")
        r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
        colors.append(((b, g, r), (int(b * 0.6), int(g * 0.6), int(r * 0.6))))
    ci, prev = 0, None
    for x, y, cmd in pattern.stitches:
        c = cmd & pyembroidery.COMMAND_MASK
        if c == pyembroidery.STITCH:
            pt = (int((x - minx) * k) + pad, int((y - miny) * k) + pad)
            if prev is not None and ci < len(colors):
                cv2.line(img, prev, pt, colors[ci][1], th + 1, cv2.LINE_AA)  # sombra
                cv2.line(img, prev, pt, colors[ci][0], max(1, th - 1), cv2.LINE_AA)
            prev = pt
        elif c == pyembroidery.COLOR_CHANGE:
            ci += 1
            prev = None
        else:
            prev = None
    cv2.imwrite(path, img)


def order_blocks(blocks, start):
    """Ordena blocos pelo vizinho mais próximo, permitindo inverter o bloco."""
    remaining = list(blocks)
    ordered = []
    cur = start
    while remaining:
        best = None
        for i, b in enumerate(remaining):
            for rev in (False, True):
                p = b[-1] if rev else b[0]
                d = (p[0] - cur[0]) ** 2 + (p[1] - cur[1]) ** 2
                if best is None or d < best[0]:
                    best = (d, i, rev)
        _, i, rev = best
        b = remaining.pop(i)
        if rev:
            b = b[::-1]
        ordered.append(b)
        cur = b[-1]
    return ordered


# --------------------------------------------------------------------------- #
# 5. Montagem do padrão de bordado
# --------------------------------------------------------------------------- #
class PatternBuilder:
    def __init__(self, max_travel_mm):
        self.pattern = pyembroidery.EmbPattern()
        self.cur = None
        self.max_travel = max_travel_mm
        self.has_stitches = False

    @staticmethod
    def _u(v):  # mm -> unidades PES (0,1 mm)
        return v * 10.0

    def new_color(self, hex_color, name):
        if self.has_stitches:
            self.pattern.add_command(pyembroidery.TRIM)
            self.pattern.add_command(pyembroidery.COLOR_CHANGE)
        thread = pyembroidery.EmbThread()
        thread.set_hex_color(hex_color)
        thread.description = name
        self.pattern.add_thread(thread)
        self.cur = None

    def sew(self, pts):
        if not pts:
            return
        x, y = pts[0]
        if self.cur is None:
            self.pattern.add_stitch_absolute(pyembroidery.JUMP, self._u(x), self._u(y))
        else:
            d = math.hypot(x - self.cur[0], y - self.cur[1])
            if d > self.max_travel:
                self.pattern.add_command(pyembroidery.TRIM)
                self.pattern.add_stitch_absolute(pyembroidery.JUMP, self._u(x), self._u(y))
        for x, y in pts:
            self.pattern.add_stitch_absolute(pyembroidery.STITCH, self._u(x), self._u(y))
        self.cur = pts[-1]
        self.has_stitches = True

    def finish(self):
        self.pattern.add_command(pyembroidery.TRIM)
        self.pattern.add_command(pyembroidery.END)
        return self.pattern


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def bgr_to_hex(c):
    b, g, r = [int(v) for v in c]
    return "#%02x%02x%02x" % (r, g, b)


def luminance(c):
    b, g, r = [int(v) for v in c]
    return 0.299 * r + 0.587 * g + 0.114 * b


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Vetoriza uma imagem e gera arquivo de bordado .PES (Wilcom/Brother)."
    )
    p.add_argument("input", help="imagem de entrada (jpg/png): nome do arquivo dentro da pasta "
                                 "de entrada, ou caminho para um arquivo em outro lugar")
    p.add_argument("-i", "--input-dir", default="entrada",
                   help="pasta onde as imagens de entrada são procuradas (padrão: ./entrada)")
    p.add_argument("-d", "--output-dir", default="saida",
                   help="pasta de saída (padrão: ./saida). Arquivos gerados: "
                        "<nome_da_imagem>.pes, <nome_da_imagem>_vetor.svg, <nome_da_imagem>_preview.png")
    p.add_argument("-o", "--output",
                   help="caminho .pes explícito (opcional; sobrepõe --output-dir/nome automático)")
    p.add_argument("-c", "--colors", type=int, default=3,
                   help="nº de cores da quantização, INCLUINDO o fundo (padrão: 3)")
    p.add_argument("-w", "--width-mm", type=float, default=150.0,
                   help="largura final do desenho bordado em mm (margens vazias são recortadas; padrão: 150)")
    p.add_argument("-H", "--max-height-mm", type=float, default=150.0,
                   help="altura máxima do desenho em mm; se passar, reduz mantendo a proporção "
                        "(0 = sem limite; padrão: 150)")
    p.add_argument("--no-crop", action="store_true",
                   help="não recorta as margens vazias (aí -w vale para a imagem inteira)")
    p.add_argument("--min-size", type=int, default=2000,
                   help="amplia imagens cujo maior lado seja menor que isso em px (0 = não amplia; padrão: 2000)")
    p.add_argument("--keep-background", action="store_true",
                   help="não remove a cor de fundo (borda tudo)")
    p.add_argument("--angle", type=float, default=45.0,
                   help="ângulo do preenchimento tatami em graus (padrão: 45)")
    p.add_argument("--density", type=float, default=0.30,
                   help="espaçamento entre linhas do preenchimento em mm; menor = mais denso "
                        "(padrão: 0.30; faixa útil 0.25-0.45)")
    p.add_argument("--underlay-spacing", type=float, default=1.5,
                   help="espaçamento do underlay tatami em mm (padrão: 1.5)")
    p.add_argument("--stitch-len", type=float, default=3.0,
                   help="comprimento do ponto do preenchimento em mm (padrão: 3.0)")
    p.add_argument("--no-underlay", action="store_true",
                   help="desativa o underlay (base) sob o preenchimento")
    p.add_argument("--outline", action="store_true",
                   help="adiciona contorno em ponto corrido sobre cada forma")
    p.add_argument("--pull-comp", type=float, default=0.25,
                   help="compensação de repuxo: expande as formas em mm (padrão: 0.25)")
    p.add_argument("--min-area", type=float, default=0.2,
                   help="descarta formas/furos menores que esta área em mm² (padrão: 0.2)")
    p.add_argument("--smooth", type=float, default=2.0,
                   help="suavização das bordas em px antes de vetorizar (padrão: 2)")
    p.add_argument("--simplify", type=float, default=0.8,
                   help="tolerância da simplificação dos contornos em px (padrão: 0.8)")
    p.add_argument("--max-travel", type=float, default=1.0,
                   help="deslocamentos maiores que isso (mm) viram corte+salto (padrão: 1.0)")
    p.add_argument("--no-svg", action="store_true", help="não salva o SVG vetorizado")
    p.add_argument("--no-preview", action="store_true", help="não salva PNG de prévia")
    return p.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    # procura primeiro na pasta de entrada; se não achar, aceita o caminho como foi passado
    in_dir_path = os.path.join(a.input_dir, a.input)
    if os.path.isfile(in_dir_path):
        a.input = in_dir_path
    elif not os.path.isfile(a.input):
        sys.exit("Arquivo não encontrado: %s (procurado também em %s/)" % (a.input, a.input_dir))
    if a.output:
        base = os.path.splitext(a.output)[0]
    else:
        name = os.path.splitext(os.path.basename(a.input))[0]
        base = os.path.join(a.output_dir, name)
    out_pes = base + ".pes"
    out_dir = os.path.dirname(os.path.abspath(out_pes))
    os.makedirs(out_dir, exist_ok=True)

    img = cv2.imread(a.input, cv2.IMREAD_COLOR)
    if img is None:
        sys.exit("Não foi possível ler a imagem: %s" % a.input)
    h0, w0 = img.shape[:2]
    # imagens pequenas: amplia antes de vetorizar (contornos mais suaves)
    up = 1
    if a.min_size and max(h0, w0) < a.min_size:
        up = int(math.ceil(a.min_size / float(max(h0, w0))))
        img = cv2.resize(img, (w0 * up, h0 * up), interpolation=cv2.INTER_CUBIC)
    print("Imagem: %dx%d px%s" % (w0, h0, " (ampliada %dx para vetorizar)" % up if up > 1 else ""))

    # 1-2. Cores
    labels, centers, lab, centers_lab = quantize(img, a.colors)
    bg = None if a.keep_background else background_label(labels)
    labels, nh = remove_halos(labels, lab, centers_lab, bg, radius_px=2 * up + 1)
    if nh:
        print("Pixels de borda (halo) corrigidos: %d" % nh)

    # Recorte automático: -w passa a ser a largura do DESENHO, não da imagem
    if bg is not None and not a.no_crop:
        bx, by, bw, bh = content_bbox(labels != bg)
    else:
        bx, by, bw, bh = 0, 0, img.shape[1], img.shape[0]
    scale = a.width_mm / float(bw)  # mm por pixel
    if a.max_height_mm and bh * scale > a.max_height_mm:
        scale = a.max_height_mm / float(bh)
    width_mm, height_mm = bw * scale, bh * scale
    print("Bordado: %.1f x %.1f mm" % (width_mm, height_mm))
    color_ids = [i for i in range(len(centers)) if i != bg]
    color_ids.sort(key=lambda i: -luminance(centers[i]))  # claras primeiro, escuras por cima
    if bg is not None:
        print("Fundo removido: %s" % bgr_to_hex(centers[bg]))

    # 3. Vetorização
    min_area_px = a.min_area / (scale * scale)
    layers = []
    for ci in color_ids:
        polys_px = mask_to_polygons(labels == ci, a.smooth, a.simplify, min_area_px)
        polys = [affinity.scale(affinity.translate(p, -bx, -by), scale, scale, origin=(0, 0))
                 for p in polys_px]
        if a.pull_comp > 0:
            grown = unary_union([p.buffer(a.pull_comp, join_style=2) for p in polys])
            polys = list(grown.geoms) if hasattr(grown, "geoms") else [grown]
            polys = [p for p in polys if not p.is_empty]
        if polys:
            layers.append((bgr_to_hex(centers[ci]), polys))
            print("Cor %s: %d formas" % (bgr_to_hex(centers[ci]), len(polys)))

    if not layers:
        sys.exit("Nenhuma forma encontrada. Tente aumentar --colors ou usar --keep-background.")

    if not a.no_svg:
        write_svg(base + "_vetor.svg", layers, width_mm, height_mm)
        print("SVG vetorizado: %s_vetor.svg" % base)

    # 4-5. Pontos
    builder = PatternBuilder(a.max_travel)
    for n, (color, polys) in enumerate(layers, 1):
        builder.new_color(color, "Cor %d %s" % (n, color))
        # ordena as formas pela posição (esquerda->direita, cima->baixo aprox.)
        polys = sorted(polys, key=lambda p: (round(p.centroid.y / 20.0), p.centroid.x))
        start = builder.cur or (0.0, 0.0)
        for poly in polys:
            start = builder.cur or start
            if not a.no_underlay:
                # underlay 1: contorno interno (edge run) -> segura as bordas
                edge = poly.buffer(-0.4)
                for ip in getattr(edge, "geoms", [edge]):
                    if ip.is_empty or ip.area < 1.0:
                        continue
                    for ring in [ip.exterior] + list(ip.interiors):
                        builder.sew(ring_from(run_ring(ring.coords, 2.0), builder.cur or start))
                # underlay 2: tatami aberto cruzado (90° do preenchimento) -> corpo
                inner = poly.buffer(-0.3)
                seq = []
                for ip in getattr(inner, "geoms", [inner]):
                    if not ip.is_empty:
                        seq += tatami_blocks(ip, a.angle + 90, a.underlay_spacing, 3.5)
                for blk in order_blocks(seq, builder.cur or start):
                    builder.sew(blk)
            top = tatami_blocks(poly, a.angle, a.density, a.stitch_len)
            for blk in order_blocks(top, builder.cur or start):
                builder.sew(blk)
            if a.outline:
                builder.sew(run_ring(poly.exterior.coords, 2.0))
                for hole in poly.interiors:
                    builder.sew(run_ring(hole.coords, 2.0))
    pattern = builder.finish()

    pyembroidery.write_pes(pattern, out_pes, {"version": 6})
    stitches = sum(1 for s in pattern.stitches if (s[2] & pyembroidery.COMMAND_MASK) == pyembroidery.STITCH)
    print("PES gerado: %s (%d pontos, %d cores)" % (out_pes, stitches, len(layers)))

    if not a.no_preview:
        render_preview(pattern, base + "_preview.png")
        print("Prévia: %s_preview.png" % base)


if __name__ == "__main__":
    main()
