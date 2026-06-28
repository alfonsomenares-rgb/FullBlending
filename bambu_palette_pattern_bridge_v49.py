#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bambu Palette Pattern Bridge v49

Interfaz visual para elegir recetas de color por patrón:
- patrón exterior de 3 capas (ej: CYC)
- patrón interior de 3 capas (ej: KWK)
- color percibido calculado con modelo de translucidez de PLA opaco

Esta versión usa el motor de conversión v28 y añade un mapa visual continuo tipo paleta: las recetas 4x4x3 capas se ven como una grilla de color y al hacer clic aparece el patrón.
Incluye salida Bambu o Snapmaker U1.

v46: para Snapmaker la salida segura es G-code plano (.gcode), no contenedor .gcode.3mf.
v47: filtro de colores repetidos/visualmente iguales activado por defecto, usando CIE L*a*b* (DeltaE) además de HEX exacto.
v48: autoasignación física antes de convertir y limpieza Snapmaker más agresiva.
v49: corrige patrones exteriores reconociendo ;LAYER_CHANGE / ;Z de Snapmaker-Orca antes del motor de conversión.
"""
from __future__ import annotations

import argparse
import itertools
import math
import colorsys
import re
import sys
import copy
import zipfile
import tempfile
import shutil
import json
import hashlib
import csv
import io
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser

try:
    import bambu_palette_pattern_bridge_v28 as core
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "No pude importar bambu_palette_pattern_bridge_v28.py. "
        "Deja v34 y v28 en la misma carpeta. Error: %s" % exc
    )

SYMBOLS_ALL = "CMYKW"
SYMBOL_LABELS = {
    "C": "Cian",
    "M": "Magenta",
    "Y": "Amarillo",
    "K": "Negro",
    "W": "Blanco",
}
DEFAULT_HEX = {
    "C": "#00FFFF",
    "M": "#FF00FF",
    "Y": "#FFFF00",
    "K": "#000000",
    "W": "#FFFFFF",
}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _norm_hex(value: str) -> str:
    h = core.normalize_hex(value or "")
    return h if h else "#000000"


# v45: distancia perceptual aproximada en CIELAB (DeltaE76), no distancia RGB.
# RGB solo describe un estímulo de pantalla; para elegir recetas visualmente conviene
# comparar en un espacio aproximadamente uniforme para percepción humana.
def _rgb_to_xyz(rgb: Tuple[int, int, int]) -> Tuple[float, float, float]:
    r, g, b = [_srgb_to_linear(v) for v in rgb]
    # Matriz sRGB D65 -> XYZ
    x = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041
    return (x, y, z)

def _xyz_to_lab(xyz: Tuple[float, float, float]) -> Tuple[float, float, float]:
    # D65 reference white
    xr, yr, zr = xyz[0] / 0.95047, xyz[1] / 1.00000, xyz[2] / 1.08883
    def f(t: float) -> float:
        return t ** (1.0 / 3.0) if t > 0.008856 else (7.787 * t + 16.0 / 116.0)
    fx, fy, fz = f(xr), f(yr), f(zr)
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))

def _rgb_to_lab(rgb: Tuple[int, int, int]) -> Tuple[float, float, float]:
    return _xyz_to_lab(_rgb_to_xyz(rgb))

def _rgb_distance(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> float:
    la, lb = _rgb_to_lab(a), _rgb_to_lab(b)
    return math.sqrt((la[0] - lb[0]) ** 2 + (la[1] - lb[1]) ** 2 + (la[2] - lb[2]) ** 2)


# -----------------------------------------------------------------------------
# v36: modelo de mezcla sustractivo / absorción para PLA opaco
# -----------------------------------------------------------------------------
# En versiones anteriores el color percibido podía calcularse como mezcla RGB.
# Para patrones CMY esto es incorrecto: C+M+Y debe oscurecerse por absorción,
# no aclararse por promedio aditivo. Todas las paletas visuales, auto-asignación
# y colores asignados llaman a perceived_recipe_rgb(), que usa densidad óptica.

_MIN_TRANSMITTANCE_RGB = (0.012, 0.010, 0.008)  # v41: piso más bajo para permitir cafés/negros reales  # piso por canal: C+M+Y tiende a café/oscuro, no a promedio RGB


def _srgb_to_linear(v: int) -> float:
    x = max(0.0, min(1.0, v / 255.0))
    if x <= 0.04045:
        return x / 12.92
    return ((x + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(x: float) -> int:
    x = max(0.0, min(1.0, x))
    if x <= 0.0031308:
        y = 12.92 * x
    else:
        y = 1.055 * (x ** (1 / 2.4)) - 0.055
    return int(round(max(0.0, min(1.0, y)) * 255))


def _rgb_to_optical_density(rgb: Tuple[int, int, int], strength: float = 1.0) -> Tuple[float, float, float]:
    """Convierte un color RGB de filamento a densidad óptica.

    La mezcla sustractiva suma densidades ópticas. Usamos el RGB del filamento
    como transmitancia/reflexión aproximada y trabajamos en espacio lineal.
    """
    out = []
    for idx, ch in enumerate(rgb):
        t = max(_MIN_TRANSMITTANCE_RGB[idx], _srgb_to_linear(ch))
        out.append(-math.log(t) * strength)
    return (out[0], out[1], out[2])


def _optical_density_to_rgb(od: Tuple[float, float, float]) -> Tuple[int, int, int]:
    vals = []
    for d in od:
        t = math.exp(-max(0.0, d))
        vals.append(_linear_to_srgb(t))
    return (vals[0], vals[1], vals[2])


def _od_add(a: Tuple[float, float, float], b: Tuple[float, float, float], wb: float = 1.0) -> Tuple[float, float, float]:
    return (a[0] + b[0] * wb, a[1] + b[1] * wb, a[2] + b[2] * wb)


def _od_avg(items: List[Tuple[float, float, float]]) -> Tuple[float, float, float]:
    if not items:
        return (0.0, 0.0, 0.0)
    n = len(items)
    return (sum(x[0] for x in items) / n, sum(x[1] for x in items) / n, sum(x[2] for x in items) / n)


def _symbol_entry(sym: str, meta: core.GcodeMetadata, opts: core.ConvertOptions):
    return core._v28_symbol_entry(sym, meta, opts)


def _symbol_density_boost(sym: str, rgb: Tuple[int, int, int]) -> float:
    """Ajuste suave por tipo de base.

    Negro/base oscura absorbe más; blanco casi no absorbe; grises absorben medio.
    Esto mantiene el comportamiento de filamento opaco sin volver todo negro.

    v43: el negro sigue pudiendo crear gama oscura cuando está fuera o cuando se
    busca un color oscuro, pero su efecto como respaldo detrás de blanco se corrige
    de forma contextual en perceived_recipe_rgb().
    """
    lum = core.luminance(rgb)
    rr, gg, bb = [v / 255.0 for v in rgb]
    _h, sat, _v = colorsys.rgb_to_hsv(rr, gg, bb)
    if sym == "W" or (lum > 0.82 and sat < 0.18):
        return 0.25
    if sym == "K" or lum < 0.12:
        return 2.35
    if sat < 0.16:
        return 0.90
    return 1.0


def _symbol_lum_sat(rgb: Tuple[int, int, int]) -> Tuple[float, float]:
    lum = core.luminance(rgb)
    rr, gg, bb = [v / 255.0 for v in rgb]
    _h, sat, _v = colorsys.rgb_to_hsv(rr, gg, bb)
    return lum, sat


def _is_white_like(sym: str, rgb: Tuple[int, int, int]) -> bool:
    lum, sat = _symbol_lum_sat(rgb)
    return sym == "W" or (lum >= 0.78 and sat <= 0.22)


def _is_black_like(sym: str, rgb: Tuple[int, int, int]) -> bool:
    lum, sat = _symbol_lum_sat(rgb)
    return sym == "K" or (lum <= 0.16 and sat <= 0.30)


def _is_neutral_wk_recipe(symbols: str) -> bool:
    return all(ch in "WK" for ch in (symbols or "").upper())


def _protect_white_over_black_gray(
    rgb: Tuple[int, int, int],
    outer_symbols: str,
    inner_symbols: str,
    outer_rgbs: List[Tuple[int, int, int]],
    inner_rgbs: List[Tuple[int, int, int]],
) -> Tuple[int, int, int]:
    """v44: protección neutra W/K más realista.

    En patrones blanco/negro, la mezcla NO debe comportarse como tinta negra
    atravesando todo el material. Si el exterior tiene mayoría blanco, incluso
    con un patrón visible BNB/WKW, el resultado debe ser gris claro o gris medio,
    no negro. Esta regla solo se aplica a recetas neutras W/K para no romper CMY.
    """
    if not (_is_neutral_wk_recipe(outer_symbols) and _is_neutral_wk_recipe(inner_symbols)):
        return rgb

    n_outer = max(1, len(outer_symbols))
    n_inner = max(1, len(inner_symbols))
    outer_white = sum(1 for s, c in zip(outer_symbols, outer_rgbs) if _is_white_like(s, c)) / n_outer
    outer_black = sum(1 for s, c in zip(outer_symbols, outer_rgbs) if _is_black_like(s, c)) / n_outer
    inner_white = sum(1 for s, c in zip(inner_symbols, inner_rgbs) if _is_white_like(s, c)) / n_inner
    inner_black = sum(1 for s, c in zip(inner_symbols, inner_rgbs) if _is_black_like(s, c)) / n_inner

    # Si el exterior es mayoritariamente blanco, el negro interior o 1/3 de negro
    # exterior no puede colapsar el resultado a negro. Ej.: BNB/WKW exterior.
    if outer_white >= 0.66:
        if inner_white >= 0.66:
            floor = 198
        elif inner_white >= 0.33:
            floor = 172
        elif inner_black > 0.0:
            floor = 145
        else:
            floor = 188
        # Si el negro también está en la piel exterior, permitimos un gris un poco
        # más bajo, pero no negro profundo.
        if outer_black >= 0.33:
            floor = min(floor, 176) if inner_white >= 0.33 else min(floor, 150)
        return (max(rgb[0], floor), max(rgb[1], floor), max(rgb[2], floor))

    # Si el patrón es 50/50 blanco/negro exterior, debe tender a gris medio, no negro.
    if outer_white >= 0.50 and outer_black <= 0.50:
        floor = 150 if inner_white >= 0.50 else 128
        return (max(rgb[0], floor), max(rgb[1], floor), max(rgb[2], floor))

    return rgb


def _backer_strength_for_symbol(sym: str, entry, opts: core.ConvertOptions) -> float:
    # Usa el modelo SUNLU/PLA opaco del motor cuando está disponible.
    try:
        base = float(core._sunlu_opaque_backer_strength(entry, opts, 0))
    except Exception:
        base = 0.18
    rgb = entry.rgb
    lum = core.luminance(rgb)
    rr, gg, bb = [v / 255.0 for v in rgb]
    _h, sat, _v = colorsys.rgb_to_hsv(rr, gg, bb)
    if sym == "K" or lum < 0.12:
        base *= 2.10
    elif sym == "W" or (lum > 0.82 and sat < 0.18):
        base *= 0.75
    elif sat < 0.16:
        base *= 1.05
    else:
        base *= 0.95
    return max(0.02, min(0.88, base))


def _symbol_translucency(sym: str, opts: core.ConvertOptions) -> float:
    """Translucidez 0..1 configurable por símbolo.

    0 = filamento muy opaco: casi solo se ve la pared exterior.
    1 = filamento muy translúcido: el respaldo interior y capas cercanas influyen más.
    """
    d = getattr(opts, "v45_symbol_translucency", None)
    if isinstance(d, dict) and sym in d:
        try:
            return _clamp01(float(d[sym]))
        except Exception:
            pass
    defaults = {"C": 0.34, "M": 0.34, "Y": 0.34, "K": 0.12, "W": 0.22}
    return defaults.get(sym, 0.25)


def _lin_rgb(rgb: Tuple[int, int, int]) -> Tuple[float, float, float]:
    return (_srgb_to_linear(rgb[0]), _srgb_to_linear(rgb[1]), _srgb_to_linear(rgb[2]))


def _rgb_from_lin(v: Tuple[float, float, float]) -> Tuple[int, int, int]:
    return (_linear_to_srgb(v[0]), _linear_to_srgb(v[1]), _linear_to_srgb(v[2]))


def _v_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _v_mul(a, b):
    return (a[0] * b[0], a[1] * b[1], a[2] * b[2])


def _v_lerp(a, b, t: float):
    t = _clamp01(t)
    return (a[0] * (1 - t) + b[0] * t, a[1] * (1 - t) + b[1] * t, a[2] * (1 - t) + b[2] * t)


def _weighted_arithmetic_lin(items: List[Tuple[float, float, float]], weights: List[float]) -> Tuple[float, float, float]:
    total = max(1e-9, sum(weights))
    return (
        sum(c[0] * w for c, w in zip(items, weights)) / total,
        sum(c[1] * w for c, w in zip(items, weights)) / total,
        sum(c[2] * w for c, w in zip(items, weights)) / total,
    )


def _weighted_geometric_lin(items: List[Tuple[float, float, float]], weights: List[float]) -> Tuple[float, float, float]:
    total = max(1e-9, sum(weights))
    eps = 0.0025
    out = []
    for ch in range(3):
        out.append(math.exp(sum(math.log(max(eps, c[ch])) * w for c, w in zip(items, weights)) / total))
    return (out[0], out[1], out[2])


def _recipe_symbol_fraction(symbols: str, wanted: str) -> float:
    if not symbols:
        return 0.0
    wanted_set = set(wanted)
    return sum(1 for s in symbols if s in wanted_set) / max(1, len(symbols))


def _has_cmy_triad(symbols: str) -> bool:
    ss = set(symbols)
    return all(x in ss for x in "CMY")


def perceived_recipe_rgb(outer_symbols: str, inner_symbols: str, meta: core.GcodeMetadata, opts: core.ConvertOptions) -> Tuple[int, int, int]:
    """Color percibido v45: modelo físico unificado y calibrable.

    Base científica aproximada:
    - sRGB se linealiza antes de mezclar.
    - La influencia de la pared interior se trata como luz filtrada por la pared exterior
      (idea Beer-Lambert / absorción: la transmitancia se multiplica, no se promedia en RGB).
    - La mezcla entre capas del patrón usa una combinación de promedio espacial y promedio
      geométrico/substractivo. Si el material es más translúcido, sube la parte sustractiva;
      si es más opaco, domina el promedio espacial de las bandas visibles.
    - Blanco y negro no tienen reglas especiales duras: el resultado sale de la translucidez
      de cada filamento. Por defecto W y K son poco translúcidos, por eso WKW/BNB no colapsa
      a negro, pero K exterior sigue viéndose oscuro.
    """
    outer_symbols = (outer_symbols or "").upper().strip()
    inner_symbols = (inner_symbols or "").upper().strip()
    if len(outer_symbols) != len(inner_symbols) or len(outer_symbols) not in (1, 2, 3):
        return (0, 0, 0)

    layer_colors: List[Tuple[float, float, float]] = []
    layer_weights: List[float] = []
    translucencies: List[float] = []

    for i, (osym, isym) in enumerate(zip(outer_symbols, inner_symbols)):
        oe = _symbol_entry(osym, meta, opts)
        ie = _symbol_entry(isym, meta, opts)
        ro = _lin_rgb(oe.rgb)
        ri = _lin_rgb(ie.rgb)
        to = _symbol_translucency(osym, opts)
        ti = _symbol_translucency(isym, opts)
        translucencies.append(to)

        # La pared exterior manda. La interior se ve a través de la exterior como una
        # contribución filtrada, no como promedio directo. El factor máximo es bajo
        # porque una pared exterior de PLA opaco/semiopaco bloquea bastante.
        back_visibility = _clamp01(0.04 + 0.42 * to + 0.06 * ti)

        # Blanco y negro suelen tener pigmentos muy dispersantes/opacos. Si el exterior
        # es blanco o negro, reducimos lo que atraviesa para evitar que el respaldo
        # destruya el color frontal. Esto no es un filtro arbitrario: representa alta
        # dispersión superficial de filamentos con TiO2/blanco o negro cargado.
        if _is_white_like(osym, oe.rgb):
            back_visibility *= 0.80
        if _is_black_like(osym, oe.rgb):
            back_visibility *= 0.22
        if _is_black_like(isym, ie.rgb) and _is_white_like(osym, oe.rgb):
            back_visibility *= 0.90

        # Modelo de capa: superficie frontal + respaldo interior filtrado por la propia
        # transmitancia/color del exterior. La multiplicación en lineal da el carácter
        # sustractivo CMY: C filtra rojo, M filtra verde, Y filtra azul.
        through = _v_mul(ro, ri)
        layer = _v_lerp(ro, through, back_visibility)
        layer_colors.append(layer)

        # Todas las capas del patrón tienen el mismo porcentaje geométrico: 2 capas = 50/50,
        # 3 capas = 33/33/33. Se deja el hook por si luego se calibra altura de capa variable.
        layer_weights.append(1.0)

    arithmetic = _weighted_arithmetic_lin(layer_colors, layer_weights)
    geometric = _weighted_geometric_lin(layer_colors, layer_weights)

    avg_t = sum(translucencies) / max(1, len(translucencies))
    all_symbols = outer_symbols + inner_symbols
    neutral_wk = all(ch in "WK" for ch in all_symbols)

    # Cuánta mezcla sustractiva vertical se percibe entre bandas/capas:
    # - con filamentos opacos domina el promedio espacial;
    # - con filamentos translúcidos crece la mezcla geométrica/substractiva;
    # - si están C, M e Y, se refuerza porque la absorción complementaria conjunta
    #   tiende a café/gris oscuro, no a RGB claro.
    subtractive_mix = 0.10 + 0.58 * avg_t
    if _has_cmy_triad(all_symbols):
        subtractive_mix += 0.22
    if neutral_wk:
        # W/K es principalmente mezcla espacial de bandas: 2/3 blanco + 1/3 negro
        # debe ser gris, no negro. La translucidez ajustable aún puede hacerlo más oscuro.
        subtractive_mix *= 0.34
    if _recipe_symbol_fraction(outer_symbols, "W") >= 0.50:
        subtractive_mix *= 0.55
    if _recipe_symbol_fraction(outer_symbols, "K") >= 0.50:
        subtractive_mix = max(subtractive_mix, 0.42)
    subtractive_mix = _clamp01(subtractive_mix)

    final_lin = _v_lerp(arithmetic, geometric, subtractive_mix)

    # Si hay CMY simultáneo, agregamos una pequeña pérdida de luminosidad física por
    # absorción acumulada. Es suave y depende de translucidez; no afecta W/K neutro.
    if _has_cmy_triad(all_symbols) and not neutral_wk:
        darken = 1.0 - _clamp01(0.10 + 0.24 * avg_t)
        # Sin espectrofotómetro no conocemos la curva real de absorción de cada PLA.
        # Para CMY ideal de pantalla, C+M+Y daría gris; en pigmentos/filamentos reales
        # hay absorciones secundarias y scattering, por eso se aproxima hacia café:
        # conserva más rojo, baja verde y baja más azul.
        final_lin = (final_lin[0] * darken * 0.98, final_lin[1] * darken * 0.82, final_lin[2] * darken * 0.62)

    return _rgb_from_lin(final_lin)



# -----------------------------------------------------------------------------
# v44: soporte real para recetas de 2 capas en el motor v28
# -----------------------------------------------------------------------------

def _v44_parse_recipe_overrides(text: str) -> Dict[int, Tuple[str, str]]:
    """Lee recetas 2 o 3 capas. Ej.: 5:WK:KW o 5:CYC:KWK."""
    result: Dict[int, Tuple[str, str]] = {}
    if not text:
        return result
    for raw in text.replace("|", "\n").splitlines():
        line = raw.strip().upper()
        if not line or line.startswith("#"):
            continue
        if not any(ch in line for ch in SYMBOLS_ALL):
            continue
        m = re.match(r"^\s*(\d+)\s*[:=,;\s]+([CMYKW]{2,3})\s*[:/|,;\s]+([CMYKW]{2,3})\s*$", line)
        if not m:
            raise ValueError(f"Receta v44 inválida: '{raw}'. Usa 5:WK:KW o 5:CYC:KWK")
        outer = m.group(2)
        inner = m.group(3)
        if len(outer) != len(inner):
            raise ValueError(f"Receta v44 inválida: '{raw}'. Exterior e interior deben tener la misma cantidad de capas.")
        result[core.human_to_tool(int(m.group(1)))] = (outer, inner)
    return result


def _v44_repair_inner_against_outer(outer_symbols: str, inner_symbols: str, available: List[str]) -> str:
    outer_symbols = outer_symbols.upper()
    inner = list(inner_symbols.upper())
    pool = [s for s in available if s in SYMBOLS_ALL]
    if not pool:
        return "".join(inner)
    for i in range(min(len(outer_symbols), len(inner))):
        if inner[i] == outer_symbols[i] and len(set(pool)) > 1:
            for cand in pool:
                if cand != outer_symbols[i] and (i == 0 or cand != inner[i - 1]):
                    inner[i] = cand
                    break
    return "".join(inner)


def _v44_recipe_plan_from_symbols(
    source_tool: int,
    outer_symbols: str,
    inner_symbols: str,
    meta: core.GcodeMetadata,
    opts: core.ConvertOptions,
    note_extra: str = "",
):
    """Plan v44 compatible con recetas 2 capas y 3 capas.

    A diferencia del validador v28 original, una receta manual puede usar K en
    exterior si K está activo. Esto es necesario para patrones neutros como
    BNB/WKW, donde 1/3 de negro exterior no debe interpretarse como negro total.
    """
    outer_symbols = (outer_symbols or "").upper().strip()
    inner_symbols = (inner_symbols or "").upper().strip()
    if len(outer_symbols) != len(inner_symbols) or len(outer_symbols) not in (2, 3):
        raise ValueError(
            f"Patrón inválido para filamento {core.tool_to_human(source_tool)}: "
            f"usa 2 o 3 capas con igual largo, ej. WK:KW o CYC:KWK"
        )
    if any(s not in SYMBOLS_ALL for s in outer_symbols + inner_symbols):
        raise ValueError(f"Receta usa símbolos no permitidos: {outer_symbols}:{inner_symbols}")
    if not getattr(opts, "recipe_use_white", True) and "W" in (outer_symbols + inner_symbols):
        raise ValueError("La receta usa W/blanco, pero el blanco está desactivado.")
    if not getattr(opts, "recipe_use_black", True) and "K" in (outer_symbols + inner_symbols):
        raise ValueError("La receta usa K/negro, pero el negro está desactivado.")

    active = ["C", "M", "Y"]
    if getattr(opts, "recipe_use_white", True):
        active.append("W")
    if getattr(opts, "recipe_use_black", True):
        active.append("K")
    if bool(getattr(opts, "recipe_avoid_same_outer_inner", True)):
        inner_symbols = _v44_repair_inner_against_outer(outer_symbols, inner_symbols, active)

    mapping = core._v28_symbol_tool_map(opts)
    outer_tools = [mapping[s] for s in outer_symbols]
    inner_tools = [mapping[s] for s in inner_symbols]
    perceived_hex = core.rgb_to_hex(perceived_recipe_rgb(outer_symbols, inner_symbols, meta, opts))
    target_hex = meta.filament_colors[source_tool] if 0 <= source_tool < len(meta.filament_colors) else None
    target_rgb = core.hex_to_rgb(target_hex) if target_hex else perceived_recipe_rgb(outer_symbols, inner_symbols, meta, opts)
    note = (
        f"v44 recipe-library: exterior{len(outer_symbols)}={outer_symbols}, "
        f"interior{len(inner_symbols)}={inner_symbols}, percibido≈{perceived_hex}; "
        f"receta {'50/50 de 2 capas' if len(outer_symbols)==2 else '33/33/33 de 3 capas'}. "
        + note_extra
    )
    return core.Plan(
        source_tool=source_tool,
        outer_tool=outer_tools[0],
        inner_tool=inner_tools[0],
        infill_tool=inner_tools[0],
        surface_tool=outer_tools[0],
        note=note,
        inner_pattern=inner_tools,
        outer_pattern=outer_tools,
        wall_cycle=None,
        surface_cycle=outer_tools,
        wall_count=opts.wall_loops or meta.wall_loops or 3,
        target_lum=core.luminance(target_rgb),
        dark_backer_tool=None,
        dark_backer_cycle=None,
        safe_dark_pattern=False,
        visible_compensation_used=True,
        visible_compensation_hex=perceived_hex,
        visible_compensation_backer_tool=inner_tools[0],
    )

def _read_target_rgb(meta: core.GcodeMetadata, tool: int) -> Tuple[int, int, int]:
    if 0 <= tool < len(meta.filament_colors) and meta.filament_colors[tool]:
        return core.hex_to_rgb(meta.filament_colors[tool] or "#000000")
    # fallback estable si el marcador no tiene HEX
    return core.hex_to_rgb(core._default_palette_color(tool))


def _read_target_hex(meta: core.GcodeMetadata, tool: int) -> str:
    if 0 <= tool < len(meta.filament_colors) and meta.filament_colors[tool]:
        return meta.filament_colors[tool] or core._default_palette_color(tool)
    return core._default_palette_color(tool)


def _tool_color(meta: core.GcodeMetadata, tool: int) -> str:
    if 0 <= tool < len(meta.filament_colors) and meta.filament_colors[tool]:
        return meta.filament_colors[tool] or core._default_palette_color(tool)
    return core._default_palette_color(tool)


class Recipe:
    __slots__ = ("outer", "inner", "hex", "rgb", "dist", "has_same_layer", "has_k_outer")

    def __init__(self, outer: str, inner: str, hx: str, rgb: Tuple[int, int, int], dist: float):
        self.outer = outer
        self.inner = inner
        self.hex = hx
        self.rgb = rgb
        self.dist = dist
        self.has_same_layer = any(outer[i] == inner[i] for i in range(min(len(outer), len(inner))))
        self.has_k_outer = "K" in outer

    @property
    def key(self) -> str:
        return f"{self.outer}:{self.inner}"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Bambu Palette Pattern Bridge v49 - patrones exteriores por capa corregidos")
        self.root.geometry("1320x820")

        self.meta: Optional[core.GcodeMetadata] = None
        self.recipes: List[Recipe] = []
        self.row_recipe: Dict[int, Recipe] = {}
        self.layer_row_recipe: Dict[int, Recipe] = {}
        self.layer_cell_records: List[Tuple[str, str, str]] = []
        self.layer_combo_recipes: List[Recipe] = []
        self.map2d_recipes: List[Recipe] = []
        self.map2d_cell_recipe: Dict[int, Recipe] = {}
        self.map2d_cols: int = 64
        self.map2d_cell: int = 13
        self.selected_recipe: Optional[Recipe] = None
        self.assignments: Dict[int, Recipe] = {}
        self.assignment_row_vars: Dict[int, Dict[str, object]] = {}

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.output_machine_var = tk.StringVar(value="bambu")
        self.process_from_var = tk.IntVar(value=5)
        self.wall_loops_var = tk.IntVar(value=3)
        self.active_symbols_var = tk.StringVar(value="CMYK")
        self.base_count_var = tk.IntVar(value=4)
        self.allow_k_outer_dark_var = tk.DoubleVar(value=0.0)
        self.avoid_same_var = tk.BooleanVar(value=False)
        self.sort_by_target_var = tk.BooleanVar(value=True)
        self.show_same_var = tk.BooleanVar(value=True)
        self.show_k_outer_var = tk.BooleanVar(value=True)
        self.filter_same_hex_var = tk.BooleanVar(value=True)
        self.duplicate_deltae_var = tk.DoubleVar(value=1.8)
        # v41: la paleta visual puede mostrar gama completa, incluyendo negro exterior,
        # aunque el auto/usuario decida después si conviene usarlo.
        self.palette_full_gamut_var = tk.BooleanVar(value=True)
        # v41: permitir gama oscura real. Para colores oscuros no conviene filtrar K/K ni repeticiones,
        # porque esas recetas son justamente las que generan negros y cafés profundos.
        self.dark_expand_var = tk.BooleanVar(value=False)
        self.dark_threshold_var = tk.DoubleVar(value=0.55)
        self.force_dark_candidates_var = tk.BooleanVar(value=False)
        self.include_two_layer_var = tk.BooleanVar(value=True)
        self.limit_var = tk.IntVar(value=0)  # 0 = todos
        self.selected_marker_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Carga un G-code o .gcode.3mf para empezar.")
        self.manual_text = tk.StringVar(value="")

        self.symbol_filament_vars: Dict[str, tk.IntVar] = {
            "C": tk.IntVar(value=1),
            "M": tk.IntVar(value=2),
            "Y": tk.IntVar(value=3),
            "K": tk.IntVar(value=4),
            "W": tk.IntVar(value=5),
        }
        self.symbol_hex_vars: Dict[str, tk.StringVar] = {s: tk.StringVar(value=DEFAULT_HEX[s]) for s in SYMBOLS_ALL}
        # v45: translucidez calibrable por filamento/símbolo. 0 = muy opaco, 1 = muy translúcido.
        # Defaults pensados para PLA opaco/semiopaco: W y K suelen dispersar/bloquear más luz.
        self.symbol_translucency_vars: Dict[str, tk.DoubleVar] = {
            "C": tk.DoubleVar(value=0.34),
            "M": tk.DoubleVar(value=0.34),
            "Y": tk.DoubleVar(value=0.34),
            "K": tk.DoubleVar(value=0.12),
            "W": tk.DoubleVar(value=0.22),
        }

        self._build_ui()

    # ---------------- UI ----------------
    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        files = ttk.LabelFrame(outer, text="Archivos", padding=8)
        files.pack(fill="x")
        ttk.Label(files, text="Entrada:").grid(row=0, column=0, sticky="w")
        ttk.Entry(files, textvariable=self.input_var).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(files, text="Buscar", command=self.browse_input).grid(row=0, column=2)
        ttk.Button(files, text="Actualizar metadata", command=self.refresh_metadata).grid(row=0, column=3, padx=5)
        ttk.Label(files, text="Salida:").grid(row=1, column=0, sticky="w")
        ttk.Entry(files, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", padx=5)
        ttk.Button(files, text="Guardar como", command=self.browse_output).grid(row=1, column=2)
        files.columnconfigure(1, weight=1)

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True, pady=(8, 0))

        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        right = ttk.Frame(body)
        right.pack(side="right", fill="both", expand=True, padx=(8, 0))

        opts = ttk.LabelFrame(left, text="Configuración", padding=8)
        opts.pack(fill="x")
        ttk.Label(opts, text="Salida G-code:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(opts, textvariable=self.output_machine_var, values=["bambu", "snapmaker"], width=12, state="readonly").grid(row=0, column=1, sticky="w")
        ttk.Label(opts, text="Marcadores desde filamento:").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(opts, from_=1, to=32, textvariable=self.process_from_var, width=6).grid(row=1, column=1, sticky="w")
        ttk.Label(opts, text="Paredes/wall loops:").grid(row=2, column=0, sticky="w")
        ttk.Spinbox(opts, from_=1, to=8, textvariable=self.wall_loops_var, width=6).grid(row=2, column=1, sticky="w")
        ttk.Label(opts, text="Símbolos activos:").grid(row=3, column=0, sticky="w")
        ttk.Combobox(opts, textvariable=self.active_symbols_var, values=["CMYK", "CMYW", "CMYKW", "CMY", "CMYKW"], width=12).grid(row=3, column=1, sticky="w")
        ttk.Label(opts, text="Modelo de color:").grid(row=4, column=0, sticky="w")
        ttk.Label(opts, text="v49 físico óptico + capas Snapmaker corregidas", foreground="#444444").grid(row=4, column=1, sticky="w")
        ttk.Checkbutton(opts, text="Ordenar por parecido perceptual CIE L*a*b*", variable=self.sort_by_target_var).grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(opts, text="Filtrar colores repetidos / casi iguales", variable=self.filter_same_hex_var).grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Label(opts, text="Umbral repetido ΔE:").grid(row=7, column=0, sticky="w")
        ttk.Entry(opts, textvariable=self.duplicate_deltae_var, width=8).grid(row=7, column=1, sticky="w")
        ttk.Checkbutton(opts, text="Sumar recetas de 2 capas 50/50 + 3 capas 33/33/33", variable=self.include_two_layer_var).grid(row=8, column=0, columnspan=2, sticky="w")
        ttk.Label(opts, text="Límite visual 0=todos:").grid(row=9, column=0, sticky="w")
        ttk.Entry(opts, textvariable=self.limit_var, width=8).grid(row=9, column=1, sticky="w")
        ttk.Label(opts, text="Nº filamentos base:").grid(row=10, column=0, sticky="w")
        ttk.Spinbox(opts, from_=1, to=5, textvariable=self.base_count_var, width=6, command=self.apply_base_count).grid(row=10, column=1, sticky="w")
        ttk.Button(opts, text="Aplicar nº bases", command=self.apply_base_count).grid(row=11, column=0, columnspan=2, sticky="ew", pady=(4,0))
        ttk.Label(
            opts,
            text="Quité filtros de K/oscuro. La gama sale del modelo óptico + translucidez por filamento. En Snapmaker se recomienda .gcode plano, no .gcode.3mf.",
            wraplength=330,
            foreground="#555555",
        ).grid(row=12, column=0, columnspan=2, sticky="w", pady=(8,0))

        pal = ttk.LabelFrame(left, text="Filamentos base / símbolos", padding=8)
        pal.pack(fill="x", pady=8)
        ttk.Label(pal, text="Símbolo").grid(row=0, column=0)
        ttk.Label(pal, text="Filamento").grid(row=0, column=1)
        ttk.Label(pal, text="HEX").grid(row=0, column=2)
        ttk.Label(pal, text="Transl.").grid(row=0, column=3)
        for i, s in enumerate(SYMBOLS_ALL, start=1):
            ttk.Label(pal, text=f"{s} {SYMBOL_LABELS[s]}").grid(row=i, column=0, sticky="w")
            ttk.Spinbox(pal, from_=1, to=32, textvariable=self.symbol_filament_vars[s], width=6, command=self.update_symbol_hex_from_filament).grid(row=i, column=1)
            ttk.Entry(pal, textvariable=self.symbol_hex_vars[s], width=10).grid(row=i, column=2, padx=4)
            ttk.Spinbox(pal, from_=0.0, to=1.0, increment=0.02, textvariable=self.symbol_translucency_vars[s], width=6, command=self.preview_with_base_colors).grid(row=i, column=3, padx=4)
        ttk.Button(pal, text="Usar HEX del G-code", command=self.update_symbol_hex_from_filament).grid(row=6, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Label(pal, text="Transl.: 0=muy opaco, 1=muy translúcido", foreground="#555555").grid(row=7, column=0, columnspan=4, sticky="w")

        marker_box = ttk.LabelFrame(left, text="Filamento virtual / marcador", padding=8)
        marker_box.pack(fill="x")
        ttk.Label(marker_box, text="Elegir marcador:").pack(anchor="w")
        self.marker_combo = ttk.Combobox(marker_box, textvariable=self.selected_marker_var, values=[], width=32, state="readonly")
        self.marker_combo.pack(fill="x")
        self.marker_combo.bind("<<ComboboxSelected>>", lambda _e: self.generate_palette())
        ttk.Button(marker_box, text="Generar paleta visual", command=self.generate_palette).pack(fill="x", pady=4)
        ttk.Button(marker_box, text="Asignar receta seleccionada", command=self.assign_selected).pack(fill="x")
        ttk.Button(marker_box, text="Convertir G-code", command=self.convert).pack(fill="x", pady=(8, 0))
        ttk.Label(marker_box, textvariable=self.status_var, wraplength=360).pack(fill="x", pady=(8, 0))

        tabs = ttk.Notebook(right)
        tabs.pack(fill="both", expand=True)
        self.tab_base = ttk.Frame(tabs)
        self.tab_map2d = ttk.Frame(tabs)
        self.tab_palette = ttk.Frame(tabs)
        self.tab_layer_palette = ttk.Frame(tabs)
        self.tab_filaments = ttk.Frame(tabs)
        self.tab_visualizer = ttk.Frame(tabs)
        self.tab_assign = ttk.Frame(tabs)
        tabs.add(self.tab_base, text="Colores base")
        tabs.add(self.tab_map2d, text="Mapa visual continuo")
        # v34: se oculta la antigua pestaña "Paleta 4x4 por capa" para dejar solo el mapa visual continuo.
        tabs.add(self.tab_filaments, text="Filamento → receta")
        tabs.add(self.tab_visualizer, text="Visualizador 3D")
        tabs.add(self.tab_palette, text="Lista técnica")
        tabs.add(self.tab_assign, text="Asignaciones / mapa")

        self._build_base_color_tab()
        self._build_visualizer_tab()

        # v32: mapa visual continuo tipo paleta de colores
        map_top = ttk.Frame(self.tab_map2d, padding=6)
        map_top.pack(fill="x")
        self.map2d_info = tk.StringVar(value="Genera el mapa: cada rectángulo es una receta completa de 2 o 3 capas. Haz clic para ver el patrón.")
        ttk.Button(map_top, text="Generar mapa visual", command=self.generate_map2d).pack(side="left")
        ttk.Button(map_top, text="Asignar receta seleccionada", command=self.assign_selected).pack(side="left", padx=5)
        ttk.Label(map_top, textvariable=self.map2d_info, wraplength=920).pack(side="left", padx=8)

        map_body = ttk.Frame(self.tab_map2d)
        map_body.pack(fill="both", expand=True)
        self.map2d_canvas = tk.Canvas(map_body, bg="white", highlightthickness=0)
        self.map2d_scroll_y = ttk.Scrollbar(map_body, orient="vertical", command=self.map2d_canvas.yview)
        self.map2d_scroll_x = ttk.Scrollbar(map_body, orient="horizontal", command=self.map2d_canvas.xview)
        self.map2d_canvas.configure(yscrollcommand=self.map2d_scroll_y.set, xscrollcommand=self.map2d_scroll_x.set)
        self.map2d_canvas.pack(side="left", fill="both", expand=True)
        self.map2d_scroll_y.pack(side="right", fill="y")
        self.map2d_scroll_x.pack(side="bottom", fill="x")
        self.map2d_canvas.bind("<Button-1>", self.on_map2d_click)
        self.map2d_canvas.bind("<MouseWheel>", lambda e: self.map2d_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        self.map2d_canvas.bind("<Button-4>", lambda e: self.map2d_canvas.yview_scroll(-3, "units"))
        self.map2d_canvas.bind("<Button-5>", lambda e: self.map2d_canvas.yview_scroll(3, "units"))

        top_palette = ttk.Frame(self.tab_palette, padding=4)
        top_palette.pack(fill="x")
        self.palette_info = tk.StringVar(value="Sin paleta generada.")
        ttk.Label(top_palette, textvariable=self.palette_info).pack(side="left", anchor="w")

        self.canvas = tk.Canvas(self.tab_palette, bg="white", highlightthickness=0)
        self.scroll_y = ttk.Scrollbar(self.tab_palette, orient="vertical", command=self.canvas.yview)
        self.scroll_x = ttk.Scrollbar(self.tab_palette, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.scroll_y.set, xscrollcommand=self.scroll_x.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll_y.pack(side="right", fill="y")
        self.scroll_x.pack(side="bottom", fill="x")
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))

        # Nueva pestaña v32: mezcla sustractiva CMY / PLA opaco y combinación de 3 capas
        layer_top = ttk.Frame(self.tab_layer_palette, padding=6)
        layer_top.pack(fill="x")
        self.layer_palette_info = tk.StringVar(value="Genera una paleta exterior × interior por capa. Luego combina recetas de 3 capas y, opcionalmente, 2 capas 50/50.")
        ttk.Button(layer_top, text="Generar paleta 2/3 capas", command=self.generate_layer_palette).pack(side="left")
        ttk.Button(layer_top, text="Asignar receta seleccionada", command=self.assign_selected).pack(side="left", padx=5)
        ttk.Label(layer_top, textvariable=self.layer_palette_info, wraplength=900).pack(side="left", padx=8)

        self.layer_canvas = tk.Canvas(self.tab_layer_palette, bg="white", highlightthickness=0)
        self.layer_scroll_y = ttk.Scrollbar(self.tab_layer_palette, orient="vertical", command=self.layer_canvas.yview)
        self.layer_scroll_x = ttk.Scrollbar(self.tab_layer_palette, orient="horizontal", command=self.layer_canvas.xview)
        self.layer_canvas.configure(yscrollcommand=self.layer_scroll_y.set, xscrollcommand=self.layer_scroll_x.set)
        self.layer_canvas.pack(side="left", fill="both", expand=True)
        self.layer_scroll_y.pack(side="right", fill="y")
        self.layer_scroll_x.pack(side="bottom", fill="x")
        self.layer_canvas.bind("<Button-1>", self.on_layer_canvas_click)
        self.layer_canvas.bind("<MouseWheel>", lambda e: self.layer_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        self.layer_canvas.bind("<Button-4>", lambda e: self.layer_canvas.yview_scroll(-3, "units"))
        self.layer_canvas.bind("<Button-5>", lambda e: self.layer_canvas.yview_scroll(3, "units"))

        # Pestaña visual editable: cada filamento virtual/marcador y su receta asignada
        filament_top = ttk.Frame(self.tab_filaments, padding=6)
        filament_top.pack(fill="x")
        ttk.Button(filament_top, text="Actualizar tabla", command=self.refresh_filament_assignment_tab).pack(side="left")
        ttk.Button(filament_top, text="Auto asignar todos", command=self.auto_assign_all_markers).pack(side="left", padx=5)
        ttk.Button(filament_top, text="Aplicar recetas escritas", command=self.apply_rows_to_assignments).pack(side="left", padx=5)
        ttk.Label(
            filament_top,
            text="Haz clic en el color asignado para abrir la paleta visual completa. Las cajas de texto quedan solo como edición avanzada opcional."
        ).pack(side="left", padx=8)

        self.filament_canvas = tk.Canvas(self.tab_filaments, bg="white", highlightthickness=0)
        self.filament_scroll = ttk.Scrollbar(self.tab_filaments, orient="vertical", command=self.filament_canvas.yview)
        self.filament_canvas.configure(yscrollcommand=self.filament_scroll.set)
        self.filament_scroll.pack(side="right", fill="y")
        self.filament_canvas.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.filament_frame = ttk.Frame(self.filament_canvas)
        self.filament_canvas_window = self.filament_canvas.create_window((0, 0), window=self.filament_frame, anchor="nw")
        self.filament_frame.bind("<Configure>", lambda _e: self.filament_canvas.configure(scrollregion=self.filament_canvas.bbox("all")))
        self.filament_canvas.bind("<Configure>", lambda e: self.filament_canvas.itemconfigure(self.filament_canvas_window, width=e.width))
        self.filament_canvas.bind("<MouseWheel>", lambda e: self.filament_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        self.filament_canvas.bind("<Button-4>", lambda e: self.filament_canvas.yview_scroll(-3, "units"))
        self.filament_canvas.bind("<Button-5>", lambda e: self.filament_canvas.yview_scroll(3, "units"))

        assign_top = ttk.Frame(self.tab_assign, padding=6)
        assign_top.pack(fill="x")
        ttk.Button(assign_top, text="Actualizar vista", command=self.refresh_assignments_view).pack(side="left")
        ttk.Button(assign_top, text="Copiar mapa manual", command=self.copy_manual_map_to_clipboard).pack(side="left", padx=5)
        ttk.Label(assign_top, text="Formato: filamento:EXTERIOR:INTERIOR. Acepta 2 capas (WK:KW) o 3 capas (CYC:KWK)").pack(side="left", padx=8)
        self.assign_text = tk.Text(self.tab_assign, wrap="none", height=20)
        self.assign_text.pack(fill="both", expand=True, padx=6, pady=6)


    # ---------------- v38 base-count helper ----------------
    def apply_base_count(self):
        """Actualiza símbolos activos y marcador inicial según la cantidad de bases.

        3 = CMY, 4 = CMYK, 5 = CMYKW. Esto no obliga a cambiar los colores HEX;
        solo define cuántos filamentos físicos forman la paleta que usa el algoritmo.
        """
        try:
            n = max(1, min(5, int(self.base_count_var.get())))
        except Exception:
            n = 4
            self.base_count_var.set(n)
        mapping = {1: "C", 2: "CM", 3: "CMY", 4: "CMYK", 5: "CMYKW"}
        self.active_symbols_var.set(mapping.get(n, "CMYK"))
        self.process_from_var.set(n + 1)
        if self.meta:
            self.refresh_base_color_tab()
            self.refresh_filament_assignment_tab()
            self.generate_map2d()
        self.status_var.set(f"Usando {n} filamentos base: {self.active_symbols_var.get()}. Marcadores desde filamento {n+1}.")

    # ---------------- v41 visualizador 3D ----------------
    def _build_visualizer_tab(self):
        top = ttk.Frame(self.tab_visualizer, padding=6)
        top.pack(fill="x")
        self.vis_mode_var = tk.StringVar(value="PLA opaco")
        self.vis_feature_var = tk.StringVar(value="Modelo")
        self.vis_view_var = tk.StringVar(value="Isométrico")
        self.vis_layer_var = tk.IntVar(value=0)
        self.vis_until_layer_var = tk.BooleanVar(value=True)
        self.vis_max_segments_var = tk.IntVar(value=12000)
        self.vis_source_var = tk.StringVar(value="Final temporal")
        self.vis_volume_var = tk.BooleanVar(value=True)
        self.vis_line_width_var = tk.DoubleVar(value=2.8)
        # v41: cámara manipulable para el visualizador
        self.vis_yaw_var = tk.DoubleVar(value=35.0)
        self.vis_pitch_var = tk.DoubleVar(value=28.0)
        self.vis_pan_x_var = tk.DoubleVar(value=0.0)
        self.vis_pan_y_var = tk.DoubleVar(value=0.0)
        self._vis_drag_mode = None
        self._vis_drag_last = (0, 0)
        self.vis_status_var = tk.StringVar(value="Carga el G-code y pulsa 'Cargar visualizador'. Por defecto muestra el G-code final temporal, no el original.")
        self.vis_segments = []
        self.vis_layers = []
        self.vis_bounds = (0, 0, 0, 0, 0, 0)

        ttk.Button(top, text="Cargar visualizador", command=self.load_visualizer).pack(side="left")
        ttk.Label(top, text="Fuente:").pack(side="left", padx=(12, 2))
        ttk.Combobox(top, textvariable=self.vis_source_var, values=["Final temporal", "Archivo de salida", "Original"], width=16, state="readonly").pack(side="left")
        ttk.Label(top, text="Modo color:").pack(side="left", padx=(12, 2))
        ttk.Combobox(top, textvariable=self.vis_mode_var, values=["Filamento real", "PLA opaco"], width=14, state="readonly").pack(side="left")
        ttk.Label(top, text="Vista:").pack(side="left", padx=(12, 2))
        ttk.Combobox(top, textvariable=self.vis_view_var, values=["Isométrico", "Superior XY"], width=12, state="readonly").pack(side="left")
        ttk.Label(top, text="Mostrar:").pack(side="left", padx=(12, 2))
        ttk.Combobox(top, textvariable=self.vis_feature_var, values=["Modelo", "Outer wall", "Inner wall", "Top/Bottom", "Todo"], width=12, state="readonly").pack(side="left")
        ttk.Checkbutton(top, text="Hasta capa", variable=self.vis_until_layer_var).pack(side="left", padx=(10, 0))
        ttk.Button(top, text="Redibujar", command=self.draw_visualizer).pack(side="left", padx=8)

        row2 = ttk.Frame(self.tab_visualizer, padding=(6, 0, 6, 4))
        row2.pack(fill="x")
        ttk.Label(row2, text="Capa:").pack(side="left")
        self.vis_layer_scale = ttk.Scale(row2, from_=0, to=0, orient="horizontal", command=lambda _v: self._on_vis_layer_slide())
        self.vis_layer_scale.pack(side="left", fill="x", expand=True, padx=6)
        self.vis_layer_label = ttk.Label(row2, text="0 / 0")
        self.vis_layer_label.pack(side="left", padx=6)
        ttk.Label(row2, text="Máx. segmentos:").pack(side="left", padx=(12, 2))
        ttk.Entry(row2, textvariable=self.vis_max_segments_var, width=8).pack(side="left")
        ttk.Checkbutton(row2, text="Volumen visual", variable=self.vis_volume_var, command=self.draw_visualizer).pack(side="left", padx=(12, 2))
        ttk.Label(row2, text="Grosor:").pack(side="left", padx=(8, 2))
        ttk.Entry(row2, textvariable=self.vis_line_width_var, width=5).pack(side="left")

        row3 = ttk.Frame(self.tab_visualizer, padding=(6, 0, 6, 4))
        row3.pack(fill="x")
        ttk.Label(row3, text="Cámara: yaw").pack(side="left")
        ttk.Scale(row3, from_=-180, to=180, orient="horizontal", variable=self.vis_yaw_var, command=lambda _v: self.draw_visualizer()).pack(side="left", fill="x", expand=True, padx=(4, 10))
        ttk.Label(row3, text="pitch").pack(side="left")
        ttk.Scale(row3, from_=-75, to=75, orient="horizontal", variable=self.vis_pitch_var, command=lambda _v: self.draw_visualizer()).pack(side="left", fill="x", expand=True, padx=(4, 10))
        ttk.Button(row3, text="Reset cámara", command=self._vis_reset_camera).pack(side="left", padx=4)
        ttk.Label(row3, text="Arrastrar izq.=rotar | arrastrar der./medio.=paneo | rueda=zoom").pack(side="left", padx=(12,0))

        body = ttk.Frame(self.tab_visualizer)
        body.pack(fill="both", expand=True)
        self.vis_canvas = tk.Canvas(body, bg="#111111", highlightthickness=0)
        self.vis_canvas.pack(fill="both", expand=True, padx=6, pady=6)
        self.vis_canvas.bind("<Configure>", lambda _e: self.draw_visualizer())
        self.vis_canvas.bind("<MouseWheel>", lambda e: self._vis_zoom(e.delta))
        self.vis_canvas.bind("<ButtonPress-1>", lambda e: self._vis_drag_start(e, 'rotate'))
        self.vis_canvas.bind("<B1-Motion>", self._vis_drag_move)
        self.vis_canvas.bind("<ButtonRelease-1>", self._vis_drag_end)
        self.vis_canvas.bind("<ButtonPress-2>", lambda e: self._vis_drag_start(e, 'pan'))
        self.vis_canvas.bind("<B2-Motion>", self._vis_drag_move)
        self.vis_canvas.bind("<ButtonRelease-2>", self._vis_drag_end)
        self.vis_canvas.bind("<ButtonPress-3>", lambda e: self._vis_drag_start(e, 'pan'))
        self.vis_canvas.bind("<B3-Motion>", self._vis_drag_move)
        self.vis_canvas.bind("<ButtonRelease-3>", self._vis_drag_end)
        self.vis_scale = 1.0
        ttk.Label(self.tab_visualizer, textvariable=self.vis_status_var, padding=6, wraplength=1100).pack(fill="x")

    def _on_vis_layer_slide(self):
        try:
            idx = int(float(self.vis_layer_scale.get()))
        except Exception:
            idx = 0
        self.vis_layer_var.set(idx)
        total = max(0, len(getattr(self, 'vis_layers', [])) - 1)
        self.vis_layer_label.config(text=f"{idx} / {total}")
        self.draw_visualizer()

    def _vis_zoom(self, delta):
        if delta > 0:
            self.vis_scale *= 1.12
        else:
            self.vis_scale /= 1.12
        self.vis_scale = max(0.3, min(8.0, self.vis_scale))
        self.draw_visualizer()

    def _vis_drag_start(self, event, mode):
        self._vis_drag_mode = mode
        self._vis_drag_last = (event.x, event.y)

    def _vis_drag_move(self, event):
        mode = getattr(self, '_vis_drag_mode', None)
        lx, ly = getattr(self, '_vis_drag_last', (event.x, event.y))
        dx, dy = event.x - lx, event.y - ly
        self._vis_drag_last = (event.x, event.y)
        if mode == 'rotate':
            self.vis_yaw_var.set(float(self.vis_yaw_var.get()) + dx * 0.45)
            self.vis_pitch_var.set(max(-75.0, min(75.0, float(self.vis_pitch_var.get()) - dy * 0.35)))
        elif mode == 'pan':
            self.vis_pan_x_var.set(float(self.vis_pan_x_var.get()) + dx)
            self.vis_pan_y_var.set(float(self.vis_pan_y_var.get()) + dy)
        self.draw_visualizer()

    def _vis_drag_end(self, _event=None):
        self._vis_drag_mode = None

    def _vis_reset_camera(self):
        self.vis_scale = 1.0
        self.vis_yaw_var.set(35.0)
        self.vis_pitch_var.set(28.0)
        self.vis_pan_x_var.set(0.0)
        self.vis_pan_y_var.set(0.0)
        self.draw_visualizer()

    def _current_tool_hex(self, tool: int) -> str:
        """Color real para una herramienta, considerando los colores base editados."""
        if self.meta:
            # Si una herramienta está asignada a un símbolo, usa el HEX editado de ese símbolo.
            for sym, var in self.symbol_filament_vars.items():
                try:
                    if core.human_to_tool(int(var.get())) == tool:
                        return _norm_hex(self.symbol_hex_vars[sym].get())
                except Exception:
                    pass
            return _tool_color(self.meta, tool)
        return core._default_palette_color(tool)

    def _tool_rgb_for_vis(self, tool: int) -> Tuple[int, int, int]:
        return core.hex_to_rgb(self._current_tool_hex(tool))

    def _perceived_pair_vis_rgb(self, outer_tool: int, inner_tool: Optional[int]) -> Tuple[int, int, int]:
        outer_rgb = self._tool_rgb_for_vis(outer_tool)
        if inner_tool is None:
            return outer_rgb
        inner_rgb = self._tool_rgb_for_vis(inner_tool)
        # Mismo modelo sustractivo que v36/v38, aplicado a una pared exterior + una interior cercana.
        outer_od = _rgb_to_optical_density(outer_rgb, _symbol_density_boost("X", outer_rgb))
        inner_od = _rgb_to_optical_density(inner_rgb, _symbol_density_boost("X", inner_rgb))
        # Fuerza conservadora: PLA opaco deja ver poco la primera pared interior.
        final_od = _od_add(outer_od, inner_od, 0.18)
        return _optical_density_to_rgb(final_od)

    def _visualizer_text_source(self) -> Tuple[str, str]:
        """Devuelve el G-code que debe visualizarse.

        v41: por defecto genera un G-code final temporal con las recetas/colores actuales,
        para que el visualizador no muestre el archivo original sin modificar.
        """
        source = self.vis_source_var.get() if hasattr(self, "vis_source_var") else "Original"
        inp = Path(self.input_var.get())
        if not inp.exists():
            raise FileNotFoundError("Selecciona un G-code válido primero.")

        if source == "Original":
            text, _member = core.read_gcode_text_from_input(inp)
            return text, f"original: {inp.name}"

        if source == "Archivo de salida":
            outp = Path(self.output_var.get())
            if not outp.exists():
                raise FileNotFoundError("El archivo de salida todavía no existe. Usa 'Final temporal' o convierte primero.")
            text, _member = core.read_gcode_text_from_input(outp)
            return text, f"salida existente: {outp.name}"

        # Final temporal con las recetas/colores actuales.
        tmpdir = Path(tempfile.mkdtemp(prefix="bppb_v41_preview_"))
        tmpout = tmpdir / "preview_final.gcode"
        opts = self.make_opts()
        _stats, _report = self.convert_with_effective_base(inp, tmpout, opts)
        text, _member = core.read_gcode_text_from_input(tmpout)
        return text, "final temporal generado con recetas actuales"

    def load_visualizer(self):
        try:
            text, label = self._visualizer_text_source()
            self.vis_segments, self.vis_layers, self.vis_bounds = self.parse_gcode_segments_for_visualizer(text)
            max_idx = max(0, len(self.vis_layers) - 1)
            self.vis_layer_scale.configure(from_=0, to=max_idx)
            self.vis_layer_scale.set(max_idx)
            self.vis_layer_var.set(max_idx)
            self.vis_layer_label.config(text=f"{max_idx} / {max_idx}")
            self.vis_status_var.set(
                f"Visualizador cargado desde {label}: {len(self.vis_segments)} segmentos dibujables, {len(self.vis_layers)} capas. "
                "v41 permite rotar cámara, hacer paneo, zoom y visualizar el G-code final temporal por defecto."
            )
            self.draw_visualizer()
        except Exception as exc:
            messagebox.showerror("Error visualizador", str(exc))

    def parse_gcode_segments_for_visualizer(self, text: str):
        x = y = z = None
        e = 0.0
        absolute_e = True
        tool = 0
        feature = ""
        segments = []
        layers = []
        layer_index_by_z = {}
        bounds = [1e9, 1e9, 1e9, -1e9, -1e9, -1e9]
        max_segments = max(1000, int(self.vis_max_segments_var.get() or 35000))
        skip_feature = False
        line_re = re.compile(r'([XYZE])([-+]?\d*\.?\d+)')
        tool_re = re.compile(r'^\s*T(\d+)\b')

        raw_segments = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith(';FEATURE:'):
                feature = line.split(':', 1)[1].strip()
                skip_feature = 'Prime tower' in feature or 'Flush' in feature
                continue
            if line.startswith(';TYPE:'):
                feature = line.split(':', 1)[1].strip()
                skip_feature = 'Prime tower' in feature or 'Custom' in feature
                continue
            if line.startswith('M82'):
                absolute_e = True
                continue
            if line.startswith('M83'):
                absolute_e = False
                continue
            mtool = tool_re.match(line)
            if mtool:
                tool = int(mtool.group(1))
                continue
            if not (line.startswith('G0') or line.startswith('G1')):
                continue
            vals = {m.group(1): float(m.group(2)) for m in line_re.finditer(line)}
            nx = vals.get('X', x)
            ny = vals.get('Y', y)
            nz = vals.get('Z', z)
            ne = vals.get('E', e)
            if x is not None and y is not None and nx is not None and ny is not None and z is not None:
                if 'E' in vals:
                    de = (ne - e) if absolute_e else ne
                    if de > 0.00001 and not skip_feature:
                        zi = float(nz if nz is not None else z)
                        # capa por Z aproximada
                        zkey = round(zi, 4)
                        if zkey not in layer_index_by_z:
                            layer_index_by_z[zkey] = len(layers)
                            layers.append(zkey)
                        li = layer_index_by_z[zkey]
                        seg = {
                            'x1': float(x), 'y1': float(y), 'z': zi,
                            'x2': float(nx), 'y2': float(ny),
                            'tool': int(tool), 'feature': feature or 'Unknown', 'layer': li, 'de': float(de),
                        }
                        raw_segments.append(seg)
                        bounds[0] = min(bounds[0], seg['x1'], seg['x2']); bounds[3] = max(bounds[3], seg['x1'], seg['x2'])
                        bounds[1] = min(bounds[1], seg['y1'], seg['y2']); bounds[4] = max(bounds[4], seg['y1'], seg['y2'])
                        bounds[2] = min(bounds[2], seg['z']); bounds[5] = max(bounds[5], seg['z'])
            x, y, z = nx, ny, nz
            if 'E' in vals:
                e = ne

        if not raw_segments:
            return [], [], (0, 0, 0, 1, 1, 1)
        step = max(1, math.ceil(len(raw_segments) / max_segments))
        # Precalcula influencia interior para segmentos outer: busca el inner más cercano por capa con grid simple.
        inner_by_layer = {}
        for seg in raw_segments:
            feat = seg['feature'].lower()
            if 'inner' in feat or 'internal' in feat:
                inner_by_layer.setdefault(seg['layer'], []).append(seg)
        for seg in raw_segments:
            if step > 1 and (len(segments) % step != 0):
                pass
        sampled = raw_segments[::step]
        # Para no hacer O(n^2) grande, comparamos contra una muestra de interiores por capa.
        for seg in sampled:
            inner_tool = None
            if 'outer' in seg['feature'].lower() and self.vis_mode_var.get() == "PLA opaco":
                inners = inner_by_layer.get(seg['layer'], [])
                if inners:
                    mx = (seg['x1'] + seg['x2']) * 0.5
                    my = (seg['y1'] + seg['y2']) * 0.5
                    best = None
                    # submuestreo estable si hay muchos interiores
                    inner_step = max(1, len(inners) // 350)
                    for ins in inners[::inner_step]:
                        ix = (ins['x1'] + ins['x2']) * 0.5
                        iy = (ins['y1'] + ins['y2']) * 0.5
                        d = (mx - ix) ** 2 + (my - iy) ** 2
                        if best is None or d < best[0]:
                            best = (d, ins['tool'])
                    if best and best[0] < 100.0:  # radio aprox 10 mm
                        inner_tool = best[1]
            seg['inner_tool'] = inner_tool
            sampled_rgb = self._perceived_pair_vis_rgb(seg['tool'], inner_tool) if self.vis_mode_var.get() == "PLA opaco" else self._tool_rgb_for_vis(seg['tool'])
            seg['color'] = core.rgb_to_hex(sampled_rgb)
        return sampled, layers, tuple(bounds)

    def _vis_feature_ok(self, feature: str) -> bool:
        mode = self.vis_feature_var.get()
        f = (feature or '').lower()
        if mode == "Todo":
            return True
        if mode == "Modelo":
            return not ('prime' in f or 'flush' in f or 'brim' in f)
        if mode == "Outer wall":
            return 'outer' in f
        if mode == "Inner wall":
            return 'inner' in f
        if mode == "Top/Bottom":
            return 'top' in f or 'bottom' in f or 'solid' in f
        return True

    def _project_vis(self, x, y, z, w, h):
        xmin, ymin, zmin, xmax, ymax, zmax = self.vis_bounds
        cx = (xmin + xmax) * 0.5
        cy = (ymin + ymax) * 0.5
        cz = (zmin + zmax) * 0.5
        x -= cx
        y -= cy
        z -= cz
        # Escala Z visual: en G-code la altura suele ser mucho menor que XY,
        # así que exageramos un poco para leer mejor el volumen.
        z *= 2.2

        if self.vis_view_var.get() == "Superior XY":
            u, v = x, -y
        else:
            yaw = math.radians(float(getattr(self, 'vis_yaw_var', tk.DoubleVar(value=35.0)).get()))
            pitch = math.radians(float(getattr(self, 'vis_pitch_var', tk.DoubleVar(value=28.0)).get()))
            cyaw, syaw = math.cos(yaw), math.sin(yaw)
            cp, sp = math.cos(pitch), math.sin(pitch)
            # rotación alrededor de Z, luego inclinación alrededor de X
            xr = x * cyaw - y * syaw
            yr = x * syaw + y * cyaw
            yp = yr * cp - z * sp
            u, v = xr, -yp

        span = max(xmax - xmin, ymax - ymin, 1.0)
        scale = min(w, h) * 0.78 / span * self.vis_scale
        pan_x = float(getattr(self, 'vis_pan_x_var', tk.DoubleVar(value=0.0)).get())
        pan_y = float(getattr(self, 'vis_pan_y_var', tk.DoubleVar(value=0.0)).get())
        return w * 0.5 + pan_x + u * scale, h * 0.52 + pan_y + v * scale

    def draw_visualizer(self):
        if not hasattr(self, 'vis_canvas'):
            return
        c = self.vis_canvas
        c.delete('all')
        if not getattr(self, 'vis_segments', None):
            c.create_text(20, 20, anchor='nw', fill='white', text="Carga un G-code para visualizar trayectorias y color percibido.")
            return
        w = max(200, c.winfo_width())
        h = max(200, c.winfo_height())
        idx = int(self.vis_layer_var.get())
        until = bool(self.vis_until_layer_var.get())
        drawn = 0
        # Orden por capa para pseudo profundidad
        for seg in self.vis_segments:
            li = seg['layer']
            if until:
                if li > idx:
                    continue
            else:
                if li != idx:
                    continue
            if not self._vis_feature_ok(seg['feature']):
                continue
            x1, y1 = self._project_vis(seg['x1'], seg['y1'], seg['z'], w, h)
            x2, y2 = self._project_vis(seg['x2'], seg['y2'], seg['z'], w, h)
            feat = seg['feature'].lower()
            width = 1
            if bool(getattr(self, 'vis_volume_var', tk.BooleanVar(value=True)).get()):
                try:
                    base_w = float(self.vis_line_width_var.get())
                except Exception:
                    base_w = 2.8
                width = max(2, int(round(base_w * max(0.75, min(2.2, self.vis_scale)))))
                if 'outer' in feat:
                    width += 1
                elif 'top' in feat or 'bottom' in feat or 'solid' in feat:
                    width = max(2, width)
                # sombra suave para dar lectura de volumen en isométrico
                if self.vis_view_var.get() == "Isométrico":
                    c.create_line(x1 + 1, y1 + 1, x2 + 1, y2 + 1, fill="#050505", width=width + 1, capstyle=tk.ROUND, joinstyle=tk.ROUND)
            else:
                width = 2 if 'outer' in feat else 1
            c.create_line(x1, y1, x2, y2, fill=seg.get('color', '#FFFFFF'), width=width, capstyle=tk.ROUND, joinstyle=tk.ROUND)
            drawn += 1
        c.create_text(10, 10, anchor='nw', fill='#EDEDED', text=f"v41 visualizador | segmentos dibujados: {drawn} | capa {idx}/{max(0,len(self.vis_layers)-1)} | modo {self.vis_mode_var.get()} | fuente {self.vis_source_var.get()}")
        c.create_text(10, 30, anchor='nw', fill='#BBBBBB', text="Izq. arrastrar=rotar | der./medio=pan | rueda=zoom. Muestra el G-code final temporal por defecto con volumen visual.")


    # ---------------- base color editor ----------------
    def _build_base_color_tab(self):
        """Pestaña para ver y cambiar los colores base físicos usados por el algoritmo.

        El G-code original puede decir que T0..T3 son ciertos colores, pero aquí puedes
        simular otra carga real de filamentos antes de convertir. La paleta visual y el
        auto-generador usan estos HEX editados.
        """
        top = ttk.Frame(self.tab_base, padding=8)
        top.pack(fill="x")
        ttk.Label(
            top,
            text=(
                "Aquí puedes cambiar manualmente los colores base físicos. "
                "El color original del G-code se mantiene como referencia, pero el algoritmo, "
                "la paleta visual y el G-code generado usarán los colores nuevos."
            ),
            wraplength=980,
        ).pack(side="left", fill="x", expand=True)

        btns = ttk.Frame(self.tab_base, padding=(8, 0, 8, 4))
        btns.pack(fill="x")
        ttk.Button(btns, text="Usar primeros 4 del G-code", command=self.use_first_four_from_gcode).pack(side="left")
        ttk.Button(btns, text="Restaurar HEX del G-code", command=self.update_symbol_hex_from_filament_and_refresh).pack(side="left", padx=5)
        ttk.Button(btns, text="Previsualizar nueva paleta", command=self.preview_with_base_colors).pack(side="left", padx=5)
        ttk.Label(btns, text="Tip: clic en 'Elegir color' abre el selector nativo de Windows/macOS/Linux.").pack(side="left", padx=10)

        wrap = ttk.Frame(self.tab_base, padding=8)
        wrap.pack(fill="both", expand=True)
        self.base_canvas = tk.Canvas(wrap, bg="white", highlightthickness=0)
        self.base_scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.base_canvas.yview)
        self.base_canvas.configure(yscrollcommand=self.base_scroll.set)
        self.base_scroll.pack(side="right", fill="y")
        self.base_canvas.pack(side="left", fill="both", expand=True)
        self.base_frame = ttk.Frame(self.base_canvas)
        self.base_window = self.base_canvas.create_window((0, 0), window=self.base_frame, anchor="nw")
        self.base_frame.bind("<Configure>", lambda _e: self.base_canvas.configure(scrollregion=self.base_canvas.bbox("all")))
        self.base_canvas.bind("<Configure>", lambda e: self.base_canvas.itemconfigure(self.base_window, width=e.width))
        self.base_canvas.bind("<MouseWheel>", lambda e: self.base_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        self.refresh_base_color_tab()

    def _base_symbols_to_show(self) -> List[str]:
        # Muestra los símbolos activos primero y permite editar también K/W aunque estén desactivados.
        active = list(self.active_symbols())
        for s in SYMBOLS_ALL:
            if s not in active:
                active.append(s)
        return active

    def refresh_base_color_tab(self):
        if not hasattr(self, "base_frame"):
            return
        for child in self.base_frame.winfo_children():
            child.destroy()
        headers = ["Símbolo", "Filamento físico", "Color original del G-code", "Color nuevo usado", "HEX nuevo", "Translucidez", "Acciones"]
        for c, h in enumerate(headers):
            ttk.Label(self.base_frame, text=h, font=("Arial", 9, "bold")).grid(row=0, column=c, sticky="w", padx=5, pady=(2, 8))

        for row, s in enumerate(self._base_symbols_to_show(), start=1):
            human = int(self.symbol_filament_vars[s].get())
            tool = core.human_to_tool(human)
            original_hex = _tool_color(self.meta, tool) if self.meta else DEFAULT_HEX.get(s, "#888888")
            new_hex = _norm_hex(self.symbol_hex_vars[s].get())
            ttk.Label(self.base_frame, text=f"{s} - {SYMBOL_LABELS.get(s, s)}").grid(row=row, column=0, sticky="w", padx=5, pady=4)
            ttk.Spinbox(
                self.base_frame,
                from_=1,
                to=32,
                textvariable=self.symbol_filament_vars[s],
                width=6,
                command=self.refresh_base_color_tab,
            ).grid(row=row, column=1, sticky="w", padx=5, pady=4)
            self._small_swatch(self.base_frame, original_hex, width=80, height=26, text=original_hex).grid(row=row, column=2, sticky="w", padx=5, pady=4)
            sw_new = self._small_swatch(self.base_frame, new_hex, width=80, height=26, text=new_hex)
            sw_new.grid(row=row, column=3, sticky="w", padx=5, pady=4)
            sw_new.configure(cursor="hand2")
            sw_new.bind("<Button-1>", lambda _e, sym=s: self.choose_symbol_color(sym))
            ttk.Entry(self.base_frame, textvariable=self.symbol_hex_vars[s], width=11).grid(row=row, column=4, sticky="w", padx=5, pady=4)
            trans_box = ttk.Frame(self.base_frame)
            trans_box.grid(row=row, column=5, sticky="w", padx=5, pady=4)
            ttk.Scale(trans_box, from_=0.0, to=1.0, variable=self.symbol_translucency_vars[s], orient="horizontal", length=110, command=lambda _v, sym=s: self.preview_with_base_colors(refresh_only=True)).pack(side="left")
            ttk.Spinbox(trans_box, from_=0.0, to=1.0, increment=0.02, textvariable=self.symbol_translucency_vars[s], width=5, command=lambda sym=s: self.preview_with_base_colors(refresh_only=True)).pack(side="left", padx=3)
            actions = ttk.Frame(self.base_frame)
            actions.grid(row=row, column=6, sticky="w", padx=5, pady=4)
            ttk.Button(actions, text="Elegir color", command=lambda sym=s: self.choose_symbol_color(sym)).pack(side="left")
            ttk.Button(actions, text="Original", command=lambda sym=s: self.reset_symbol_to_original(sym)).pack(side="left", padx=3)

        info = ttk.Label(
            self.base_frame,
            text=(
                "Los colores nuevos reemplazan visualmente a los primeros filamentos base en la simulación. "
                "Al convertir, también se actualiza la línea ; filament_colour del G-code de salida para que Bambu/Orca muestre esos 4 colores."
            ),
            wraplength=900,
        )
        info.grid(row=len(self._base_symbols_to_show()) + 1, column=0, columnspan=7, sticky="w", padx=5, pady=(12, 4))

    def choose_symbol_color(self, sym: str):
        current = _norm_hex(self.symbol_hex_vars[sym].get())
        rgb, hx = colorchooser.askcolor(color=current, title=f"Elegir color para {sym} / {SYMBOL_LABELS.get(sym, sym)}")
        if hx:
            self.symbol_hex_vars[sym].set(_norm_hex(hx))
            self.refresh_base_color_tab()
            self.preview_with_base_colors(refresh_only=True)

    def reset_symbol_to_original(self, sym: str):
        if not self.meta:
            return
        human = int(self.symbol_filament_vars[sym].get())
        tool = core.human_to_tool(human)
        self.symbol_hex_vars[sym].set(_tool_color(self.meta, tool))
        self.refresh_base_color_tab()
        self.preview_with_base_colors(refresh_only=True)

    def use_first_four_from_gcode(self):
        # Ajusta C/M/Y/K a los filamentos 1..4. W queda disponible en filamento 5 si existe.
        for i, s in enumerate("CMYK", start=1):
            self.symbol_filament_vars[s].set(i)
        if "W" in self.symbol_filament_vars:
            self.symbol_filament_vars["W"].set(5)
        self.update_symbol_hex_from_filament()
        self.refresh_base_color_tab()
        self.preview_with_base_colors(refresh_only=True)
        self.status_var.set("Base actualizada: C/M/Y/K usan los primeros 4 filamentos del G-code.")

    def update_symbol_hex_from_filament_and_refresh(self):
        self.update_symbol_hex_from_filament()
        self.refresh_base_color_tab()
        self.preview_with_base_colors(refresh_only=True)

    def apply_base_overrides_to_meta(self, meta: core.GcodeMetadata) -> core.GcodeMetadata:
        new_meta = copy.copy(meta)
        new_meta.filament_colors = list(meta.filament_colors)
        for s in SYMBOLS_ALL:
            try:
                tool = core.human_to_tool(int(self.symbol_filament_vars[s].get()))
            except Exception:
                continue
            if tool < 0:
                continue
            while len(new_meta.filament_colors) <= tool:
                new_meta.filament_colors.append(None)
            new_meta.filament_colors[tool] = _norm_hex(self.symbol_hex_vars[s].get())
        return new_meta

    def effective_meta(self) -> core.GcodeMetadata:
        if not self.meta:
            raise RuntimeError("Primero carga metadata.")
        return self.apply_base_overrides_to_meta(self.meta)

    def preview_with_base_colors(self, refresh_only: bool = False):
        # Regenera las vistas principales usando los colores base editados.
        try:
            self.refresh_base_color_tab()
            self.refresh_filament_assignment_tab()
            if self.map2d_recipes:
                self.generate_map2d()
            if self.recipes:
                self.generate_palette()
            if self.layer_combo_recipes:
                self.generate_layer_palette()
            if not refresh_only:
                self.status_var.set("Previsualización actualizada con los colores base nuevos.")
        except Exception as exc:
            if not refresh_only:
                messagebox.showerror("Error previsualizando", str(exc))



    def _is_snapmaker_raw_output(self, out: Path) -> bool:
        """Snapmaker U1 debe recibir/abrir un G-code plano.

        Un .gcode.3mf heredado de Bambu/Orca conserva Metadata/plate_1.gcode,
        project_settings.config y otros restos del proyecto previo. Snapmaker Orca
        a veces intenta procesar ese G-code interno como "previous 3mf" y falla.
        """
        return core.output_wants_raw_gcode(out)

    def _snapmaker_safe_raw_output_path(self, out: Path) -> Path:
        """Devuelve una ruta .gcode segura para Snapmaker.

        Si el usuario eligió .3mf o .gcode.3mf, no sobreescribimos ese contenedor:
        generamos un .gcode plano al lado, sin Metadata/plate_1.gcode ni marcadores
        reinyectados por el 3MF.
        """
        if self._is_snapmaker_raw_output(out):
            return out
        name = out.name
        low = name.lower()
        if low.endswith('.gcode.3mf'):
            stem = name[:-len('.gcode.3mf')]
        elif low.endswith('.3mf'):
            stem = name[:-len('.3mf')]
        else:
            stem = out.stem
        safe = out.with_name(stem + '_snapmaker_safe.gcode')
        if safe == out:
            safe = out.with_suffix('.gcode')
        return safe

    def _sanitize_snapmaker_orca_settings_text(self, text: str) -> str:
        """Corrige valores de configuración que Snapmaker Orca reemplaza o rechaza."""
        text = re.sub(
            r'("ensure_vertical_shell_thickness"\s*:\s*)"enabled"',
            r'\1"ensure_all"',
            text,
        )
        text = re.sub(
            r'(ensure_vertical_shell_thickness\s*[=:]\s*)enabled\b',
            r'\1ensure_all',
            text,
        )
        return text

    def _snapmaker_physical_color_list_for_output(self, original_meta: core.GcodeMetadata, count: int = 4) -> List[str]:
        """Lista de colores físicos para salida Snapmaker U1.

        La U1 solo debe anunciar T0-T3. Aunque el G-code de entrada tenga 24
        filamentos virtuales/marcadores, el archivo final no debe conservar esas
        entradas en la metadata, porque Orca/Snapmaker las vuelve a mostrar como
        filamentos disponibles.
        """
        base = []
        original = list(original_meta.filament_colors or [])
        for i in range(count):
            if i < len(original) and original[i]:
                base.append(_norm_hex(original[i]))
            else:
                base.append(_norm_hex(core._default_palette_color(i)))

        # Solo los símbolos activos. Así no metemos W como quinto color si el
        # modo activo es CMYK, ni K si el modo activo es CMYW.
        for s in self.active_symbols()[:count]:
            try:
                tool = core.human_to_tool(int(self.symbol_filament_vars[s].get()))
            except Exception:
                continue
            if 0 <= tool < count:
                base[tool] = _norm_hex(self.symbol_hex_vars[s].get())
        return base[:count]

    def _split_filament_array_value(self, value: str):
        """Divide listas de metadata de filamento respetando comillas.

        Orca/Bambu guarda muchas líneas como:
        ; filament_density = 1.25,1.25,...
        ; filament_settings_id = "A";"B";...
        """
        raw = value.strip()
        if ";" in raw:
            delim = ";"
        elif "," in raw:
            delim = ","
        else:
            return None, None
        try:
            fields = next(csv.reader([raw], delimiter=delim, quotechar='"'))
        except Exception:
            fields = raw.split(delim)
        return delim, fields

    def _join_filament_array_value(self, fields: List[str], delim: str) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=delim, quotechar='"', lineterminator="", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(fields)
        return buf.getvalue()

    def _force_snapmaker_4_filament_text(self, text: str, colors: List[str], count: int = 4) -> str:
        """Elimina marcadores T4+ de la metadata textual para salida Snapmaker.

        v48: limpieza agresiva y coherente. Snapmaker/Orca puede leer no solo
        ; filament_colour, sino también extruder_colour, nozzle_temperature y muchas
        listas por filamento. Si quedan 16 entradas, la app vuelve a mostrar/usar
        marcadores virtuales. Aquí recortamos a T0-T3 todas las listas cuyo largo
        coincide con la cantidad de filamentos del archivo original.
        """
        fixed_colors = [(_norm_hex(c) if c else _norm_hex(core._default_palette_color(i))) for i, c in enumerate(colors[:count])]
        original_count = len(self.meta.filament_colors) if self.meta and self.meta.filament_colors else 0
        force_color_keys = {"filament_colour", "filament_colors", "extruder_colour", "default_filament_colour"}

        def should_truncate_key(key: str, fields: List[str]) -> bool:
            lk = key.strip().lower()
            if len(fields) <= count:
                return False
            if lk in force_color_keys:
                return True
            if original_count and len(fields) == original_count:
                return True
            if lk.startswith("filament") or "filament" in lk:
                return True
            if lk.startswith("nozzle_temperature") or lk.endswith("_temp") or lk.endswith("_temperature"):
                return True
            if "fan_speed" in lk or "pressure_advance" in lk:
                return True
            return False

        out_lines = []
        for line in text.splitlines():
            m = re.match(r"^(\s*;\s*([^=]+?)\s*=\s*)(.*)$", line)
            if not m:
                out_lines.append(line)
                continue
            prefix, key, value = m.group(1), m.group(2).strip(), m.group(3)
            lk = key.lower()
            if lk in ("filament_colour", "filament_colors", "extruder_colour"):
                out_lines.append(prefix + ";".join(fixed_colors))
                continue
            if lk == "default_filament_colour":
                out_lines.append(prefix + ";".join([""] * count))
                continue
            delim, fields = self._split_filament_array_value(value)
            if delim and fields and should_truncate_key(key, fields):
                out_lines.append(prefix + self._join_filament_array_value(fields[:count], delim))
                continue
            out_lines.append(line)

        out = "\n".join(out_lines)
        if text.endswith("\n"):
            out += "\n"
        return out

    def _snapmaker_tool_remap_from_assignments(self) -> Dict[int, int]:
        """Mapa marcador virtual T4+ -> herramienta física T0-T3 para comandos auxiliares."""
        mapping: Dict[int, int] = {}
        symbol_to_tool = {}
        for s in SYMBOLS_ALL:
            try:
                symbol_to_tool[s] = core.human_to_tool(int(self.symbol_filament_vars[s].get()))
            except Exception:
                pass
        for t, r in sorted(self.assignments.items()):
            if not r.outer:
                continue
            sym = r.outer[0]
            if sym in symbol_to_tool:
                dst = symbol_to_tool[sym]
                if 0 <= dst < 4:
                    mapping[t] = dst
        return mapping

    def _snapmaker_tool_remap_from_redirect_comments(self, text: str) -> Dict[int, int]:
        """Lee comentarios AI_CMYK ya generados como respaldo: T15 -> T3, etc."""
        mapping: Dict[int, int] = {}
        for line in text.splitlines():
            m = re.search(r"marcador\s+filamento\s+\d+\s*/\s*T(\d+)\s+redirigido\s+a\s+filamento\s+\d+\s*/\s*T(\d+)", line, re.I)
            if m:
                src = int(m.group(1)); dst = int(m.group(2))
                if 0 <= dst < 4:
                    mapping[src] = dst
        return mapping

    def _remap_snapmaker_tool_tokens_text(self, text: str) -> str:
        """Reemplaza T4+ que quedaron en M104/M109/preheat/cooldown por T0-T3.

        El motor ya cambia las líneas Tn principales, pero Snapmaker todavía veía
        comandos como M104 S220 T15. Esos comandos no deben apuntar a marcadores.
        """
        if self.output_machine_var.get() != "snapmaker":
            return text
        mapping = self._snapmaker_tool_remap_from_assignments()
        mapping.update(self._snapmaker_tool_remap_from_redirect_comments(text))
        if not mapping:
            return text
        out_lines = []
        for line in text.splitlines():
            code, sep, comment = line.partition(";")
            def repl(m):
                n = int(m.group(1))
                if n in mapping:
                    return f"T{mapping[n]}"
                return m.group(0)
            code = re.sub(r"\bT(\d+)\b", repl, code)
            out_lines.append(code + (sep + comment if sep else ""))
        out = "\n".join(out_lines)
        if text.endswith("\n"):
            out += "\n"
        return out

    def _patch_snapmaker_json_metadata(self, text: str, colors: List[str], count: int = 4) -> Optional[str]:
        """Recorta listas JSON de filamento a 4 entradas para Snapmaker U1."""
        try:
            obj = json.loads(text)
        except Exception:
            return None

        fixed_colors = [(_norm_hex(c) if c else _norm_hex(core._default_palette_color(i))) for i, c in enumerate(colors[:count])]
        changed = False

        def walk(x):
            nonlocal changed
            if isinstance(x, dict):
                for k, v in list(x.items()):
                    lk = str(k).lower()
                    if lk in ("filament_colour", "filament_colors"):
                        x[k] = list(fixed_colors)
                        changed = True
                    elif lk.startswith("filament") and isinstance(v, list) and len(v) > count:
                        x[k] = v[:count]
                        changed = True
                    elif isinstance(v, (dict, list)):
                        walk(v)
            elif isinstance(x, list):
                for item in x:
                    if isinstance(item, (dict, list)):
                        walk(item)

        walk(obj)
        if not changed:
            return None
        return json.dumps(obj, ensure_ascii=False, indent=4) + "\n"

    def _base_override_color_list_for_output(self, original_meta: core.GcodeMetadata) -> List[str]:
        """Devuelve la lista ;filament_colour en el mismo orden real de herramientas T.

        v41 corrige un fallo importante: antes se cambiaba principalmente la línea
        del G-code interno, pero en .gcode.3mf Bambu/Orca también lee
        Metadata/project_settings.config y Metadata/plate_1.json. Eso podía hacer
        que la previsualización mostrara los colores base en un orden viejo.

        La regla aquí es: el color configurado para cada símbolo se guarda en el
        índice de herramienta que realmente usará el G-code. Ejemplo: si C está
        configurado como filamento 1, se escribe en T0; si K está configurado
        como filamento 4, se escribe en T3.
        """
        if self.output_machine_var.get() == "snapmaker":
            return self._snapmaker_physical_color_list_for_output(original_meta, 4)

        colors = list(original_meta.filament_colors or [])

        # Mantener al menos los filamentos que existen en el archivo original y
        # los que el usuario asignó como bases. No reordenamos por nombre;
        # respetamos el número de filamento/herramienta configurado.
        for s in SYMBOLS_ALL:
            try:
                tool = core.human_to_tool(int(self.symbol_filament_vars[s].get()))
            except Exception:
                continue
            if tool < 0:
                continue
            while len(colors) <= tool:
                colors.append(core._default_palette_color(len(colors)))
            colors[tool] = _norm_hex(self.symbol_hex_vars[s].get())

        return [(_norm_hex(c) if c else _norm_hex(core._default_palette_color(i))) for i, c in enumerate(colors)]

    def _replace_filament_colour_line(self, text: str, colors: List[str]) -> str:
        joined = ";".join(colors)
        lines = text.splitlines()
        replaced = False
        out_lines = []
        for line in lines:
            if re.match(r"^\s*;\s*filament_colour\s*=", line):
                prefix = line.split("=", 1)[0]
                out_lines.append(f"{prefix}= {joined}")
                replaced = True
            else:
                out_lines.append(line)
        if not replaced:
            out_lines.insert(0, f"; filament_colour = {joined}")
        return "\n".join(out_lines) + ("\n" if text.endswith("\n") else "")

    def _patch_json_filament_colors(self, text: str, colors: List[str]) -> Optional[str]:
        """Actualiza JSON/config de Bambu/Orca si contiene colores de filamento."""
        try:
            obj = json.loads(text)
        except Exception:
            return None

        changed = False
        if isinstance(obj, dict):
            if "filament_colour" in obj and isinstance(obj.get("filament_colour"), list):
                obj["filament_colour"] = list(colors)
                changed = True
            # plate_1.json usa filament_colors, no filament_colour.
            if "filament_colors" in obj and isinstance(obj.get("filament_colors"), list):
                obj["filament_colors"] = list(colors)
                changed = True

        if not changed:
            return None
        return json.dumps(obj, ensure_ascii=False, indent=4) + "\n"

    def _patch_textual_filament_arrays(self, text: str, colors: List[str]) -> str:
        """Fallback regex para archivos con JSON parcial o formato raro."""
        arr = json.dumps(colors, ensure_ascii=False, indent=8)
        # Reindentar aproximadamente para que quede legible dentro de configs.
        arr = arr.replace("\n", "\n    ")
        patterns = [
            r'("filament_colour"\s*:\s*)\[[\s\S]*?\]',
            r'("filament_colors"\s*:\s*)\[[\s\S]*?\]',
        ]
        out = text
        for pat in patterns:
            out = re.sub(pat, r'\1' + arr, out, count=1)
        return out

    def update_output_filament_colours(self, output_path: Path):
        """Actualiza colores base en TODOS los lugares relevantes del G-code/.gcode.3mf.

        v41: además del ; filament_colour dentro del G-code, actualiza:
        - Metadata/project_settings.config -> filament_colour
        - Metadata/plate_*.json -> filament_colors
        - md5 del G-code interno cuando existe

        Esto evita que Bambu/Orca muestre un orden viejo de filamentos aunque el
        G-code convertido use el orden configurado.
        """
        if not self.meta or not output_path.exists():
            return
        colors = self._base_override_color_list_for_output(self.meta)

        snapmaker_out = self.output_machine_var.get() == "snapmaker"

        # Archivo plano .gcode
        if core.output_wants_raw_gcode(output_path) or output_path.suffix.lower() == ".gcode":
            text = output_path.read_text(encoding="utf-8", errors="replace")
            text = self._replace_filament_colour_line(text, colors)
            if snapmaker_out:
                text = self._remap_snapmaker_tool_tokens_text(text)
                text = self._force_snapmaker_4_filament_text(text, colors, 4)
            output_path.write_text(text, encoding="utf-8")
            return

        # Contenedor .gcode.3mf / .3mf
        try:
            with zipfile.ZipFile(output_path, "r") as zin:
                infos = zin.infolist()
                file_data = {info.filename: zin.read(info.filename) for info in infos if not info.is_dir()}
        except zipfile.BadZipFile:
            text = output_path.read_text(encoding="utf-8", errors="replace")
            output_path.write_text(self._replace_filament_colour_line(text, colors), encoding="utf-8")
            return

        gcode_md5_updates: Dict[str, str] = {}
        new_file_data: Dict[str, bytes] = {}

        for name, data in file_data.items():
            lower = name.lower()
            new_data = data
            should_try_text = lower.endswith((".gcode", ".config", ".json", ".xml", ".model"))
            if should_try_text:
                try:
                    text = data.decode("utf-8")
                except Exception:
                    text = data.decode("utf-8", errors="ignore")

                changed_text: Optional[str] = None
                if snapmaker_out:
                    patched = self._replace_filament_colour_line(text, colors) if lower.endswith(".gcode") else text
                    if lower.endswith(".gcode"):
                        patched = self._remap_snapmaker_tool_tokens_text(patched)
                    patched = self._sanitize_snapmaker_orca_settings_text(patched)
                    js = self._patch_snapmaker_json_metadata(patched, colors, 4)
                    if js is not None:
                        patched = js
                    else:
                        patched = self._patch_textual_filament_arrays(patched, colors)
                        patched = self._force_snapmaker_4_filament_text(patched, colors, 4)
                    if patched != text:
                        changed_text = patched
                elif lower.endswith(".gcode") or "filament_colour" in text:
                    patched = self._replace_filament_colour_line(text, colors) if lower.endswith(".gcode") else text
                    js = self._patch_json_filament_colors(patched, colors)
                    if js is not None:
                        patched = js
                    else:
                        patched = self._patch_textual_filament_arrays(patched, colors)
                    if patched != text:
                        changed_text = patched
                elif "filament_colors" in text:
                    js = self._patch_json_filament_colors(text, colors)
                    if js is not None:
                        changed_text = js
                    else:
                        patched = self._patch_textual_filament_arrays(text, colors)
                        if patched != text:
                            changed_text = patched

                if changed_text is not None:
                    new_data = changed_text.encode("utf-8")
                    if lower.endswith(".gcode"):
                        gcode_md5_updates[name + ".md5"] = hashlib.md5(new_data).hexdigest()

            new_file_data[name] = new_data

        # Actualizar md5 si el contenedor trae Metadata/plate_1.gcode.md5.
        for md5_name, md5_hex in gcode_md5_updates.items():
            if md5_name in new_file_data:
                new_file_data[md5_name] = md5_hex.encode("utf-8")

        tmp = output_path.with_suffix(output_path.suffix + ".tmp")
        with zipfile.ZipFile(output_path, "r") as zin, zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.is_dir():
                    zout.writestr(info, b"")
                    continue
                data = new_file_data.get(info.filename, zin.read(info.filename))
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = info.external_attr
                zout.writestr(zi, data)
        tmp.replace(output_path)


    # ---------------- v49 layer compatibility ----------------
    def _patch_layer_markers_text_v49(self, text: str) -> Tuple[str, int]:
        """Inserta comentarios de capa compatibles con el motor v28.

        El motor histórico solo leía líneas del tipo:
            ; layer num/total_layer_count: 12/967

        Pero Snapmaker Orca/Orca/Bambu suelen exportar:
            ;LAYER_CHANGE
            ;Z:0.28
            ;HEIGHT:0.08

        Si no se traducía eso, el índice de capa quedaba siempre en 0. Resultado:
        el patrón exterior C/M/Y/K/W nunca avanzaba y en el laminador se veía una
        sola tonalidad en la pared exterior. Esta función conserva el G-code y añade
        una línea compatible en cada ;LAYER_CHANGE.
        """
        if not text:
            return text, 0
        # Si ya trae el formato antiguo, igual puede traer ;LAYER_CHANGE. Insertamos
        # solo donde falte para no duplicar capa en archivos Bambu antiguos.
        lines = text.splitlines()
        total = sum(1 for line in lines if line.strip().upper() == ";LAYER_CHANGE")
        if total <= 0:
            # Fallback: contar SET_PRINT_STATS_INFO CURRENT_LAYER si existe.
            max_current = -1
            for line in lines:
                m = re.search(r"CURRENT_LAYER\s*=\s*(\d+)", line)
                if m:
                    max_current = max(max_current, int(m.group(1)))
            total = max_current + 1 if max_current >= 0 else 0
        if total <= 0:
            return text, 0

        out_lines: List[str] = []
        layer_no = 0
        inserted = 0
        old_re = re.compile(r"^;\s*layer num/total_layer_count:\s*\d+/\d+", re.I)
        for idx, line in enumerate(lines):
            out_lines.append(line)
            if line.strip().upper() == ";LAYER_CHANGE":
                layer_no += 1
                # No dupliques si la siguiente línea ya es compatible.
                nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
                if not old_re.match(nxt):
                    out_lines.append(f"; layer num/total_layer_count: {layer_no}/{total} ; AI_CMYK_V49_LAYER_COMPAT")
                    inserted += 1
        # Conserva newline final si existía.
        patched = "\n".join(out_lines)
        if text.endswith("\n"):
            patched += "\n"
        return patched, inserted

    def _prepare_input_with_v49_layers(self, inp: Path) -> Tuple[Path, Optional[tempfile.TemporaryDirectory], int]:
        """Devuelve una entrada temporal con capas compatibles para core.convert_gcode.

        Soporta .gcode plano y .gcode.3mf/.3mf rebanado. En contenedores 3MF copia
        todo el ZIP y reemplaza solo el miembro interno de G-code.
        """
        tmpdir: Optional[tempfile.TemporaryDirectory] = None
        try:
            if core.is_zip_container(inp):
                member = core.find_gcode_member(inp)
                if not member:
                    return inp, None, 0
                with zipfile.ZipFile(inp, "r") as zin:
                    text = core.decode_gcode_bytes(zin.read(member))
                    patched, inserted = self._patch_layer_markers_text_v49(text)
                    if inserted <= 0:
                        return inp, None, 0
                    tmpdir = tempfile.TemporaryDirectory(prefix="ai_cmyk_v49_layers_")
                    tmp_path = Path(tmpdir.name) / inp.name
                    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                        for info in zin.infolist():
                            if info.is_dir():
                                zout.writestr(info, b"")
                                continue
                            if info.filename == member:
                                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                                zi.compress_type = zipfile.ZIP_DEFLATED
                                zi.external_attr = info.external_attr
                                zout.writestr(zi, patched.encode("utf-8"))
                            else:
                                zout.writestr(info, zin.read(info.filename))
                    return tmp_path, tmpdir, inserted
            else:
                text = inp.read_text(encoding="utf-8", errors="replace")
                patched, inserted = self._patch_layer_markers_text_v49(text)
                if inserted <= 0:
                    return inp, None, 0
                tmpdir = tempfile.TemporaryDirectory(prefix="ai_cmyk_v49_layers_")
                tmp_path = Path(tmpdir.name) / inp.name
                tmp_path.write_text(patched, encoding="utf-8")
                return tmp_path, tmpdir, inserted
        except Exception:
            if tmpdir is not None:
                tmpdir.cleanup()
            raise

    def convert_with_effective_base(self, inp: Path, out: Path, opts: core.ConvertOptions):
        """Convierte usando colores base editados y modelo óptico v46.

        v46: si la salida es Snapmaker, se fuerza a G-code plano .gcode cuando el
        usuario eligió .3mf/.gcode.3mf. Eso evita que Snapmaker Orca intente leer
        Metadata/plate_1.gcode del 3MF anterior y vuelva a cargar marcadores o falle.
        """
        actual_out = out
        snapmaker_forced_raw = False
        if self.output_machine_var.get() == "snapmaker" and not self._is_snapmaker_raw_output(out):
            actual_out = self._snapmaker_safe_raw_output_path(out)
            snapmaker_forced_raw = True
        self._last_actual_output_path = actual_out
        orig_read_metadata = core.read_metadata
        orig_perceived = getattr(core, "_v28_perceived_recipe_rgb", None)
        orig_parse_recipes = getattr(core, "_v28_parse_recipe_overrides", None)
        orig_plan_from_symbols = getattr(core, "_v28_recipe_plan_from_symbols", None)
        app = self

        def patched_read_metadata(path: Path):
            meta = orig_read_metadata(path)
            return app.apply_base_overrides_to_meta(meta)

        core.read_metadata = patched_read_metadata
        # El motor v28 también puede calcular recetas automáticas durante la conversión.
        # Lo parcheamos para que use el mismo modelo sustractivo que la interfaz.
        core._v28_perceived_recipe_rgb = perceived_recipe_rgb
        core._v28_parse_recipe_overrides = _v44_parse_recipe_overrides
        core._v28_recipe_plan_from_symbols = _v44_recipe_plan_from_symbols
        tmp_layer_dir = None
        layer_inserted = 0
        convert_input = inp
        try:
            convert_input, tmp_layer_dir, layer_inserted = self._prepare_input_with_v49_layers(inp)
            stats, report = core.convert_gcode(convert_input, actual_out, opts, dry_run=False)
        finally:
            core.read_metadata = orig_read_metadata
            if orig_perceived is not None:
                core._v28_perceived_recipe_rgb = orig_perceived
            if orig_parse_recipes is not None:
                core._v28_parse_recipe_overrides = orig_parse_recipes
            if orig_plan_from_symbols is not None:
                core._v28_recipe_plan_from_symbols = orig_plan_from_symbols
            if tmp_layer_dir is not None:
                tmp_layer_dir.cleanup()
        if layer_inserted:
            report += f"\n\n[v49] Capas Snapmaker/Orca reconocidas: se añadieron {layer_inserted} marcadores internos compatibles para que el patrón exterior avance por capa."
        try:
            self.update_output_filament_colours(actual_out)
        except Exception as exc:
            report += f"\n\nAviso: no pude actualizar ; filament_colour en la salida: {exc}"
        if snapmaker_forced_raw:
            report += f"\n\nSnapmaker seguro: elegiste un contenedor 3MF como salida, pero para evitar el error de Metadata/plate_1.gcode se generó un G-code plano aquí:\n{actual_out}"
        report += "\n\nModelo de color: v49 físico óptico + capas Snapmaker corregidas. Usa sRGB lineal, mezcla sustractiva por transmisión/absorción, promedio espacial + geométrico entre capas, distancia perceptual CIE L*a*b*, translucidez configurable por filamento y filtro ΔE para eliminar colores visualmente repetidos. Para salida Snapmaker se fuerza salida .gcode plana, metadata de 4 filamentos físicos T0-T3, remapeo de M104/M109 T4+, recorte de arrays heredadas de 16 filamentos y reconocimiento de ;LAYER_CHANGE para patrones exteriores reales."
        return stats, report

    # ---------------- metadata/options ----------------
    def browse_input(self):
        fn = filedialog.askopenfilename(filetypes=[("G-code", "*.gcode *.gcode.3mf *.3mf"), ("Todos", "*.*")])
        if not fn:
            return
        self.input_var.set(fn)
        p = Path(fn)
        if not self.output_var.get():
            suffix = ".gcode" if self.output_machine_var.get() == "snapmaker" else p.suffix
            out = p.with_name(p.stem + "_pattern_bridge" + suffix)
            self.output_var.set(str(out))
        self.refresh_metadata()

    def browse_output(self):
        filetypes = [("G-code plano", "*.gcode"), ("Todos", "*.*")] if self.output_machine_var.get() == "snapmaker" else [("G-code", "*.gcode *.gcode.3mf"), ("Todos", "*.*")]
        fn = filedialog.asksaveasfilename(defaultextension=".gcode", filetypes=filetypes)
        if fn:
            self.output_var.set(fn)

    def refresh_metadata(self):
        try:
            p = Path(self.input_var.get())
            if not p.exists():
                raise FileNotFoundError("Selecciona un archivo de entrada válido.")
            self.meta = core.read_metadata(p)
            self.update_symbol_hex_from_filament()
            self.refresh_base_color_tab()
            values = []
            start_tool = core.human_to_tool(int(self.process_from_var.get()))
            max_tool = max(len(self.meta.filament_colors) - 1, max(self.meta.tools_seen.keys()) if self.meta.tools_seen else -1)
            for t in range(max(0, start_tool), max_tool + 1):
                hx = _read_target_hex(self.meta, t)
                values.append(f"Filamento {core.tool_to_human(t)} / T{t} / {hx}")
            self.marker_combo.configure(values=values)
            if values:
                self.selected_marker_var.set(values[0])
            self.status_var.set(
                f"Metadata OK: {len(self.meta.filament_colors)} colores en metadata, "
                f"{sum(self.meta.tools_seen.values())} cambios T detectados."
            )
            self.refresh_filament_assignment_tab()
        except Exception as exc:
            self.meta = None
            messagebox.showerror("Error leyendo metadata", str(exc))

    def update_symbol_hex_from_filament(self):
        if not self.meta:
            return
        for s in SYMBOLS_ALL:
            human = int(self.symbol_filament_vars[s].get())
            tool = core.human_to_tool(human)
            self.symbol_hex_vars[s].set(_tool_color(self.meta, tool))

    def marker_tool(self) -> int:
        text = self.selected_marker_var.get()
        m = re.search(r"T(\d+)", text)
        if m:
            return int(m.group(1))
        m = re.search(r"Filamento\s+(\d+)", text)
        if m:
            return core.human_to_tool(int(m.group(1)))
        return core.human_to_tool(int(self.process_from_var.get()))

    def make_opts(self) -> core.ConvertOptions:
        opts = core.ConvertOptions()
        opts.mode = "recipe-library"
        opts.process_from_filament = int(self.process_from_var.get())
        opts.wall_loops = int(self.wall_loops_var.get())
        opts.output_machine = self.output_machine_var.get()
        opts.toolchange_mode = "snapmaker-u1" if opts.output_machine == "snapmaker" else "bambu-ams"
        opts.cyan_filament = int(self.symbol_filament_vars["C"].get())
        opts.magenta_filament = int(self.symbol_filament_vars["M"].get())
        opts.yellow_filament = int(self.symbol_filament_vars["Y"].get())
        opts.black_filament = int(self.symbol_filament_vars["K"].get())
        opts.white_filament = int(self.symbol_filament_vars["W"].get())
        opts.recipe_use_black = "K" in self.active_symbols()
        opts.recipe_use_white = "W" in self.active_symbols()
        opts.recipe_black_visible_darkness_threshold = 0.0
        opts.recipe_avoid_same_outer_inner = False
        # v45: el color lo calcula el modelo físico local; estas banderas del motor
        # antiguo quedan activadas solo para compatibilidad de conversión.
        opts.visible_translucency_compensation = True
        opts.sunlu_opaque_translucency_model = True
        opts.v45_symbol_translucency = {
            s: _clamp01(float(self.symbol_translucency_vars[s].get()))
            for s in SYMBOLS_ALL
        }
        # Manual map desde asignaciones
        opts.manual_map_text = self.manual_map_text_from_assignments()
        return opts

    def active_symbols(self) -> str:
        raw = self.active_symbols_var.get().upper().strip()
        result = ""
        for s in raw:
            if s in SYMBOLS_ALL and s not in result:
                result += s
        if not result:
            result = "CMYK"
        return result

    def recipe_layer_counts(self) -> List[int]:
        """Capas verticales de receta a generar.

        3 = receta clásica 33/33/33.
        2 = receta 50/50 real, alternada por capa en el G-code final.
        """
        return [3, 2] if bool(getattr(self, "include_two_layer_var", tk.BooleanVar(value=True)).get()) else [3]

    def filter_duplicate_color_codes(self, recipes: List[Recipe]) -> List[Recipe]:
        """v47: elimina colores repetidos reales y casi repetidos.

        Antes solo se quitaban recetas con el mismo HEX exacto y la opción venía
        apagada. Ahora el filtro viene activo y usa dos niveles:
        1) HEX exacto igual.
        2) diferencia perceptual muy baja en CIE L*a*b* (DeltaE aproximado).

        Esto evita que la paleta se llene con muchas recetas que visualmente son
        el mismo color, especialmente al sumar recetas de 2 capas y 3 capas.
        Conserva la receta con menor error al color objetivo y, en empate, la más
        simple/limpia para imprimir.
        """
        if not recipes:
            return recipes
        if not self.filter_same_hex_var.get():
            return recipes

        try:
            threshold = max(0.0, float(self.duplicate_deltae_var.get()))
        except Exception:
            threshold = 1.8

        def score(r: Recipe):
            # menor es mejor: parecido al marcador, menos cambios, receta más corta,
            # evita exterior=interior si hay alternativa, evita K exterior solo en empate.
            changes = sum(1 for i in range(1, len(r.outer)) if r.outer[i] != r.outer[i-1])
            changes += sum(1 for i in range(1, len(r.inner)) if r.inner[i] != r.inner[i-1])
            return (r.dist, changes, len(r.outer), int(r.has_same_layer), int(r.has_k_outer), r.outer, r.inner)

        # Primero colapsa HEX exactos conservando la mejor receta.
        exact: Dict[str, Recipe] = {}
        for r in recipes:
            key = r.hex.upper()
            old = exact.get(key)
            if old is None or score(r) < score(old):
                exact[key] = r

        if threshold <= 0.0:
            return list(exact.values())

        # Luego elimina colores perceptualmente casi iguales. Para no hacer O(n²),
        # usamos cubetas en LAB y revisamos solo vecindad cercana.
        ordered = sorted(exact.values(), key=score)
        cell = max(0.5, threshold)
        buckets: Dict[Tuple[int, int, int], List[Tuple[Tuple[float, float, float], Recipe]]] = {}
        kept: List[Recipe] = []

        def bucket_of(lab):
            return (int(math.floor(lab[0] / cell)), int(math.floor(lab[1] / cell)), int(math.floor(lab[2] / cell)))

        for r in ordered:
            lab = _rgb_to_lab(r.rgb)
            b = bucket_of(lab)
            duplicate = False
            for dx in (-1, 0, 1):
                if duplicate:
                    break
                for dy in (-1, 0, 1):
                    if duplicate:
                        break
                    for dz in (-1, 0, 1):
                        for old_lab, _old_r in buckets.get((b[0] + dx, b[1] + dy, b[2] + dz), []):
                            de = math.sqrt(
                                (lab[0] - old_lab[0]) ** 2 +
                                (lab[1] - old_lab[1]) ** 2 +
                                (lab[2] - old_lab[2]) ** 2
                            )
                            if de <= threshold:
                                duplicate = True
                                break
                        if duplicate:
                            break
            if duplicate:
                continue
            kept.append(r)
            buckets.setdefault(b, []).append((lab, r))
        return kept

    # ---------------- recipe generation ----------------
    def build_recipes_for_tool(self, target_tool: int, apply_limit: bool = False) -> List[Recipe]:
        """Genera todas las recetas posibles para un marcador según los filtros actuales."""
        if not self.meta:
            return []
        target_rgb = _read_target_rgb(self.meta, target_tool)
        # v45: sin modo oscuro especial ni filtros por K. La paleta completa se
        # calcula físicamente; los oscuros aparecen si el patrón y la translucidez lo permiten.
        dark_mode = False
        active = self.active_symbols()
        opts = self.make_opts()

        outer_symbols = active
        inner_symbols = active

        recipes: List[Recipe] = []
        for layer_count in self.recipe_layer_counts():
            for outer in map("".join, itertools.product(outer_symbols, repeat=layer_count)):
                for inner in map("".join, itertools.product(inner_symbols, repeat=layer_count)):
                    if self.avoid_same_var.get() and not self.show_same_var.get() and not dark_mode:
                        if any(outer[i] == inner[i] for i in range(layer_count)):
                            continue
                    rgb = perceived_recipe_rgb(outer, inner, self.effective_meta(), opts)
                    hx = core.rgb_to_hex(rgb)
                    dist = _rgb_distance(rgb, target_rgb)
                    recipes.append(Recipe(outer, inner, hx, rgb, dist))
        # v41: si el objetivo es oscuro, asegura que aparezcan candidatos realmente oscuros.
        # Algunos filtros pensados para colores claros eliminaban K/K o patrones repetidos,
        # justo los necesarios para negro, café oscuro y sombras profundas.
        if dark_mode and bool(getattr(self, "force_dark_candidates_var", tk.BooleanVar(value=True)).get()) and "K" in active:
            dark_outers = ["KKK", "KCK", "KMK", "KYK", "CKK", "MKK", "YKK"]
            dark_inners = ["KKK", "KWK", "KCK", "KMK", "KYK", "CKK", "MKK", "YKK"]
            for outer in dark_outers:
                if any(ch not in active for ch in outer):
                    continue
                for inner in dark_inners:
                    if any(ch not in active for ch in inner):
                        continue
                    rgb = perceived_recipe_rgb(outer, inner, self.effective_meta(), opts)
                    hx = core.rgb_to_hex(rgb)
                    dist = _rgb_distance(rgb, target_rgb)
                    recipes.append(Recipe(outer, inner, hx, rgb, dist))
        if self.sort_by_target_var.get():
            recipes.sort(key=lambda r: (r.dist, r.outer, r.inner))
        else:
            recipes.sort(key=lambda r: (r.outer, r.inner))
        recipes = self.filter_duplicate_color_codes(recipes)
        if self.sort_by_target_var.get():
            recipes.sort(key=lambda r: (r.dist, r.outer, r.inner))
        else:
            recipes.sort(key=lambda r: (r.outer, r.inner))
        if apply_limit:
            limit = int(self.limit_var.get() or 0)
            if limit > 0:
                recipes = recipes[:limit]
        return recipes

    def total_recipe_count_for_tool(self, target_tool: int) -> Tuple[int, str, str]:
        if not self.meta:
            return (0, "", "")
        darkness = 1.0 - core.luminance(_read_target_rgb(self.meta, target_tool))
        active = self.active_symbols()
        outer_symbols = active
        inner_symbols = active
        total = sum((len(outer_symbols) ** n) * (len(inner_symbols) ** n) for n in self.recipe_layer_counts())
        return (total, outer_symbols, inner_symbols)

    def recipe_from_patterns(self, outer: str, inner: str, target_tool: Optional[int] = None) -> Recipe:
        """Construye una Recipe desde texto manual CYC/KWK validando símbolos."""
        if not self.meta:
            raise ValueError("Primero carga metadata.")
        outer = (outer or "").strip().upper()
        inner = (inner or "").strip().upper()
        active = set(self.active_symbols())
        if len(outer) != len(inner) or len(outer) not in (2, 3):
            raise ValueError("El patrón exterior e interior deben tener 2 o 3 letras, por ejemplo CW/KW (50/50) o CYC/KWK.")
        bad = sorted((set(outer + inner) - active) | (set(outer + inner) - set(SYMBOLS_ALL)))
        if bad:
            raise ValueError("Símbolos no permitidos en receta: " + ", ".join(bad))
        opts = self.make_opts()
        rgb = perceived_recipe_rgb(outer, inner, self.effective_meta(), opts)
        hx = core.rgb_to_hex(rgb)
        dist = 0.0
        if target_tool is not None:
            dist = _rgb_distance(rgb, _read_target_rgb(self.meta, target_tool))
        return Recipe(outer, inner, hx, rgb, dist)

    def generate_palette(self):
        if not self.meta:
            self.refresh_metadata()
            if not self.meta:
                return
        target_tool = self.marker_tool()
        target_hex = _read_target_hex(self.meta, target_tool)
        total_count, outer_symbols, inner_symbols = self.total_recipe_count_for_tool(target_tool)
        recipes = self.build_recipes_for_tool(target_tool, apply_limit=True)

        self.recipes = recipes
        self.selected_recipe = recipes[0] if recipes else None
        self.palette_info.set(
            f"Marcador: filamento {core.tool_to_human(target_tool)} / T{target_tool} / {target_hex}  |  "
            f"Símbolos exterior: {outer_symbols}  interior: {inner_symbols}  |  "
            f"Combinaciones posibles antes de filtro: {total_count}  |  colores únicos/repetidos filtrados mostrados: {len(recipes)}"
        )
        self.draw_palette()

    def draw_palette(self):
        self.canvas.delete("all")
        self.row_recipe.clear()
        if not self.recipes or not self.meta:
            self.canvas.create_text(20, 20, anchor="nw", text="No hay recetas para mostrar.", fill="black")
            return
        # Headers
        row_h = 30
        y0 = 34
        cols = {
            "idx": 10,
            "perc": 72,
            "outer": 170,
            "inner": 315,
            "txt": 465,
            "dist": 650,
        }
        self.canvas.create_text(cols["idx"], 8, anchor="nw", text="#", fill="black", font=("Arial", 9, "bold"))
        self.canvas.create_text(cols["perc"], 8, anchor="nw", text="Color percibido", fill="black", font=("Arial", 9, "bold"))
        self.canvas.create_text(cols["outer"], 8, anchor="nw", text="Exterior 2/3 capas", fill="black", font=("Arial", 9, "bold"))
        self.canvas.create_text(cols["inner"], 8, anchor="nw", text="Interior 2/3 capas", fill="black", font=("Arial", 9, "bold"))
        self.canvas.create_text(cols["txt"], 8, anchor="nw", text="Receta", fill="black", font=("Arial", 9, "bold"))
        self.canvas.create_text(cols["dist"], 8, anchor="nw", text="Error", fill="black", font=("Arial", 9, "bold"))

        for i, r in enumerate(self.recipes):
            y = y0 + i * row_h
            if self.selected_recipe is r:
                self.canvas.create_rectangle(0, y - 2, 820, y + row_h - 2, fill="#E8F1FF", outline="")
            self.row_recipe[i] = r
            self.canvas.create_text(cols["idx"], y + 5, anchor="nw", text=str(i + 1), fill="black")
            self.canvas.create_rectangle(cols["perc"], y + 3, cols["perc"] + 75, y + 23, fill=r.hex, outline="#555555")
            self.canvas.create_text(cols["perc"] + 82, y + 5, anchor="nw", text=r.hex, fill="black")
            self._draw_pattern(cols["outer"], y + 3, r.outer)
            self._draw_pattern(cols["inner"], y + 3, r.inner)
            self.canvas.create_text(cols["txt"], y + 5, anchor="nw", text=f"{r.outer}:{r.inner}", fill="black", font=("Consolas", 10))
            warn = ""
            if r.has_same_layer:
                warn += "  =misma pared"
            if r.has_k_outer:
                warn += "  K exterior"
            self.canvas.create_text(cols["dist"], y + 5, anchor="nw", text=f"{r.dist:.1f}{warn}", fill="black")
        self.canvas.configure(scrollregion=(0, 0, 900, y0 + len(self.recipes) * row_h + 20))

    def _draw_pattern(self, x: int, y: int, pattern: str):
        box_w = 34
        for i, s in enumerate(pattern):
            hx = self.symbol_hex_vars.get(s, tk.StringVar(value=DEFAULT_HEX.get(s, "#888888"))).get()
            self.canvas.create_rectangle(x + i * box_w, y, x + (i + 1) * box_w - 2, y + 20, fill=_norm_hex(hx), outline="#555555")
            # Texto blanco/negro según luminancia
            rgb = core.hex_to_rgb(_norm_hex(hx))
            fg = "white" if core.luminance(rgb) < 0.35 else "black"
            self.canvas.create_text(x + i * box_w + 16, y + 10, text=s, fill=fg, font=("Arial", 9, "bold"))

    def on_canvas_click(self, event):
        row_h = 30
        y0 = 34
        y = self.canvas.canvasy(event.y)
        idx = int((y - y0) // row_h)
        if idx in self.row_recipe:
            self.selected_recipe = self.row_recipe[idx]
            self.draw_palette()
            self.status_var.set(f"Seleccionada receta {self.selected_recipe.outer}:{self.selected_recipe.inner} color≈{self.selected_recipe.hex}")


    # ---------------- v32 continuous visual map ----------------
    def _recipe_sort_key_visual(self, r: Recipe):
        rr, gg, bb = [v / 255.0 for v in r.rgb]
        h, sat, val = colorsys.rgb_to_hsv(rr, gg, bb)
        lum = core.luminance(r.rgb)
        # Neutros arriba, después hue. Dentro de cada fila, oscuro→claro.
        neutral = 0 if sat < 0.08 else 1
        hue_bin = -1 if neutral == 0 else int(h * 36)
        return (neutral, hue_bin, lum, sat, r.outer, r.inner)

    def generate_map2d(self):
        """Genera un mapa visual continuo tipo paleta: cada celda es una receta completa de 2 o 3 capas."""
        if not self.meta:
            self.refresh_metadata()
            if not self.meta:
                return
        target_tool = self.marker_tool()
        target_rgb = _read_target_rgb(self.meta, target_tool)
        self.layer_cell_records = self.build_layer_cells_for_tool(target_tool)
        recipes: List[Recipe] = []
        for layer_count in self.recipe_layer_counts():
            for combo in itertools.product(self.layer_cell_records, repeat=layer_count):
                outer = "".join(c[0] for c in combo)
                inner = "".join(c[1] for c in combo)
                if self.avoid_same_var.get() and not self.show_same_var.get():
                    if any(outer[i] == inner[i] for i in range(layer_count)):
                        continue
                rgb = perceived_recipe_rgb(outer, inner, self.effective_meta(), self.make_opts())
                hx = core.rgb_to_hex(rgb)
                dist = _rgb_distance(rgb, target_rgb)
                recipes.append(Recipe(outer, inner, hx, rgb, dist))

        raw_count = len(recipes)
        recipes = self.filter_duplicate_color_codes(recipes)
        # El mapa visual se ordena por color percibido, no por cercanía, para verse como paleta.
        recipes.sort(key=self._recipe_sort_key_visual)
        self.map2d_recipes = recipes
        self.selected_recipe = recipes[0] if recipes else None
        outer_symbols, inner_symbols = self._layer_outer_inner_symbols(target_tool)
        self.map2d_info.set(
            f"Mapa visual de recetas para filamento {core.tool_to_human(target_tool)} / T{target_tool}. "
            f"Una celda = receta de 2 o 3 capas exterior×interior. Base por capa: {len(outer_symbols)}×{len(inner_symbols)}. "
            f"Recetas únicas/repetidos filtrados: {len(recipes)} de {raw_count}. Haz clic en un color para ver su patrón."
        )
        self.draw_map2d()

    def draw_map2d(self):
        c = self.map2d_canvas
        c.delete("all")
        self.map2d_cell_recipe.clear()
        if not self.meta or not self.map2d_recipes:
            c.create_text(20, 20, anchor="nw", text="Pulsa 'Generar mapa visual'.", fill="black", font=("Arial", 11, "bold"))
            c.configure(scrollregion=(0, 0, 900, 650))
            return

        target_tool = self.marker_tool()
        target_hex = _read_target_hex(self.meta, target_tool)
        cell = self.map2d_cell
        cols = self.map2d_cols
        x0, y0 = 20, 145
        # Panel superior de detalle
        c.create_text(20, 12, anchor="nw", text="Mapa visual continuo: cada rectángulo es una receta completa", fill="black", font=("Arial", 12, "bold"))
        c.create_text(20, 38, anchor="nw", text=f"Color objetivo: filamento {core.tool_to_human(target_tool)} / T{target_tool} / {target_hex}", fill="black")
        c.create_rectangle(310, 34, 380, 62, fill=_norm_hex(target_hex), outline="#555555")

        if self.selected_recipe:
            r = self.selected_recipe
            c.create_text(20, 75, anchor="nw", text=f"Seleccionado: {r.outer}:{r.inner}  percibido {r.hex}  error {r.dist:.1f}", fill="black", font=("Arial", 10, "bold"))
            c.create_rectangle(310, 72, 380, 102, fill=r.hex, outline="#555555")
            c.create_text(405, 72, anchor="nw", text="Exterior 2/3 capas:", fill="black")
            self._draw_pattern_on_canvas(c, 520, 70, r.outer)
            c.create_text(405, 101, anchor="nw", text="Interior 2/3 capas:", fill="black")
            self._draw_pattern_on_canvas(c, 520, 99, r.inner)
            # Mostrar por capa como exterior/interior.
            lx = 660
            for k in range(len(r.outer)):
                o = r.outer[k]
                inn = r.inner[k]
                oh = self.symbol_hex_vars.get(o, tk.StringVar(value="#CCCCCC")).get()
                ih = self.symbol_hex_vars.get(inn, tk.StringVar(value="#CCCCCC")).get()
                c.create_text(lx + k * 90, 70, anchor="nw", text=f"Capa {k+1}", fill="black")
                c.create_rectangle(lx + k * 90, 92, lx + k * 90 + 34, 111, fill=_norm_hex(oh), outline="#555555")
                c.create_rectangle(lx + k * 90 + 36, 92, lx + k * 90 + 70, 111, fill=_norm_hex(ih), outline="#555555")
                c.create_text(lx + k * 90, 115, anchor="nw", text=f"{o}/{inn}", fill="black", font=("Consolas", 9))

        c.create_text(x0, y0 - 24, anchor="nw", text="Paleta: ordenada por color percibido. Haz clic en un rectángulo para ver/asignar su patrón.", fill="black")
        for idx, r in enumerate(self.map2d_recipes):
            row = idx // cols
            col = idx % cols
            x = x0 + col * cell
            y = y0 + row * cell
            self.map2d_cell_recipe[idx] = r
            outline = ""
            if self.selected_recipe is r:
                outline = "#000000"
            c.create_rectangle(x, y, x + cell, y + cell, fill=r.hex, outline=outline)
        rows = (len(self.map2d_recipes) + cols - 1) // cols
        c.configure(scrollregion=(0, 0, x0 + cols * cell + 40, y0 + rows * cell + 40))

    def on_map2d_click(self, event):
        if not self.map2d_recipes:
            return
        cell = self.map2d_cell
        cols = self.map2d_cols
        x0, y0 = 20, 145
        x = self.map2d_canvas.canvasx(event.x)
        y = self.map2d_canvas.canvasy(event.y)
        col = int((x - x0) // cell)
        row = int((y - y0) // cell)
        if col < 0 or row < 0 or col >= cols:
            return
        idx = row * cols + col
        if idx in self.map2d_cell_recipe:
            self.selected_recipe = self.map2d_cell_recipe[idx]
            self.draw_map2d()
            self.status_var.set(
                f"Seleccionada receta {self.selected_recipe.outer}:{self.selected_recipe.inner} "
                f"color≈{self.selected_recipe.hex}. Pulsa 'Asignar receta seleccionada'."
            )

    # ---------------- v32 layer palette 4x4 ----------------
    def _layer_outer_inner_symbols(self, target_tool: int) -> Tuple[str, str]:
        """Símbolos disponibles para exterior/interior en la paleta por capa."""
        if not self.meta:
            return ("", "")
        active = self.active_symbols()
        # v45: exterior e interior usan todos los símbolos activos. No se oculta K por reglas artificiales.
        return active, active

    def build_layer_cells_for_tool(self, target_tool: int) -> List[Tuple[str, str, str]]:
        """Genera la paleta por capa: cada celda es exterior x interior."""
        if not self.meta:
            return []
        opts = self.make_opts()
        outer_symbols, inner_symbols = self._layer_outer_inner_symbols(target_tool)
        cells: List[Tuple[str, str, str]] = []
        for outer in outer_symbols:
            for inner in inner_symbols:
                # Aquí NO eliminamos pares iguales, porque la paleta 4x4 debe mostrar CC, MM, etc.
                rgb = perceived_recipe_rgb(outer * 3, inner * 3, self.effective_meta(), opts)
                cells.append((outer, inner, core.rgb_to_hex(rgb)))
        return cells

    def generate_layer_palette(self):
        """Genera combinaciones de 3 capas y 2 capas 50/50 a partir de una grilla de capa exterior×interior."""
        if not self.meta:
            self.refresh_metadata()
            if not self.meta:
                return
        target_tool = self.marker_tool()
        target_rgb = _read_target_rgb(self.meta, target_tool)
        self.layer_cell_records = self.build_layer_cells_for_tool(target_tool)
        self.layer_combo_recipes = []
        self.layer_row_recipe.clear()
        if not self.layer_cell_records:
            self.layer_palette_info.set("No hay celdas disponibles. Revisa símbolos activos o metadata.")
            return

        recipes: List[Recipe] = []
        for layer_count in self.recipe_layer_counts():
            for combo in itertools.product(self.layer_cell_records, repeat=layer_count):
                outer = "".join(c[0] for c in combo)
                inner = "".join(c[1] for c in combo)
                if self.avoid_same_var.get() and not self.show_same_var.get():
                    # Ahora la regla se evalúa por capa: evita exterior==interior en la misma capa.
                    if any(outer[i] == inner[i] for i in range(layer_count)):
                        continue
                rgb = perceived_recipe_rgb(outer, inner, self.effective_meta(), self.make_opts())
                hx = core.rgb_to_hex(rgb)
                dist = _rgb_distance(rgb, target_rgb)
                recipes.append(Recipe(outer, inner, hx, rgb, dist))
        raw_count = len(recipes)
        if self.sort_by_target_var.get():
            recipes.sort(key=lambda r: (r.dist, r.outer, r.inner))
        else:
            recipes.sort(key=lambda r: (r.outer, r.inner))
        recipes = self.filter_duplicate_color_codes(recipes)
        if self.sort_by_target_var.get():
            recipes.sort(key=lambda r: (r.dist, r.outer, r.inner))
        else:
            recipes.sort(key=lambda r: (r.outer, r.inner))
        limit = int(self.limit_var.get() or 0)
        if limit > 0:
            shown = recipes[:limit]
        else:
            shown = recipes
        self.layer_combo_recipes = shown
        total_possible = raw_count
        unique_count = len(recipes)
        outer_symbols, inner_symbols = self._layer_outer_inner_symbols(target_tool)
        grid_name = f"{len(outer_symbols)}×{len(inner_symbols)}"
        self.layer_palette_info.set(
            f"Filamento {core.tool_to_human(target_tool)} / T{target_tool}: paleta por capa {grid_name} "
            f"({len(self.layer_cell_records)} colores por capa) → {total_possible} combinaciones de 2/3 capas, "
            f"{unique_count} colores únicos/repetidos filtrados. Mostrando {len(shown)}."
        )
        self.draw_layer_palette()

    def draw_layer_palette(self):
        c = self.layer_canvas
        c.delete("all")
        self.layer_row_recipe.clear()
        if not self.meta or not self.layer_cell_records:
            c.create_text(12, 12, anchor="nw", text="Genera la mezcla sustractiva CMY / PLA opaco para verla aquí.", fill="black")
            c.configure(scrollregion=(0, 0, 800, 600))
            return
        target_tool = self.marker_tool()
        outer_symbols, inner_symbols = self._layer_outer_inner_symbols(target_tool)

        # Grilla base de una capa: exterior × interior
        x0, y0 = 20, 30
        cell_w, cell_h = 86, 42
        c.create_text(x0, 8, anchor="nw", text="Paleta de UNA capa: filas = exterior visible, columnas = interior/respaldo", fill="black", font=("Arial", 10, "bold"))
        for j, ins in enumerate(inner_symbols):
            c.create_text(x0 + (j + 1) * cell_w + 10, y0 - 18, anchor="nw", text=f"int {ins}", fill="black", font=("Arial", 9, "bold"))
        cell_lookup = {(o, i): hx for o, i, hx in self.layer_cell_records}
        for i, outs in enumerate(outer_symbols):
            c.create_text(x0, y0 + i * cell_h + 10, anchor="nw", text=f"ext {outs}", fill="black", font=("Arial", 9, "bold"))
            for j, ins in enumerate(inner_symbols):
                hx = cell_lookup.get((outs, ins), "#CCCCCC")
                x = x0 + (j + 1) * cell_w
                y = y0 + i * cell_h
                c.create_rectangle(x, y, x + cell_w - 6, y + cell_h - 6, fill=_norm_hex(hx), outline="#555555")
                rgb = core.hex_to_rgb(_norm_hex(hx))
                fg = "white" if core.luminance(rgb) < 0.35 else "black"
                c.create_text(x + 7, y + 5, anchor="nw", text=f"{outs}/{ins}", fill=fg, font=("Arial", 9, "bold"))
                c.create_text(x + 7, y + 22, anchor="nw", text=hx, fill=fg, font=("Arial", 8))

        # Tabla de combinaciones de 3 capas
        table_y = y0 + max(1, len(outer_symbols)) * cell_h + 42
        c.create_text(20, table_y - 25, anchor="nw", text="Combinaciones de 2/3 capas: cada bloque es exterior/interior de esa capa", fill="black", font=("Arial", 10, "bold"))
        cols = {"idx": 20, "perc": 70, "l1": 170, "l2": 260, "l3": 350, "outer": 455, "inner": 570, "txt": 690, "err": 830}
        headers = [("#", "idx"), ("Color percibido", "perc"), ("Capa 1", "l1"), ("Capa 2", "l2"), ("Capa 3", "l3"), ("Exterior", "outer"), ("Interior", "inner"), ("Receta", "txt"), ("Error", "err")]
        for name, key in headers:
            c.create_text(cols[key], table_y, anchor="nw", text=name, fill="black", font=("Arial", 9, "bold"))
        row_h = 32
        start_y = table_y + 28
        for idx, r in enumerate(self.layer_combo_recipes):
            y = start_y + idx * row_h
            if self.selected_recipe is r:
                c.create_rectangle(0, y - 2, 980, y + row_h - 2, fill="#E8F1FF", outline="")
            self.layer_row_recipe[idx] = r
            c.create_text(cols["idx"], y + 6, anchor="nw", text=str(idx + 1), fill="black")
            c.create_rectangle(cols["perc"], y + 4, cols["perc"] + 82, y + 24, fill=r.hex, outline="#555555")
            c.create_text(cols["perc"] + 88, y + 6, anchor="nw", text=r.hex, fill="black")
            # Por capa: exterior/interior. Las recetas de 2 capas dejan la tercera columna vacía.
            for k, colkey in enumerate(["l1", "l2", "l3"]):
                x = cols[colkey]
                if k >= len(r.outer):
                    c.create_text(x + 6, y + 7, anchor="nw", text="—", fill="#777777", font=("Consolas", 9))
                    continue
                pair = r.outer[k] + "/" + r.inner[k]
                # swatch doble: arriba exterior, abajo interior
                o_hx = self.symbol_hex_vars.get(r.outer[k], tk.StringVar(value="#CCCCCC")).get()
                i_hx = self.symbol_hex_vars.get(r.inner[k], tk.StringVar(value="#CCCCCC")).get()
                c.create_rectangle(x, y + 3, x + 38, y + 14, fill=_norm_hex(o_hx), outline="#555555")
                c.create_rectangle(x, y + 14, x + 38, y + 25, fill=_norm_hex(i_hx), outline="#555555")
                c.create_text(x + 44, y + 7, anchor="nw", text=pair, fill="black", font=("Consolas", 9))
            self._draw_pattern_on_canvas(c, cols["outer"], y + 4, r.outer)
            self._draw_pattern_on_canvas(c, cols["inner"], y + 4, r.inner)
            c.create_text(cols["txt"], y + 6, anchor="nw", text=f"{r.outer}:{r.inner}", fill="black", font=("Consolas", 9))
            warn = ""
            if r.has_same_layer:
                warn += " =misma"
            if r.has_k_outer:
                warn += " K ext"
            c.create_text(cols["err"], y + 6, anchor="nw", text=f"{r.dist:.1f}{warn}", fill="black")
        c.configure(scrollregion=(0, 0, 980, start_y + max(1, len(self.layer_combo_recipes)) * row_h + 30))

    def _draw_pattern_on_canvas(self, canvas: tk.Canvas, x: int, y: int, pattern: str):
        box_w = 32
        for i, s in enumerate(pattern):
            hx = self.symbol_hex_vars.get(s, tk.StringVar(value=DEFAULT_HEX.get(s, "#888888"))).get()
            canvas.create_rectangle(x + i * box_w, y, x + (i + 1) * box_w - 2, y + 20, fill=_norm_hex(hx), outline="#555555")
            rgb = core.hex_to_rgb(_norm_hex(hx))
            fg = "white" if core.luminance(rgb) < 0.35 else "black"
            canvas.create_text(x + i * box_w + 15, y + 10, text=s, fill=fg, font=("Arial", 8, "bold"))

    def on_layer_canvas_click(self, event):
        if not self.layer_combo_recipes:
            return
        target_tool = self.marker_tool()
        outer_symbols, _inner_symbols = self._layer_outer_inner_symbols(target_tool)
        y0 = 30
        cell_h = 42
        table_y = y0 + max(1, len(outer_symbols)) * cell_h + 42
        start_y = table_y + 28
        row_h = 32
        y = self.layer_canvas.canvasy(event.y)
        idx = int((y - start_y) // row_h)
        if idx in self.layer_row_recipe:
            self.selected_recipe = self.layer_row_recipe[idx]
            self.draw_layer_palette()
            self.status_var.set(f"Seleccionada receta por capas {self.selected_recipe.outer}:{self.selected_recipe.inner} color≈{self.selected_recipe.hex}")

    # ---------------- visual filament assignment tab ----------------
    def _small_swatch(self, parent, hx: str, width: int = 54, height: int = 22, text: str = "") -> tk.Canvas:
        c = tk.Canvas(parent, width=width, height=height, highlightthickness=1, highlightbackground="#777777")
        c.create_rectangle(0, 0, width, height, fill=_norm_hex(hx), outline="")
        if text:
            rgb = core.hex_to_rgb(_norm_hex(hx))
            fg = "white" if core.luminance(rgb) < 0.35 else "black"
            c.create_text(width // 2, height // 2, text=text, fill=fg, font=("Arial", 8, "bold"))
        return c

    def _pattern_widget(self, parent, pattern: str, width: int = 90, height: int = 22) -> tk.Canvas:
        c = tk.Canvas(parent, width=width, height=height, highlightthickness=1, highlightbackground="#777777")
        pattern = (pattern or "--").upper()
        n = max(1, len(pattern))
        box_w = max(1, width // n)
        for i in range(n):
            s = pattern[i] if i < len(pattern) else "-"
            hx = self.symbol_hex_vars.get(s, tk.StringVar(value="#CCCCCC")).get() if s in SYMBOLS_ALL else "#CCCCCC"
            c.create_rectangle(i * box_w, 0, (i + 1) * box_w, height, fill=_norm_hex(hx), outline="#555555")
            if s in SYMBOLS_ALL:
                rgb = core.hex_to_rgb(_norm_hex(hx))
                fg = "white" if core.luminance(rgb) < 0.35 else "black"
                c.create_text(i * box_w + box_w // 2, height // 2, text=s, fill=fg, font=("Arial", 8, "bold"))
        return c

    def _marker_tools(self) -> List[int]:
        if not self.meta:
            return []
        start_tool = core.human_to_tool(int(self.process_from_var.get()))
        max_tool = max(len(self.meta.filament_colors) - 1, max(self.meta.tools_seen.keys()) if self.meta.tools_seen else -1)
        return list(range(max(0, start_tool), max_tool + 1))

    def _used_marker_tools(self) -> List[int]:
        """Marcadores virtuales realmente usados en el G-code.

        v48: antes se auto-generaban planes con el motor viejo v28 cuando el usuario
        no había asignado recetas. Eso hacía que el output no coincidiera con la
        paleta visual física. Ahora los marcadores usados se autoasignan aquí con
        la misma paleta v48 antes de convertir.
        """
        if not self.meta:
            return []
        start_tool = core.human_to_tool(int(self.process_from_var.get()))
        used = sorted(t for t, n in (self.meta.tools_seen or {}).items() if n > 0 and t >= start_tool)
        if used:
            return used
        return self._marker_tools()

    def ensure_auto_assignments_for_conversion(self) -> List[Tuple[int, Recipe]]:
        """Rellena recetas faltantes con el MISMO modelo que usa la paleta visual.

        Esto evita que la conversión caiga al auto-generador histórico v28, que aún
        tenía filtros viejos de K/oscuro y solo recetas de 3 capas.
        """
        added: List[Tuple[int, Recipe]] = []
        if not self.meta:
            return added
        for t in self._used_marker_tools():
            if t in self.assignments:
                continue
            recipes = self.build_recipes_for_tool(t, apply_limit=False)
            if recipes:
                self.assignments[t] = recipes[0]
                added.append((t, recipes[0]))
        return added

    def refresh_filament_assignment_tab(self):
        if not hasattr(self, "filament_frame"):
            return
        for child in self.filament_frame.winfo_children():
            child.destroy()
        self.assignment_row_vars.clear()
        if not self.meta:
            ttk.Label(self.filament_frame, text="Carga un G-code para ver los filamentos marcadores.").grid(row=0, column=0, sticky="w", padx=6, pady=6)
            return

        headers = ["Filamento original", "Original", "Asignado/perceptual", "Exterior 2/3 capas", "Interior 2/3 capas", "Patrón exterior", "Patrón interior", "Acciones"]
        for col, h in enumerate(headers):
            ttk.Label(self.filament_frame, text=h, font=("Arial", 9, "bold")).grid(row=0, column=col, sticky="w", padx=4, pady=(2, 6))

        tools = self._marker_tools()
        if not tools:
            ttk.Label(self.filament_frame, text="No hay filamentos desde el valor configurado como marcadores.").grid(row=1, column=0, sticky="w", padx=6, pady=6)
            return

        for r, t in enumerate(tools, start=1):
            original_hex = _read_target_hex(self.meta, t)
            assigned = self.assignments.get(t)
            outer_txt = tk.StringVar(value=assigned.outer if assigned else "")
            inner_txt = tk.StringVar(value=assigned.inner if assigned else "")
            perceived_hex = assigned.hex if assigned else "#DDDDDD"
            self.assignment_row_vars[t] = {"outer": outer_txt, "inner": inner_txt}

            ttk.Label(self.filament_frame, text=f"Filamento {core.tool_to_human(t)} / T{t}\n{original_hex}").grid(row=r, column=0, sticky="w", padx=4, pady=3)
            self._small_swatch(self.filament_frame, original_hex, text="orig").grid(row=r, column=1, padx=4, pady=3)
            sw_assigned = self._small_swatch(self.filament_frame, perceived_hex, text=(assigned.hex if assigned else "elegir"))
            sw_assigned.grid(row=r, column=2, padx=4, pady=3)
            sw_assigned.configure(cursor="hand2")
            sw_assigned.bind("<Button-1>", lambda _e, tool=t: self.open_recipe_selector_for_tool(tool))
            ttk.Entry(self.filament_frame, textvariable=outer_txt, width=8).grid(row=r, column=3, padx=4, pady=3)
            ttk.Entry(self.filament_frame, textvariable=inner_txt, width=8).grid(row=r, column=4, padx=4, pady=3)
            self._pattern_widget(self.filament_frame, assigned.outer if assigned else "").grid(row=r, column=5, padx=4, pady=3)
            self._pattern_widget(self.filament_frame, assigned.inner if assigned else "").grid(row=r, column=6, padx=4, pady=3)

            actions = ttk.Frame(self.filament_frame)
            actions.grid(row=r, column=7, sticky="w", padx=4, pady=3)
            ttk.Button(actions, text="Paleta visual", command=lambda tool=t: self.open_recipe_selector_for_tool(tool)).pack(side="left")
            ttk.Button(actions, text="Auto", command=lambda tool=t: self.auto_assign_marker(tool)).pack(side="left", padx=3)
            ttk.Button(actions, text="Guardar", command=lambda tool=t: self.apply_row_to_assignment(tool)).pack(side="left", padx=3)
            ttk.Button(actions, text="Limpiar", command=lambda tool=t: self.clear_assignment(tool)).pack(side="left", padx=3)

    def build_visual_recipes_for_tool(self, tool: int) -> List[Recipe]:
        """Genera el espectro visual de recetas para un filamento marcador.

        Se basa en la paleta por capa exterior×interior y combina 3 capas + recetas de 2 capas 50/50.
        El resultado se ordena como mapa de color continuo, no como lista técnica.
        """
        if not self.meta:
            return []
        target_rgb = _read_target_rgb(self.meta, tool)
        cells = self.build_layer_cells_for_tool(tool)
        recipes: List[Recipe] = []
        for layer_count in self.recipe_layer_counts():
            for combo in itertools.product(cells, repeat=layer_count):
                outer = "".join(c[0] for c in combo)
                inner = "".join(c[1] for c in combo)
                if self.avoid_same_var.get() and not self.show_same_var.get():
                    if any(outer[i] == inner[i] for i in range(layer_count)):
                        continue
                rgb = perceived_recipe_rgb(outer, inner, self.effective_meta(), self.make_opts())
                hx = core.rgb_to_hex(rgb)
                dist = _rgb_distance(rgb, target_rgb)
                recipes.append(Recipe(outer, inner, hx, rgb, dist))
        recipes = self.filter_duplicate_color_codes(recipes)
        recipes.sort(key=self._recipe_sort_key_visual)
        return recipes

    def open_recipe_selector_for_tool(self, tool: int):
        """Abre un selector visual tipo espectro para elegir la receta de un filamento."""
        if not self.meta:
            self.refresh_metadata()
            if not self.meta:
                return

        recipes = self.build_visual_recipes_for_tool(tool)
        if not recipes:
            messagebox.showwarning("Sin recetas", "No pude generar recetas visuales para este filamento.")
            return

        current = self.assignments.get(tool)
        target_hex = _read_target_hex(self.meta, tool)
        target_rgb = _read_target_rgb(self.meta, tool)
        selected = {"recipe": current if current else min(recipes, key=lambda r: r.dist)}

        win = tk.Toplevel(self.root)
        win.title(f"Elegir color percibido - Filamento {core.tool_to_human(tool)} / T{tool}")
        win.geometry("1160x760")
        win.transient(self.root)

        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text=f"Filamento marcador {core.tool_to_human(tool)} / T{tool}", font=("Arial", 11, "bold")).pack(side="left")
        ttk.Label(top, text="Color original:").pack(side="left", padx=(18, 4))
        orig_sw = self._small_swatch(top, target_hex, width=70, height=24, text=target_hex)
        orig_sw.pack(side="left")
        ttk.Label(top, text="  Haz clic en un cuadrado del espectro y luego pulsa Asignar.").pack(side="left", padx=12)

        detail_var = tk.StringVar()
        detail = ttk.Frame(win, padding=(8, 0, 8, 8))
        detail.pack(fill="x")
        ttk.Label(detail, textvariable=detail_var, wraplength=900).pack(side="left", anchor="w")
        ttk.Button(detail, text="Asignar seleccionado", command=lambda: assign_and_close(False)).pack(side="right", padx=4)
        ttk.Button(detail, text="Asignar y cerrar", command=lambda: assign_and_close(True)).pack(side="right", padx=4)

        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        canvas = tk.Canvas(body, bg="white", highlightthickness=0)
        sy = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        sx = ttk.Scrollbar(body, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        canvas.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")

        cell = 14
        cols = 64
        x0, y0 = 20, 150
        cell_to_recipe: Dict[int, Recipe] = {}

        def recipe_equal(a: Optional[Recipe], b: Recipe) -> bool:
            return bool(a and a.outer == b.outer and a.inner == b.inner)

        def update_detail(r: Optional[Recipe] = None):
            rr = r or selected["recipe"]
            if not rr:
                detail_var.set("Sin selección.")
                return
            capas = ", ".join(f"{i+1} {rr.outer[i]}/{rr.inner[i]}" for i in range(len(rr.outer)))
            detail_var.set(
                f"Seleccionado: {rr.hex}  |  receta {rr.outer}:{rr.inner}  |  "
                f"error vs original {rr.dist:.1f}  |  "
                f"capas: {capas}"
            )

        def draw():
            canvas.delete("all")
            cell_to_recipe.clear()
            canvas.create_text(20, 12, anchor="nw", text="Paleta visual de colores percibidos", fill="black", font=("Arial", 12, "bold"))
            canvas.create_text(20, 38, anchor="nw", text="Cada cuadrado representa una receta completa de 2 o 3 capas exterior/interior. El borde negro marca el color asignado actual; el borde azul marca la selección.", fill="black")
            canvas.create_text(20, 66, anchor="nw", text="Original", fill="black", font=("Arial", 9, "bold"))
            canvas.create_rectangle(80, 62, 155, 92, fill=_norm_hex(target_hex), outline="#555555")
            canvas.create_text(165, 68, anchor="nw", text=target_hex, fill="black")
            if current:
                canvas.create_text(260, 66, anchor="nw", text="Actual", fill="black", font=("Arial", 9, "bold"))
                canvas.create_rectangle(320, 62, 395, 92, fill=current.hex, outline="#000000", width=2)
                canvas.create_text(405, 68, anchor="nw", text=f"{current.hex}  {current.outer}:{current.inner}", fill="black")
            if selected["recipe"]:
                rr = selected["recipe"]
                canvas.create_text(20, 108, anchor="nw", text=f"Selección: {rr.hex}  {rr.outer}:{rr.inner}", fill="black", font=("Arial", 10, "bold"))
                canvas.create_rectangle(210, 103, 285, 132, fill=rr.hex, outline="#0077CC", width=3)
                canvas.create_text(310, 104, anchor="nw", text="Exterior:", fill="black")
                self._draw_pattern_on_canvas(canvas, 370, 101, rr.outer)
                canvas.create_text(490, 104, anchor="nw", text="Interior:", fill="black")
                self._draw_pattern_on_canvas(canvas, 550, 101, rr.inner)

            for idx, r in enumerate(recipes):
                row = idx // cols
                col = idx % cols
                x = x0 + col * cell
                y = y0 + row * cell
                cell_to_recipe[idx] = r
                canvas.create_rectangle(x, y, x + cell, y + cell, fill=r.hex, outline="")
                if recipe_equal(current, r):
                    canvas.create_rectangle(x, y, x + cell, y + cell, outline="#000000", width=2)
                if recipe_equal(selected["recipe"], r):
                    canvas.create_rectangle(x + 2, y + 2, x + cell - 2, y + cell - 2, outline="#0077CC", width=2)
            rows = (len(recipes) + cols - 1) // cols
            canvas.configure(scrollregion=(0, 0, x0 + cols * cell + 50, y0 + rows * cell + 50))
            update_detail()

        def click(event, assign_now: bool = False):
            x = canvas.canvasx(event.x)
            y = canvas.canvasy(event.y)
            col = int((x - x0) // cell)
            row = int((y - y0) // cell)
            if col < 0 or row < 0 or col >= cols:
                return
            idx = row * cols + col
            if idx not in cell_to_recipe:
                return
            selected["recipe"] = cell_to_recipe[idx]
            draw()
            if assign_now:
                assign_and_close(False)

        def assign_and_close(close: bool):
            rr = selected.get("recipe")
            if not rr:
                return
            self.assignments[tool] = rr
            self.selected_recipe = rr
            self.refresh_filament_assignment_tab()
            self.refresh_assignments_view()
            self.status_var.set(f"Asignado visualmente: filamento {core.tool_to_human(tool)} / T{tool} → {rr.outer}:{rr.inner} ≈ {rr.hex}")
            if close:
                win.destroy()

        canvas.bind("<Button-1>", lambda e: click(e, False))
        canvas.bind("<Double-Button-1>", lambda e: click(e, True))
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        canvas.bind("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
        canvas.bind("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))
        draw()

        # Centra la vista cerca del color actual o del más parecido al original.
        focus_recipe = current or min(recipes, key=lambda r: _rgb_distance(r.rgb, target_rgb))
        try:
            focus_idx = next(i for i, r in enumerate(recipes) if r.outer == focus_recipe.outer and r.inner == focus_recipe.inner)
            focus_row = focus_idx // cols
            rows = max(1, (len(recipes) + cols - 1) // cols)
            canvas.update_idletasks()
            canvas.yview_moveto(max(0.0, min(1.0, (focus_row - 8) / max(1, rows))))
        except StopIteration:
            pass

    def select_marker_and_generate(self, tool: int):
        if not self.meta:
            return
        target = f"Filamento {core.tool_to_human(tool)} / T{tool} / {_read_target_hex(self.meta, tool)}"
        self.selected_marker_var.set(target)
        self.generate_palette()
        self.status_var.set(f"Mostrando paleta visual para filamento {core.tool_to_human(tool)} / T{tool}.")

    def auto_assign_marker(self, tool: int):
        recipes = self.build_recipes_for_tool(tool, apply_limit=False)
        if not recipes:
            messagebox.showwarning("Sin recetas", "No pude generar recetas para este filamento.")
            return
        self.assignments[tool] = recipes[0]
        self.refresh_filament_assignment_tab()
        self.refresh_assignments_view()
        self.status_var.set(f"Auto: filamento {core.tool_to_human(tool)} / T{tool} → {recipes[0].outer}:{recipes[0].inner} ≈ {recipes[0].hex}")

    def auto_assign_all_markers(self):
        if not self.meta:
            self.refresh_metadata()
            if not self.meta:
                return
        count = 0
        for t in self._marker_tools():
            recipes = self.build_recipes_for_tool(t, apply_limit=False)
            if recipes:
                self.assignments[t] = recipes[0]
                count += 1
        self.refresh_filament_assignment_tab()
        self.refresh_assignments_view()
        self.status_var.set(f"Auto asignación lista para {count} filamentos marcadores.")

    def apply_row_to_assignment(self, tool: int):
        try:
            row = self.assignment_row_vars.get(tool)
            if not row:
                return
            outer = row["outer"].get()  # type: ignore[index,union-attr]
            inner = row["inner"].get()  # type: ignore[index,union-attr]
            recipe = self.recipe_from_patterns(outer, inner, target_tool=tool)
            self.assignments[tool] = recipe
            self.refresh_filament_assignment_tab()
            self.refresh_assignments_view()
            self.status_var.set(f"Guardado: filamento {core.tool_to_human(tool)} / T{tool} → {recipe.outer}:{recipe.inner} ≈ {recipe.hex}")
        except Exception as exc:
            messagebox.showerror("Receta inválida", str(exc))

    def apply_rows_to_assignments(self):
        if not self.meta:
            return
        errors = []
        applied = 0
        for t, row in list(self.assignment_row_vars.items()):
            outer = row["outer"].get().strip()  # type: ignore[index,union-attr]
            inner = row["inner"].get().strip()  # type: ignore[index,union-attr]
            if not outer and not inner:
                continue
            try:
                self.assignments[t] = self.recipe_from_patterns(outer, inner, target_tool=t)
                applied += 1
            except Exception as exc:
                errors.append(f"Filamento {core.tool_to_human(t)}: {exc}")
        self.refresh_filament_assignment_tab()
        self.refresh_assignments_view()
        if errors:
            messagebox.showerror("Algunas recetas no se aplicaron", "\n".join(errors[:10]))
        self.status_var.set(f"Recetas escritas aplicadas: {applied}.")

    def clear_assignment(self, tool: int):
        self.assignments.pop(tool, None)
        self.refresh_filament_assignment_tab()
        self.refresh_assignments_view()
        self.status_var.set(f"Asignación limpiada para filamento {core.tool_to_human(tool)} / T{tool}.")

    # ---------------- assignments/conversion ----------------
    def assign_selected(self):
        if not self.selected_recipe:
            messagebox.showwarning("Sin receta", "Selecciona una receta en la paleta visual.")
            return
        t = self.marker_tool()
        self.assignments[t] = self.selected_recipe
        self.refresh_filament_assignment_tab()
        self.refresh_assignments_view()
        self.status_var.set(f"Asignado filamento {core.tool_to_human(t)} / T{t} → {self.selected_recipe.outer}:{self.selected_recipe.inner}")

    def manual_map_text_from_assignments(self) -> str:
        lines = []
        for t, r in sorted(self.assignments.items()):
            lines.append(f"{core.tool_to_human(t)}:{r.outer}:{r.inner}")
        return "\n".join(lines)

    def refresh_assignments_view(self):
        self.assign_text.delete("1.0", "end")
        self.assign_text.insert("end", "Asignaciones actuales:\n\n")
        if not self.assignments:
            self.assign_text.insert("end", "Aún no hay asignaciones. Selecciona un marcador, elige una receta visual y pulsa 'Asignar'.\n")
            return
        for t, r in sorted(self.assignments.items()):
            hx = _read_target_hex(self.meta, t) if self.meta else "?"
            self.assign_text.insert("end", f"Filamento {core.tool_to_human(t)} / T{t} / original {hx}\n")
            self.assign_text.insert("end", f"  Exterior 2/3 capas: {r.outer}\n")
            self.assign_text.insert("end", f"  Interior 2/3 capas: {r.inner}\n")
            self.assign_text.insert("end", f"  Color percibido aprox: {r.hex}\n")
            self.assign_text.insert("end", f"  Mapa: {core.tool_to_human(t)}:{r.outer}:{r.inner}\n\n")
        self.assign_text.insert("end", "Mapa manual completo para v45/v28:\n")
        self.assign_text.insert("end", self.manual_map_text_from_assignments())

    def copy_manual_map_to_clipboard(self):
        text = self.manual_map_text_from_assignments()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set("Mapa manual copiado al portapapeles.")

    def convert(self):
        try:
            inp = Path(self.input_var.get())
            out = Path(self.output_var.get())
            if not inp.exists():
                raise FileNotFoundError("Selecciona un archivo de entrada válido.")
            if not self.assignments:
                if not messagebox.askyesno("Sin asignaciones", "No has asignado recetas manuales. ¿Quieres que el programa autoasigne con la MISMA paleta física v49 antes de convertir?"):
                    return
            auto_added = self.ensure_auto_assignments_for_conversion()
            opts = self.make_opts()
            stats, report = self.convert_with_effective_base(inp, out, opts)
            if auto_added:
                report += "\n\n[v49] Autoasignaciones físicas aplicadas antes de convertir:\n"
                for t, r in auto_added[:40]:
                    report += f"  Filamento {core.tool_to_human(t)} / T{t}: {r.outer}:{r.inner} ≈ {r.hex} error {r.dist:.1f}\n"
                if len(auto_added) > 40:
                    report += f"  ... y {len(auto_added)-40} más.\n"
            actual_out = getattr(self, "_last_actual_output_path", out)
            self.output_var.set(str(actual_out))
            self.status_var.set(f"Conversión lista: {actual_out.name}")
            messagebox.showinfo("Conversión lista", report[:3000] + ("\n..." if len(report) > 3000 else ""))
        except Exception as exc:
            messagebox.showerror("Error convirtiendo", str(exc))


def run_gui():
    root = tk.Tk()
    App(root)
    root.mainloop()


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bambu Palette Pattern Bridge v49 - patrones exteriores por capa corregidos")
    p.add_argument("input", nargs="?", type=Path)
    p.add_argument("output", nargs="?", type=Path)
    p.add_argument("--gui", action="store_true")
    p.add_argument("--output-machine", choices=["bambu", "snapmaker"], default="bambu")
    p.add_argument("--manual-map", default="", help="Recetas: 5:WK:KW | 6:CYC:KWK")
    p.add_argument("--process-from", type=int, default=5)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    if args.gui or not args.input:
        run_gui()
        return 0
    if not args.output:
        print("Falta archivo de salida. Usa --gui para interfaz visual.", file=sys.stderr)
        return 2
    opts = core.ConvertOptions()
    opts.mode = "recipe-library"
    opts.process_from_filament = args.process_from
    opts.output_machine = args.output_machine
    opts.toolchange_mode = "snapmaker-u1" if args.output_machine == "snapmaker" else "bambu-ams"
    opts.manual_map_text = args.manual_map.replace("|", "\n")
    cli_forced_snapmaker_raw = False
    cli_output = args.output
    if args.output_machine == "snapmaker" and not core.output_wants_raw_gcode(cli_output):
        low = cli_output.name.lower()
        if low.endswith(".gcode.3mf"):
            stem = cli_output.name[:-len(".gcode.3mf")]
        elif low.endswith(".3mf"):
            stem = cli_output.name[:-len(".3mf")]
        else:
            stem = cli_output.stem
        cli_output = cli_output.with_name(stem + "_snapmaker_safe.gcode")
        cli_forced_snapmaker_raw = True
    try:
        orig_perceived = getattr(core, "_v28_perceived_recipe_rgb", None)
        orig_parse_recipes = getattr(core, "_v28_parse_recipe_overrides", None)
        orig_plan_from_symbols = getattr(core, "_v28_recipe_plan_from_symbols", None)
        core._v28_perceived_recipe_rgb = perceived_recipe_rgb
        core._v28_parse_recipe_overrides = _v44_parse_recipe_overrides
        core._v28_recipe_plan_from_symbols = _v44_recipe_plan_from_symbols
        try:
            _stats, report = core.convert_gcode(args.input, cli_output, opts, dry_run=False)
        finally:
            if orig_perceived is not None:
                core._v28_perceived_recipe_rgb = orig_perceived
            if orig_parse_recipes is not None:
                core._v28_parse_recipe_overrides = orig_parse_recipes
            if orig_plan_from_symbols is not None:
                core._v28_recipe_plan_from_symbols = orig_plan_from_symbols
        if cli_forced_snapmaker_raw:
            report += f"\n\nSnapmaker seguro: salida 3MF cambiada a G-code plano:\n{cli_output}"
        report += "\n\nModelo de color: v49 físico óptico + capas Snapmaker corregidas. Usa sRGB lineal, mezcla sustractiva por transmisión/absorción, promedio espacial + geométrico entre capas, distancia perceptual CIE L*a*b*, translucidez configurable por filamento y filtro ΔE para eliminar colores visualmente repetidos. Para salida Snapmaker se fuerza salida .gcode plana."
        print(report)
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
