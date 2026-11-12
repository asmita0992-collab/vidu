"""
Reportes en terminal y exportación de resultados a JSON/CSV/TXT.

Rutas de salida (gestionadas desde vidup.py, no aquí):
  reportes/comparativas/json/
  reportes/comparativas/csv/
  reportes/comparativas/txt/

Este módulo solo construye el informe y lo exporta a las rutas que le
indique el llamador. No decide rutas por sí mismo.

Usa rich si está disponible; si no, cae a ANSI puro.
"""

from __future__ import annotations
import shutil
import threading
try:
    from rich.table import Table
except ImportError:
    Table = None  # type: ignore
from .presentacion import (
    CONSOLA, NC, NEGRITA, AZUL, AMARILLO, ROJO, imprimir, rich_escape, rich_box,
)

import csv
import io
import json
import os
import re
import sys
import time
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
ZONA_MEXICO = timezone(timedelta(hours=-6))
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .comparator import ResultadoComparacion

# ============================================================================
# Rich (opcional)
# ============================================================================
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.rule import Rule
    from rich.text import Text
    from rich.markup import escape as rich_escape
    from rich import box as rich_box
    TIENE_RICH = True
except ImportError:
    Console = None      # type: ignore
    Panel = None        # type: ignore
    Table = None        # type: ignore
    Rule = None         # type: ignore
    Text = None         # type: ignore
    rich_escape = lambda texto: str(texto)
    rich_box = None     # type: ignore
    TIENE_RICH = False

CONSOLA: Optional[Any] = (
    Console(highlight=False, soft_wrap=True)
    if TIENE_RICH and Console is not None
    else None
)

# ============================================================================
# Colores ANSI (fallback)
# ============================================================================
_USA_COLOR = (
    sys.stdout.isatty()
    and os.environ.get('NO_COLOR') is None
)


class C:
    if _USA_COLOR:
        RESET = '\033[0m'
        NEGRITA = '\033[1m'
        ROJO = '\033[91m'
        AMARILLO = '\033[93m'
        VERDE = '\033[92m'
        CIAN = '\033[96m'
        GRIS = '\033[90m'
        MAGENTA = '\033[95m'
    else:
        RESET = ''
        NEGRITA = ''
        ROJO = ''
        AMARILLO = ''
        VERDE = ''
        CIAN = ''
        GRIS = ''
        MAGENTA = ''


# ============================================================================
# Metadatos de tipo de hallazgo
# ============================================================================
_TIPO_META = {
    'duplicado_exacto':      ('DUPLICADO EXACTO',   'red'),
    'duplicado_visual':      ('DUPLICADO VISUAL',   'yellow'),
    'fragmento':             ('FRAGMENTO',          'cyan'),
    'posible_fragmento':     ('POSIBLE FRAGMENTO',  'magenta'),
    'posible_duplicado':     ('POSIBLE DUPLICADO',  'magenta'),
}


def _etiqueta_rich(tipo: str) -> str:
    label, color = _TIPO_META.get(tipo, (tipo.upper(), 'grey'))
    return f'[bold {color}][{label}][/]'


def _etiqueta_ansi(tipo: str) -> str:
    label, color = _TIPO_META.get(tipo, (tipo.upper(), 'grey'))
    mapa = {
        'red': C.ROJO, 'yellow': C.AMARILLO, 'cyan': C.CIAN,
        'magenta': C.MAGENTA, 'grey': C.GRIS,
    }
    return f'{mapa.get(color, C.GRIS)}{C.NEGRITA}[{label}]{C.RESET}'


# ============================================================================
# Utilidades de formato
# ============================================================================
def _fmt_seg(segundos: float) -> str:
    segundos = max(0.0, segundos)
    horas = int(segundos // 3600)
    minutos = int((segundos % 3600) // 60)
    seg = int(segundos % 60)
    return f'{horas:02d}:{minutos:02d}:{seg:02d}'


def _fmt_bytes(cantidad: int) -> str:
    if cantidad <= 0:
        return '0 B'
    if cantidad < 1024:
        return f'{cantidad} B'
    if cantidad < 1024 ** 2:
        return f'{cantidad / 1024:.1f} KB'
    if cantidad < 1024 ** 3:
        return f'{cantidad / (1024 ** 2):.1f} MB'
    return f'{cantidad / (1024 ** 3):.2f} GB'


def _sin_color(texto: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', texto)


def _texto_audio(r: ResultadoComparacion) -> str:
    if r.tipo in ('fragmento', 'posible_fragmento'):
        return 'no evaluado (sin alineación temporal)'
    if r.tipo == 'duplicado_exacto':
        return 'no evaluado (SHA256 suficiente)'
    if r.similitud_audio < 0:
        return 'sin huellas comparables'
    return f'{r.similitud_audio:.1%}'


def _criterio_humano(criterio: str) -> str:
    return {
        'tamanio': 'tamaño',
        'resolucion': 'resolución',
        'codec': 'códec',
        'bitrate_video': 'bitrate de vídeo',
        'empate': 'empate técnico',
        'fragmento': 'fragmento',
    }.get(criterio, criterio.replace('_', ' '))


# ============================================================================
# Bloques de reporte (rich y ANSI)
# ============================================================================
def _bloque_info_video_rich(info: Dict[str, Any]) -> List[str]:
    if not info:
        return []
    ancho, alto = info.get('resolucion', (0, 0))
    resolucion_txt = f'{ancho}x{alto}' if ancho and alto else 'N/D'
    bitrate = info.get('bitrate_kbps', 0.0)
    bitrate_txt = f'{bitrate:.0f} kbps' if bitrate > 0 else 'N/D'
    bitrate_video = info.get('bitrate_video_kbps', 0.0)
    bitrate_video_txt = (
        f'{round(bitrate_video)} kbps' if bitrate_video > 0
        else 'N/D (requiere metadatos de la pista)'
    )
    return [
        f'    [bright_cyan]Dispositivo     :[/] {rich_escape(str(info.get("dispositivo") or "N/D"))}',
        f'    [bright_cyan]ID dispositivo  :[/] {rich_escape(str(info.get("dispositivo_id") or "N/D"))}',
        f'    [grey]Resolución      :[/] {resolucion_txt}',
        f'    [grey]Duración        :[/] {_fmt_seg(info.get("duracion", 0.0)) if info.get("duracion", 0.0) else "N/D"}',
        f'    [grey]Códec video     :[/] {info.get("codec_video", "N/D") or "N/D"}',
        f'    [grey]Códec audio     :[/] {info.get("codec_audio", "N/D") or "N/D"}',
        f'    [grey]Tamaño archivo  :[/] {_fmt_bytes(info.get("tamanio_bytes", 0))}',
        f'    [grey]Bitrate aprox.  :[/] {bitrate_txt}',
        f'    [grey]Bitrate vídeo   :[/] {bitrate_video_txt}',
        f'    [grey]Frames huella   :[/] {info.get("frames_muestreados", 0)}',
    ]


def _bloque_info_video_ansi(info: Dict[str, Any]) -> List[str]:
    if not info:
        return []
    ancho, alto = info.get('resolucion', (0, 0))
    resolucion_txt = f'{ancho}x{alto}' if ancho and alto else 'N/D'
    bitrate = info.get('bitrate_kbps', 0.0)
    bitrate_txt = f'{bitrate:.0f} kbps' if bitrate > 0 else 'N/D'
    bitrate_video = info.get('bitrate_video_kbps', 0.0)
    bitrate_video_txt = (
        f'{round(bitrate_video)} kbps' if bitrate_video > 0
        else 'N/D (requiere metadatos de la pista)'
    )
    return [
        f'    {C.CIAN}Dispositivo     : {info.get("dispositivo") or "N/D"}{C.RESET}',
        f'    {C.CIAN}ID dispositivo  : {info.get("dispositivo_id") or "N/D"}{C.RESET}',
        f'    {C.GRIS}Resolución      : {resolucion_txt}{C.RESET}',
        f'    {C.GRIS}Duración        : {_fmt_seg(info.get("duracion", 0.0)) if info.get("duracion", 0.0) else "N/D"}{C.RESET}',
        f'    {C.GRIS}Códec video     : {info.get("codec_video", "N/D") or "N/D"}{C.RESET}',
        f'    {C.GRIS}Códec audio     : {info.get("codec_audio", "N/D") or "N/D"}{C.RESET}',
        f'    {C.GRIS}Tamaño archivo  : {_fmt_bytes(info.get("tamanio_bytes", 0))}{C.RESET}',
        f'    {C.GRIS}Bitrate aprox.  : {bitrate_txt}{C.RESET}',
        f'    {C.GRIS}Bitrate vídeo   : {bitrate_video_txt}{C.RESET}',
        f'    {C.GRIS}Frames huella   : {info.get("frames_muestreados", 0)}{C.RESET}',
    ]


def _bloque_recomendacion_rich(r: ResultadoComparacion) -> List[str]:
    rec = r.recomendacion
    if not rec:
        return []
    criterio = rec.get('criterio', '')

    if criterio == 'fragmento':
        return [
            '',
            '  [cyan][bold]Relación de fragmento:[/][/]',
            f'    [grey]Video largo  :[/] {rec.get("conservar_nombre", "")}',
            f'    [grey]Fragmento    :[/] {r.nombre_b}',
            f'    [grey]Motivo       :[/] {rec.get("motivo", "")}',
        ]

    eliminar = rec.get('eliminar_nombre', '')
    conservar = rec.get('conservar_nombre', '')
    motivo = rec.get('motivo', '')
    if not eliminar or not conservar:
        return []

    return [
        '',
        '  [green][bold]Recomendación:[/][/]',
        f'    [green]CONSERVAR  →[/]  {conservar} (ID {rec.get("conservar_dispositivo_id") or "N/D"})',
        f'    [red]ELIMINAR   →[/]  {eliminar} (ID {rec.get("eliminar_dispositivo_id") or "N/D"})',
        f'    [grey]Criterio   : {_criterio_humano(criterio)}[/]',
        f'    [grey]Motivo     : {motivo}[/]',
    ]


def _bloque_recomendacion_ansi(r: ResultadoComparacion) -> List[str]:
    rec = r.recomendacion
    if not rec:
        return []
    criterio = rec.get('criterio', '')

    if criterio == 'fragmento':
        return [
            '',
            f'  {C.CIAN}{C.NEGRITA}Relación de fragmento:{C.RESET}',
            f'    {C.GRIS}Video largo  : {rec.get("conservar_nombre", "")}{C.RESET}',
            f'    {C.GRIS}Fragmento    : {r.nombre_b}{C.RESET}',
            f'    {C.GRIS}Motivo       : {rec.get("motivo", "")}{C.RESET}',
        ]

    eliminar = rec.get('eliminar_nombre', '')
    conservar = rec.get('conservar_nombre', '')
    motivo = rec.get('motivo', '')
    if not eliminar or not conservar:
        return []

    return [
        '',
        f'  {C.VERDE}{C.NEGRITA}Recomendación:{C.RESET}',
        f'    {C.VERDE}CONSERVAR  →{C.RESET}  {conservar} (ID {rec.get("conservar_dispositivo_id") or "N/D"})',
        f'    {C.ROJO}ELIMINAR   →{C.RESET}  {eliminar} (ID {rec.get("eliminar_dispositivo_id") or "N/D"})',
        f'    {C.GRIS}Criterio   : {_criterio_humano(criterio)}{C.RESET}',
        f'    {C.GRIS}Motivo     : {motivo}{C.RESET}',
    ]


# ============================================================================
# Bloque de un hallazgo (rich)
# ============================================================================
def _bloque_rich(r: ResultadoComparacion, indice: int) -> str:
    es_fragmento = r.tipo in ('fragmento', 'posible_fragmento')
    etiq_a = 'VIDEO LARGO' if es_fragmento else 'VIDEO A'
    etiq_b = 'FRAGMENTO  ' if es_fragmento else 'VIDEO B'

    lineas: List[str] = [
        '[grey]' + '─' * 72 + '[/]',
        f'[bold]#{indice}[/]  {_etiqueta_rich(r.tipo)}  Confianza: [bold]{r.confianza:.1%}[/]',
        '',
        f'  [green]{etiq_a}[/]  {rich_escape(r.nombre_a)}',
        f'  [grey]↳ {rich_escape(r.ruta_a)}[/]',
    ]
    lineas.extend(_bloque_info_video_rich(r.info_a))
    lineas.append('')
    lineas.append(f'  [yellow]{etiq_b}[/]  {rich_escape(r.nombre_b)}')
    lineas.append(f'  [grey]↳ {rich_escape(r.ruta_b)}[/]')
    lineas.extend(_bloque_info_video_rich(r.info_b))
    lineas.append('')
    lineas.append(
        f'  Similitud visual: [bold]{r.similitud_visual:.1%}[/]   |   '
        f'Audio: [bold]{_texto_audio(r)}[/]'
    )

    if es_fragmento and r.offset_inicio is not None:
        inicio = _fmt_seg(r.offset_inicio)
        fin = _fmt_seg(r.offset_fin if r.offset_fin is not None else 0)
        lineas.append(
            f'  Posición en video largo: [cyan]{inicio}[/] → [cyan]{fin}[/]'
        )

    if r.notas:
        lineas.append(f'  [grey]Nota: {rich_escape(r.notas)}[/]')

    lineas.extend(_bloque_recomendacion_rich(r))
    return '\n'.join(lineas)


# ============================================================================
# Bloque de un hallazgo (ANSI)
# ============================================================================
def _bloque_ansi(r: ResultadoComparacion, indice: int) -> str:
    es_fragmento = r.tipo in ('fragmento', 'posible_fragmento')
    etiq_a = 'VIDEO LARGO' if es_fragmento else 'VIDEO A'
    etiq_b = 'FRAGMENTO  ' if es_fragmento else 'VIDEO B'

    lineas: List[str] = [
        f'{C.GRIS}{"─" * 72}{C.RESET}',
        f'{C.NEGRITA}#{indice}{C.RESET}  {_etiqueta_ansi(r.tipo)}  '
        f'Confianza: {C.NEGRITA}{r.confianza:.1%}{C.RESET}',
        '',
        f'  {C.VERDE}{etiq_a}{C.RESET}  {r.nombre_a}',
        f'  {C.GRIS}↳ {r.ruta_a}{C.RESET}',
    ]
    lineas.extend(_bloque_info_video_ansi(r.info_a))
    lineas.append('')
    lineas.append(f'  {C.AMARILLO}{etiq_b}{C.RESET}  {r.nombre_b}')
    lineas.append(f'  {C.GRIS}↳ {r.ruta_b}{C.RESET}')
    lineas.extend(_bloque_info_video_ansi(r.info_b))
    lineas.append('')
    lineas.append(
        f'  Similitud visual: {C.NEGRITA}{r.similitud_visual:.1%}{C.RESET}   |   '
        f'Audio: {C.NEGRITA}{_texto_audio(r)}{C.RESET}'
    )

    if es_fragmento and r.offset_inicio is not None:
        inicio = _fmt_seg(r.offset_inicio)
        fin = _fmt_seg(r.offset_fin if r.offset_fin is not None else 0)
        lineas.append(
            f'  Posición en video largo: {C.CIAN}{inicio}{C.RESET} → {C.CIAN}{fin}{C.RESET}'
        )

    if r.notas:
        lineas.append(f'  {C.GRIS}Nota: {r.notas}{C.RESET}')

    lineas.extend(_bloque_recomendacion_ansi(r))
    return '\n'.join(lineas)


# ============================================================================
# Reporte principal: versión rich
# ============================================================================
def _imprimir_reporte_rich(
    resultados: List[ResultadoComparacion],
    total_videos: int = 0,
    tiempo_seg: float = 0.0,
) -> None:
    from .database import info_dispositivo_actual
    assert CONSOLA is not None
    assert Rule is not None
    assert Table is not None
    assert rich_box is not None

    orden_tipos = [
        'duplicado_exacto', 'duplicado_visual',
        'fragmento', 'posible_fragmento', 'posible_duplicado',
    ]
    por_tipo: Dict[str, List[ResultadoComparacion]] = {t: [] for t in orden_tipos}
    for r in resultados:
        if r.tipo not in por_tipo:
            por_tipo[r.tipo] = []
            orden_tipos.append(r.tipo)
        por_tipo[r.tipo].append(r)

    info_disp = info_dispositivo_actual()

    CONSOLA.print(Rule('[bold bright_white]VIDUP — Reporte de análisis[/]',
                       style='bright_cyan'))
    CONSOLA.print(
        f'[deep_sky_blue1]Fecha:[/] [spring_green2]{datetime.now(ZONA_MEXICO).strftime("%d/%m/%Y %H:%M:%S")}[/]\n'
        f'[deep_sky_blue1]Dispositivo:[/] [turquoise2]{info_disp["dispositivo"]}[/] '
        f'[bright_black](ID {info_disp["dispositivo_id"]})[/]\n'
        f'[deep_sky_blue1]Plataforma:[/] [gold1]{info_disp["plataforma"]}[/] [grey58]({info_disp["maquina"]})[/]'
    )
    if total_videos:
        CONSOLA.print(f'[deep_sky_blue1]Vídeos analizados:[/] [bold spring_green2]{total_videos}[/]')
    if tiempo_seg:
        CONSOLA.print(f'[deep_sky_blue1]Tiempo de análisis:[/] [bold gold1]{_fmt_seg(tiempo_seg)}[/]')
    CONSOLA.print(f'[deep_sky_blue1]Hallazgos totales:[/] [bold spring_green2]{len(resultados)}[/]')
    CONSOLA.print(Rule(style='bright_cyan'))

    if not resultados:
        CONSOLA.print('\n[green]No se encontraron duplicados ni fragmentos.[/]\n')
        return

    # Resumen por tipo
    tabla = Table(title='Resumen', box=rich_box.ROUNDED, border_style='bright_cyan', title_style='bold plum1')
    tabla.add_column('Tipo', style='bold spring_green2')
    tabla.add_column('Cantidad', style='bold gold1', justify='right')
    for tipo in orden_tipos:
        if not por_tipo[tipo]:
            continue
        label, color = _TIPO_META.get(tipo, (tipo.upper(), 'grey'))
        tabla.add_row(f'[{color}]{label}[/]', f'[bold gold1]{len(por_tipo[tipo])}[/]')
        if tipo == 'duplicado_exacto':
            subgrupos_count = {}
            for r in por_tipo[tipo]:
                d1 = str(r.info_a.get('dispositivo') or 'N/D')
                d2 = str(r.info_b.get('dispositivo') or 'N/D')
                sg = " y ".join(sorted([d1, d2]))
                subgrupos_count[sg] = subgrupos_count.get(sg, 0) + 1
            for sg, cant in subgrupos_count.items():
                tabla.add_row(f'    [cyan]Duplicados de {rich_escape(sg)}[/]', f'[spring_green2]{cant}[/]')
    CONSOLA.print(tabla)

    indice = 1
    for tipo in orden_tipos:
        grupo = por_tipo[tipo]
        if not grupo:
            continue
            
        if tipo == 'duplicado_exacto':
            def _llave_disp(r):
                d1 = str(r.info_a.get('dispositivo') or 'N/D')
                d2 = str(r.info_b.get('dispositivo') or 'N/D')
                return " y ".join(sorted([d1, d2]))
            grupo.sort(key=_llave_disp)
            
        label, color = _TIPO_META.get(tipo, (tipo.upper(), 'grey'))
        CONSOLA.print(Rule(
            f'[{color}][bold]{label} ({len(grupo)})[/][/]',
            style=color,
        ))
        
        ultimo_subgrupo = None
        for r in grupo:
            if tipo == 'duplicado_exacto':
                subgrupo = _llave_disp(r)
                if subgrupo != ultimo_subgrupo:
                    count_sub = sum(1 for item in grupo if _llave_disp(item) == subgrupo)
                    CONSOLA.print()
                    CONSOLA.print()
                    CONSOLA.print(f'[bright_blue]----------DUPLICADOS DE [bold turquoise2]{rich_escape(subgrupo)}[/] [gold1]({count_sub})[/]---------------------[/]')
                    ultimo_subgrupo = subgrupo
                    
            CONSOLA.print(_bloque_rich(r, indice))
            indice += 1

    CONSOLA.print(Rule(style='bright_cyan'))


# ============================================================================
# Reporte principal: versión ANSI
# ============================================================================
def _imprimir_reporte_ansi(
    resultados: List[ResultadoComparacion],
    total_videos: int = 0,
    tiempo_seg: float = 0.0,
) -> None:
    from .database import info_dispositivo_actual

    orden_tipos = [
        'duplicado_exacto', 'duplicado_visual',
        'fragmento', 'posible_fragmento', 'posible_duplicado',
    ]
    por_tipo: Dict[str, List[ResultadoComparacion]] = {t: [] for t in orden_tipos}
    for r in resultados:
        if r.tipo not in por_tipo:
            por_tipo[r.tipo] = []
            orden_tipos.append(r.tipo)
        por_tipo[r.tipo].append(r)

    info_disp = info_dispositivo_actual()
    sep = f'{C.GRIS}{"═" * 72}{C.RESET}'

    print()
    print(sep)
    print(f'{C.NEGRITA}  VIDUP — Reporte de análisis{C.RESET}')
    print(f'  Fecha: {datetime.now(ZONA_MEXICO).strftime("%d/%m/%Y %H:%M:%S")}')
    print(f'  Dispositivo: {info_disp["dispositivo"]} (ID {info_disp["dispositivo_id"]})')
    print(f'  Plataforma: {info_disp["plataforma"]} ({info_disp["maquina"]})')
    if total_videos:
        print(f'  Videos analizados : {total_videos}')
    if tiempo_seg:
        print(f'  Tiempo de análisis: {_fmt_seg(tiempo_seg)}')
    print(f'  Hallazgos totales : {C.NEGRITA}{len(resultados)}{C.RESET}')
    print(sep)

    if not resultados:
        print(f'\n  {C.VERDE}No se encontraron duplicados ni fragmentos.{C.RESET}\n')
        return

    print(f'\n  {C.NEGRITA}Resumen:{C.RESET}')
    for tipo in orden_tipos:
        cantidad = len(por_tipo[tipo])
        if not cantidad:
            continue
        label, color = _TIPO_META.get(tipo, (tipo.upper(), 'grey'))
        mapa = {
            'red': C.ROJO, 'yellow': C.AMARILLO, 'cyan': C.CIAN,
            'magenta': C.MAGENTA, 'grey': C.GRIS,
        }
        c = mapa.get(color, C.GRIS)
        print(f'    {c}{label:<20}{C.RESET}  {cantidad}')
        if tipo == 'duplicado_exacto':
            subgrupos_count = {}
            for r in por_tipo[tipo]:
                d1 = str(r.info_a.get('dispositivo') or 'N/D')
                d2 = str(r.info_b.get('dispositivo') or 'N/D')
                sg = " y ".join(sorted([d1, d2]))
                subgrupos_count[sg] = subgrupos_count.get(sg, 0) + 1
            for sg, cant in subgrupos_count.items():
                print(f'        Duplicados de {sg}: {cant}')

    indice = 1
    for tipo in orden_tipos:
        grupo = por_tipo[tipo]
        if not grupo:
            continue
            
        if tipo == 'duplicado_exacto':
            def _llave_disp(r):
                d1 = str(r.info_a.get('dispositivo') or 'N/D')
                d2 = str(r.info_b.get('dispositivo') or 'N/D')
                return " y ".join(sorted([d1, d2]))
            grupo.sort(key=_llave_disp)
            
        label, color = _TIPO_META.get(tipo, (tipo.upper(), 'grey'))
        mapa = {
            'red': C.ROJO, 'yellow': C.AMARILLO, 'cyan': C.CIAN,
            'magenta': C.MAGENTA, 'grey': C.GRIS,
        }
        c = mapa.get(color, C.GRIS)
        print(f'\n{c}{C.NEGRITA}  {"─" * 10} {label} ({len(grupo)}) {"─" * 10}{C.RESET}')
        
        ultimo_subgrupo = None
        for r in grupo:
            if tipo == 'duplicado_exacto':
                subgrupo = _llave_disp(r)
                if subgrupo != ultimo_subgrupo:
                    count_sub = sum(1 for item in grupo if _llave_disp(item) == subgrupo)
                    print()
                    print()
                    print(f'{C.GRIS}----------DUPLICADOS DE {subgrupo} ({count_sub})---------------------{C.RESET}')
                    ultimo_subgrupo = subgrupo
                    
            print(_bloque_ansi(r, indice))
            indice += 1

    print('\n' + sep + '\n')


# ============================================================================
# Construcción del informe (fuente única para terminal y archivos)
# ============================================================================
def construir_informe(
    resultados: List[ResultadoComparacion],
    total_videos: int = 0,
    tiempo_seg: float = 0.0,
    metadatos: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Construye el informe completo. Es la fuente única para terminal y archivos."""
    from .database import info_dispositivo_actual

    orden = list(_TIPO_META)
    ordenados = sorted(
        resultados,
        key=lambda r: orden.index(r.tipo) if r.tipo in orden else len(orden),
    )

    salida = io.StringIO()
    with redirect_stdout(salida):
        _imprimir_reporte_ansi(ordenados, total_videos, tiempo_seg)
    texto_terminal = _sin_color(salida.getvalue())

    meta = dict(metadatos or {})
    meta.setdefault('dispositivo', info_dispositivo_actual())

    cobertura = meta.get('comparativas', {})
    if cobertura:
        resumen = [
            'RESUMEN DE COMPARATIVAS',
            f'Pares únicos previstos : {cobertura.get("total_pares", 0)}',
            f'Pares evaluados        : {cobertura.get("pares_evaluados", 0)}',
            f'Cobertura completa     : {"sí" if cobertura.get("cobertura_completa") else "no"}',
            f'Alias del mismo archivo: {cobertura.get("mismo_archivo_fisico", 0)}',
            f'Intervalos incompatibles sin SHA256 coincidente: {cobertura.get("intervalos_incompatibles", 0)}',
            f'Sin huellas visuales ni SHA256 coincidente: {cobertura.get("sin_huellas_visuales", 0)}',
            'Cada par se evalúa una vez; A/B y B/A representan el mismo par.',
            'Las coincidencias visuales están sujetas al muestreo y a los umbrales elegidos.',
            '', 'COBERTURA POR VÍDEO',
        ]
        resumen.extend(
            f'{v["nombre"]} | {v["ruta"]} | {v["pares_evaluados"]} comparativas'
            for v in cobertura.get('por_video', [])
        )
        texto_terminal += '\n'.join(resumen) + '\n'

    if meta.get('bases_de_datos'):
        texto_terminal += '\nBases de datos: ' + ' | '.join(meta['bases_de_datos']) + '\n'
    if meta.get('dispositivo'):
        d = meta['dispositivo']
        texto_terminal += (
            f'Dispositivo que ejecutó el análisis: {d.get("dispositivo", "N/D")} '
            f'(ID {d.get("dispositivo_id", "N/D")}, '
            f'plataforma {d.get("plataforma", "N/D")})\n'
        )

    registros = []
    for indice, r in enumerate(ordenados, 1):
        registro = asdict(r)
        registro.update(
            indice=indice,
            tipo_legible=_TIPO_META.get(r.tipo, (r.tipo.replace('_', ' '), ''))[0],
            confianza_texto=f'{r.confianza:.1%}',
            similitud_visual_texto=f'{r.similitud_visual:.1%}',
            audio_texto=_texto_audio(r),
            criterio_texto=_criterio_humano(r.recomendacion.get('criterio', '')),
            texto_terminal=_sin_color(_bloque_ansi(r, indice)),
        )
        registros.append(registro)

    return dict(
        version=2,
        fecha=datetime.now(ZONA_MEXICO).isoformat(),
        total_videos=total_videos,
        tiempo_seg=tiempo_seg,
        total_hallazgos=len(registros),
        metadatos=meta,
        resultados=registros,
        texto_terminal=texto_terminal,
    )


# ============================================================================
# Impresión y exportación
# ============================================================================
def imprimir_reporte(
    resultados: List[ResultadoComparacion],
    total_videos: int = 0,
    tiempo_seg: float = 0.0,
    ruta_recomendaciones: Optional[str] = None,
    informe: Optional[Dict[str, Any]] = None,
) -> None:
    datos = informe or construir_informe(resultados, total_videos, tiempo_seg)

    if CONSOLA is not None and Table is not None and rich_box is not None:
        _imprimir_reporte_rich(
            [ResultadoComparacion(**{k: v for k, v in r.items()
                                     if k in ResultadoComparacion.__dataclass_fields__})
             for r in datos['resultados']],
            total_videos=datos.get('total_videos', 0),
            tiempo_seg=datos.get('tiempo_seg', 0.0),
        )
    else:
        print(datos['texto_terminal'], end='')

    if ruta_recomendaciones:
        exportar_txt(datos, ruta_recomendaciones)


def exportar_txt(informe: Dict[str, Any], ruta_salida: str) -> None:
    destino = Path(ruta_salida)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(informe['texto_terminal'], encoding='utf-8')


def exportar_json(
    resultados: List[ResultadoComparacion],
    ruta_salida: str,
    metadatos: Optional[Dict[str, Any]] = None,
    informe: Optional[Dict[str, Any]] = None,
) -> None:
    datos = informe or construir_informe(resultados, metadatos=metadatos)
    destino = Path(ruta_salida)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        json.dumps(datos, ensure_ascii=False, indent=4) + '\n',
        encoding='utf-8',
    )


def _aplanar(registro: Dict[str, Any], prefijo: str = '') -> Dict[str, Any]:
    plano: Dict[str, Any] = {}
    for clave, valor in registro.items():
        nombre = prefijo + clave
        if isinstance(valor, dict):
            plano.update(_aplanar(valor, nombre + '.'))
        elif isinstance(valor, (list, tuple)):
            plano[nombre] = json.dumps(valor, ensure_ascii=False)
        else:
            plano[nombre] = valor
    return plano


def exportar_csv(
    resultados: List[ResultadoComparacion],
    ruta_salida: str,
    informe: Optional[Dict[str, Any]] = None,
) -> None:
    datos = informe or construir_informe(resultados)

    filas = [dict(
        registro='resumen',
        texto_terminal=datos['texto_terminal'],
        total_videos=datos['total_videos'],
        tiempo_seg=datos['tiempo_seg'],
        total_hallazgos=datos['total_hallazgos'],
        metadatos=json.dumps(datos['metadatos'], ensure_ascii=False),
    )]
    filas.extend(dict(registro='hallazgo', **_aplanar(r)) for r in datos['resultados'])

    columnas = list(dict.fromkeys(clave for fila in filas for clave in fila))
    destino = Path(ruta_salida)
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open('w', newline='', encoding='utf-8-sig') as archivo:
        escritor = csv.DictWriter(archivo, fieldnames=columnas)
        escritor.writeheader()
        escritor.writerows(filas)


def exportar_candidatos_a_eliminar(
    informe: Dict[str, Any],
    carpeta_base: Path,
    identificador: str,
) -> None:
    """Genera reportes de candidatos a eliminar en CSV, TXT y JSON agrupados por dispositivo."""
    resultados = informe.get('resultados', [])
    candidatos_map: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for r in resultados:
        rec = r.get('recomendacion') if isinstance(r, dict) else getattr(r, 'recomendacion', {})
        if not rec or not isinstance(rec, dict):
            continue
        eliminar_ruta = rec.get('eliminar_ruta') or ''
        eliminar_nombre = rec.get('eliminar_nombre') or ''
        if not eliminar_ruta and not eliminar_nombre:
            continue

        elim_disp_id = str(rec.get('eliminar_dispositivo_id') or 'N/D')
        elim_disp = str(rec.get('eliminar_dispositivo') or 'Dispositivo no identificado')
        clave = (elim_disp_id, eliminar_ruta or eliminar_nombre)

        if clave not in candidatos_map:
            candidatos_map[clave] = {
                'dispositivo': elim_disp,
                'dispositivo_id': elim_disp_id,
                'nombre': eliminar_nombre,
                'ruta_completa': eliminar_ruta or eliminar_nombre,
                'criterio': rec.get('criterio', 'duplicado'),
                'motivo': rec.get('motivo', ''),
                'conservar_nombre': rec.get('conservar_nombre', ''),
                'conservar_ruta': rec.get('conservar_ruta', ''),
                'conservar_dispositivo': rec.get('conservar_dispositivo', ''),
                'conservar_dispositivo_id': rec.get('conservar_dispositivo_id', ''),
            }

    # Agrupar por dispositivo
    por_dispositivo: Dict[str, List[Dict[str, Any]]] = {}
    for cand in candidatos_map.values():
        disp_tag = f"{cand['dispositivo']} (ID {cand['dispositivo_id']})"
        por_dispositivo.setdefault(disp_tag, []).append(cand)

    # 1. JSON
    json_agrupado = []
    for disp_tag, lista in por_dispositivo.items():
        if not lista:
            continue
        disp_nombre = lista[0]['dispositivo']
        disp_id = lista[0]['dispositivo_id']
        json_agrupado.append({
            'dispositivo': disp_nombre,
            'dispositivo_id': disp_id,
            'total_candidatos': len(lista),
            'candidatos': lista,
        })

    datos_json = {
        'fecha': informe.get('fecha', datetime.now(ZONA_MEXICO).isoformat()),
        'total_candidatos_a_eliminar': len(candidatos_map),
        'dispositivos': json_agrupado,
    }

    ruta_json = carpeta_base / 'json' / f'candidatos_a_eliminar_{identificador}.json'
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(json.dumps(datos_json, ensure_ascii=False, indent=4) + '\n', encoding='utf-8')

    # 2. TXT
    lineas_txt = [
        '================================================================================',
        'CANDIDATOS A ELIMINAR — VÍDEOS DUPLICADOS DETECTADOS',
        '================================================================================',
        f'Fecha: {datetime.now(ZONA_MEXICO).strftime("%d/%m/%Y %H:%M:%S")}',
        f'Total vídeos candidatos a eliminar: {len(candidatos_map)}',
        '================================================================================',
        '',
    ]

    if not candidatos_map:
        lineas_txt.append('No se encontraron vídeos candidatos a eliminar.')
    else:
        for disp_tag, lista in por_dispositivo.items():
            lineas_txt.append('--------------------------------------------------------------------------------')
            lineas_txt.append(f'DISPOSITIVO: {disp_tag} — ({len(lista)} candidato{"s" if len(lista) != 1 else ""})')
            lineas_txt.append('--------------------------------------------------------------------------------')
            for idx, item in enumerate(lista, 1):
                lineas_txt.append(f'  {idx}. ELIMINAR   →  {item["nombre"]} (ID {item["dispositivo_id"]})')
                lineas_txt.append(f'     Ruta completa: {item["ruta_completa"]}')
                if item["motivo"]:
                    lineas_txt.append(f'     Motivo       : {item["motivo"]}')
                if item["conservar_nombre"]:
                    lineas_txt.append(f'     Conservar    : {item["conservar_nombre"]} (ID {item["conservar_dispositivo_id"]})')
                    if item["conservar_ruta"]:
                        lineas_txt.append(f'     Ruta conservar: {item["conservar_ruta"]}')
                lineas_txt.append('')

    ruta_txt = carpeta_base / 'txt' / f'candidatos_a_eliminar_{identificador}.txt'
    ruta_txt.parent.mkdir(parents=True, exist_ok=True)
    ruta_txt.write_text('\n'.join(lineas_txt) + '\n', encoding='utf-8')

    # 3. CSV
    ruta_csv = carpeta_base / 'csv' / f'candidatos_a_eliminar_{identificador}.csv'
    ruta_csv.parent.mkdir(parents=True, exist_ok=True)
    cols_csv = [
        'dispositivo', 'dispositivo_id', 'nombre', 'ruta_completa',
        'criterio', 'motivo', 'conservar_nombre', 'conservar_ruta',
        'conservar_dispositivo', 'conservar_dispositivo_id'
    ]
    with ruta_csv.open('w', newline='', encoding='utf-8-sig') as f_csv:
        escritor = csv.DictWriter(f_csv, fieldnames=cols_csv)
        escritor.writeheader()
        for cand in candidatos_map.values():
            escritor.writerow(cand)


def exportar_reporte_huellas_faltantes(
    informe: Dict[str, Any],
    carpeta_base: Path,
    identificador: str,
    base_nombre: str = '',
) -> Dict[str, Path]:
    """Exporta el reporte de vídeos faltantes de huellas en JSON, TXT y CSV."""
    for sub in ('json', 'txt', 'csv'):
        (carpeta_base / sub).mkdir(parents=True, exist_ok=True)

    ruta_json = carpeta_base / 'json' / f'videos_faltantes_huellas_{identificador}.json'
    ruta_txt = carpeta_base / 'txt' / f'videos_faltantes_huellas_{identificador}.txt'
    ruta_csv = carpeta_base / 'csv' / f'videos_faltantes_huellas_{identificador}.csv'

    # 1. JSON
    ruta_json.write_text(json.dumps(informe, ensure_ascii=False, indent=4) + '\n', encoding='utf-8')

    # 2. TXT
    lineas = [
        '================================================================================',
        'REPORTE DE VÍDEOS FALTANTES DE HUELLAS',
        '================================================================================',
        f'Fecha: {datetime.now(ZONA_MEXICO).strftime("%d/%m/%Y %H:%M:%S")}',
        f'Base de datos: {base_nombre or "SQLite / Neon"}',
        f'Total vídeos analizados        : {informe.get("total_videos_analizados", 0):,}',
        f'Vídeos con huellas completas  : {informe.get("total_videos_con_huellas_completas", 0):,}',
        f'Vídeos faltantes de huellas   : {informe.get("total_videos_faltantes_huellas", 0):,}',
        '================================================================================',
        'DESGLOSE DE HUELLAS FALTANTES:',
        f'  • Sin SHA256        : {informe.get("desglose_faltantes", {}).get("sin_sha256", 0):,}',
        f'  • Sin Huella visual : {informe.get("desglose_faltantes", {}).get("sin_huella_visual", 0):,}',
        f'  • Sin Huella audio  : {informe.get("desglose_faltantes", {}).get("sin_huella_audio", 0):,}',
        '================================================================================',
        '',
    ]

    dispositivos = informe.get('dispositivos', [])
    if not dispositivos:
        lineas.append('✓ Todos los vídeos registrados cuentan con las 3 huellas completas.')
    else:
        for disp in dispositivos:
            d_nom = disp.get('dispositivo', 'N/D')
            d_id = disp.get('dispositivo_id', 'N/D')
            vids = disp.get('videos', [])
            lineas.append('--------------------------------------------------------------------------------')
            lineas.append(f'DISPOSITIVO: {d_nom} (ID: {d_id}) — ({len(vids)} vídeo{"s" if len(vids) != 1 else ""} con huellas faltantes)')
            lineas.append('--------------------------------------------------------------------------------')
            for idx, v in enumerate(vids, 1):
                estado_partes = []
                if v['tiene_sha256']:
                    estado_partes.append('SHA256: sí')
                else:
                    estado_partes.append('SHA256: NO')

                if v['tiene_huella_visual']:
                    estado_partes.append('Visual: sí')
                else:
                    estado_partes.append('Visual: NO')

                if v['tiene_huella_audio']:
                    estado_partes.append('Audio: sí')
                else:
                    estado_partes.append('Audio: NO')

                lineas.append(f'  {idx}. {v["nombre"]}')
                lineas.append(f'     Ruta   : {v["ruta"]}')
                lineas.append(f'     Estado : {" | ".join(estado_partes)}')
                lineas.append(f'     FALTA  : {v["detalle_faltantes"]}')
                lineas.append(f'     Tamaño : {_fmt_bytes(v.get("tamanio_bytes", 0))} | Duración: {_fmt_seg(v.get("duracion", 0.0))}')
                lineas.append('')

    ruta_txt.write_text('\n'.join(lineas) + '\n', encoding='utf-8')

    # 3. CSV
    cols_csv = [
        'dispositivo', 'dispositivo_id', 'nombre', 'ruta',
        'tamanio_bytes', 'duracion_seg', 'tiene_sha256',
        'tiene_huella_visual', 'tiene_huella_audio', 'huellas_faltantes'
    ]
    with ruta_csv.open('w', newline='', encoding='utf-8-sig') as f_csv:
        escritor = csv.DictWriter(f_csv, fieldnames=cols_csv)
        escritor.writeheader()
        for disp in dispositivos:
            for v in disp.get('videos', []):
                escritor.writerow({
                    'dispositivo': disp.get('dispositivo'),
                    'dispositivo_id': disp.get('dispositivo_id'),
                    'nombre': v.get('nombre'),
                    'ruta': v.get('ruta'),
                    'tamanio_bytes': v.get('tamanio_bytes'),
                    'duracion_seg': v.get('duracion'),
                    'tiene_sha256': v.get('tiene_sha256'),
                    'tiene_huella_visual': v.get('tiene_huella_visual'),
                    'tiene_huella_audio': v.get('tiene_huella_audio'),
                    'huellas_faltantes': v.get('detalle_faltantes'),
                })

    return {'json': ruta_json, 'txt': ruta_txt, 'csv': ruta_csv}


# ============================================================================
# Resumen de escaneo
# ============================================================================
def imprimir_resumen_escaneo(
    procesados: int,
    errores: int,
    omitidos: int,
    tiempo_seg: float,
    interrumpido: bool = False,
) -> None:
    if interrumpido:
        titulo = "Escaneo interrumpido"
        estilo_titulo = "bold yellow"
        estilo_borde = "yellow"
    elif procesados == 0 and errores > 0:
        titulo = "Escaneo fallido (0 procesados)"
        estilo_titulo = "bold red"
        estilo_borde = "red"
    elif errores > 0:
        titulo = "Escaneo finalizado con errores"
        estilo_titulo = "bold orange3"
        estilo_borde = "orange3"
    elif procesados == 0 and omitidos > 0:
        titulo = "Escaneo omitido (archivos al día)"
        estilo_titulo = "bold deep_sky_blue1"
        estilo_borde = "deep_sky_blue1"
    elif procesados == 0:
        titulo = "Escaneo finalizado (sin procesar)"
        estilo_titulo = "bold yellow"
        estilo_borde = "yellow"
    else:
        titulo = "Escaneo completado"
        estilo_titulo = "bold plum1"
        estilo_borde = "bright_cyan"

    if CONSOLA is not None and Table is not None and rich_box is not None:
        tabla = Table(title=titulo, box=rich_box.ROUNDED,
                      border_style=estilo_borde, title_style=estilo_titulo, show_header=False)
        tabla.add_column('Campo', style='bold deep_sky_blue1')
        tabla.add_column('Valor', style='bold spring_green2')
        tabla.add_row('Procesados', f'[bold spring_green2]{procesados}[/]')
        if errores:
            tabla.add_row('Con error', f'[bold orange3]{errores}[/]')
        tabla.add_row('Tiempo', f'[bold gold1]{_fmt_seg(tiempo_seg)}[/]')
        CONSOLA.print(tabla)
    else:
        print()
        print(f'{C.GRIS}{"─" * 72}{C.RESET}')
        print(f'  {C.NEGRITA}{titulo}{C.RESET}')
        print(f'  Procesados : {C.VERDE}{procesados}{C.RESET}')
        if errores:
            print(f'  Con error  : {C.ROJO}{errores}{C.RESET}')
        print(f'  Tiempo     : {_fmt_seg(tiempo_seg)}')
        print(f'{C.GRIS}{"─" * 72}{C.RESET}')
        print()


# ============================================================================
# Progreso multipropósito para operaciones que informan (fase, completado, total)
# ============================================================================
class ProgresoMultilinea:
    """Progreso VIDUP adaptativo: tarea, archivo, barra y tiempos en líneas separadas.

    Es el componente estándar para operaciones por lote. En Termux mantiene
    visibles el detalle y los contadores sin concatenarlos horizontalmente;
    fuera de TTY informa hitos de 10 % sin códigos de color ni redibujos.
    """

    def __init__(self, total: int, titulo: str = 'Procesando vídeos'):
        self.total = max(0, int(total))
        self.titulo = str(titulo)
        self._inicio = time.monotonic()
        self._ultimo_porcentaje = -1
        self._actual = 0
        self._detalle = 'Preparando operación…'
        self._live = None
        self._interactivo = sys.stdout.isatty() and CONSOLA is not None

    def __enter__(self):
        if self._interactivo:
            from rich.live import Live
            self._live = Live(self._render(), console=CONSOLA,
                              refresh_per_second=4, transient=False)
            self._live.start()
        else:
            print(f'{self.titulo}: iniciando ({self.total:,} elementos)', flush=True)
        return self

    @staticmethod
    def _cortar(texto: str, ancho: int) -> str:
        texto = str(texto).replace('\n', ' ').replace('\r', ' ').strip()
        if len(texto) <= ancho:
            return texto
        return '…' + texto[-max(1, ancho - 1):]

    def _render(self):
        from rich.console import Group
        from rich.text import Text
        ancho_terminal = max(24, shutil.get_terminal_size((55, 24)).columns)
        ancho_barra = max(10, min(32, ancho_terminal - 9))
        total = max(1, self.total)
        fraccion = min(1., max(0., self._actual / total))
        verdes = min(ancho_barra, int(fraccion * ancho_barra))
        transcurrido = time.monotonic() - self._inicio
        eta = transcurrido * (1 - fraccion) / fraccion if fraccion else 0
        return Group(
            Text(self._cortar(self.titulo, ancho_terminal - 1), style='bold bright_cyan'),
            Text('Archivo: ' + self._cortar(self._detalle, max(12, ancho_terminal - 10)),
                 style='white'),
            Text.assemble(('█' * verdes, 'spring_green2'),
                          ('░' * (ancho_barra - verdes), 'grey42'),
                          (f' {fraccion:.0%}', 'bold yellow')),
            Text(f'Avance: {self._actual:,}/{self.total:,}', style='bright_green'),
            Text('Tiempo: ' + _fmt_seg(transcurrido) +
                 ' · ETA: ' + (_fmt_seg(eta) if fraccion else 'calculando'),
                 style='grey70'),
        )

    def actualizar(self, actual: int, total: Optional[int] = None, detalle: str = '') -> None:
        self._actual = max(0, int(actual))
        if total is not None:
            self.total = max(0, int(total))
        if detalle:
            self._detalle = str(detalle)
        if self._live:
            self._live.update(self._render(), refresh=True)
        else:
            pct = int(100 * min(self._actual, max(1, self.total)) / max(1, self.total))
            franja = pct // 10
            if franja != self._ultimo_porcentaje or self._actual == self.total:
                self._ultimo_porcentaje = franja
                print(f'  {self._actual:,}/{self.total:,} ({pct}%) · '
                      f'{self._cortar(self._detalle, 65)}', flush=True)

    def __exit__(self, exc_type, exc, tb):
        if self._live:
            self._live.stop()
        elif exc_type is None:
            print(f'{self.titulo}: finalizado', flush=True)
        else:
            print(f'{self.titulo}: cancelado o interrumpido', flush=True)
        return False


class ProgresoTareas:
    """Progreso de varias etapas, usando siempre el panel multilínea de VIDUP."""

    def __init__(self, tareas):
        self._tareas = tuple(tareas)
        self._descripcion = {clave: desc for clave, desc in self._tareas}
        self._barra = None

    def __enter__(self):
        self._barra = ProgresoMultilinea(1, 'VIDUP · Progreso por etapas')
        self._barra.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._barra is not None:
            self._barra.__exit__(exc_type, exc, tb)
        return False

    def actualizar(self, etapa, actual, total, detalle=''):
        if self._barra is None or etapa not in self._descripcion:
            return
        index = next((i for i, (k, _) in enumerate(self._tareas, 1)
                      if k == etapa), 1)
        self._barra.titulo = (f'VIDUP · Etapa {index}/{len(self._tareas)} · '
                             f'{self._descripcion[etapa]}')
        self._barra.actualizar(actual, total, detalle or self._descripcion[etapa])


# ============================================================================
# Barra de progreso (comparación de pares)
# ============================================================================
class BarraProgreso:
    """Panel de progreso multilínea, coloreado y redibujable en terminal.

    Sigue el patrón de los paneles de hevc.py y normalizar.py: conserva
    cuántas líneas dibujó, las limpia antes de imprimir mensajes y vuelve
    a dibujar el estado actual. En terminales no interactivos no emite
    secuencias ANSI ni barras que ensucien los registros.
    """

    def __init__(self, total: int, ancho: int = 32, total_videos: int = 0):
        self.total = max(1, int(total))
        self.total_videos = max(0, int(total_videos))
        self.videos_completos = 0
        self.ancho = max(10, int(ancho))
        self._stream = sys.stderr
        self._activo = bool(getattr(self._stream, 'isatty', lambda: False)())
        self._usar_color = self._activo and os.environ.get('NO_COLOR') is None
        self._inicio = time.monotonic()
        self._ultimo_momento = 0.0
        self._terminada = False
        self._panel_activo = False
        self._lineas_dibujadas = 0
        self._estado = {
            'actual': 0.0,
            'total': self.total,
            'nombre_a': '',
            'nombre_b': '',
        }
        self._lock = threading.RLock()
        # La misma consola sirve para mensajes estáticos en TTY y en logs;
        # el panel interactivo solo se activa cuando stderr es una terminal.
        self._console = (
            Console(file=self._stream, highlight=False)
            if TIENE_RICH and Console is not None
            else None
        )

    @staticmethod
    def _acortar(texto: str, limite: int) -> str:
        texto = str(texto or '').replace('\r', ' ').replace('\n', ' ').strip()
        if limite <= 0:
            return ''
        if len(texto) <= limite:
            return texto
        if limite <= 3:
            return texto[:limite]
        return texto[:limite - 3] + '...'

    def _colores_ansi(self) -> Dict[str, str]:
        if not self._usar_color:
            return {k: '' for k in ('reset', 'bold', 'red', 'yellow', 'green', 'cyan', 'grey', 'magenta')}
        return {
            'reset': '\033[0m', 'bold': '\033[1m',
            'red': '\033[91m', 'yellow': '\033[93m',
            'green': '\033[92m', 'cyan': '\033[96m',
            'grey': '\033[90m', 'magenta': '\033[95m',
        }

    def _construir_lineas(self) -> tuple[List[str], List[str]]:
        estado = self._estado
        total = max(1, int(estado['total']))
        actual = max(0.0, min(float(estado['actual']), float(total)))
        fraccion = max(0.0, min(1.0, actual / total))
        llenos = min(self.ancho, int(self.ancho * fraccion))
        vacios = self.ancho - llenos
        transcurrido = max(0.0, time.monotonic() - self._inicio)
        eta = transcurrido * (1.0 - fraccion) / fraccion if fraccion > 0 else 0.0
        eta_texto = _fmt_seg(eta) if fraccion > 0 else 'calculando'
        pares_txt = f'{min(int(actual), total):,}/{total:,}'
        videos_txt = f'{self.videos_completos:,}/{self.total_videos:,}'

        ancho_terminal = shutil.get_terminal_size((100, 24)).columns
        limite_nombres = max(12, min(76, ancho_terminal - 20))
        nombre_a = self._acortar(estado['nombre_a'], limite_nombres // 2)
        nombre_b = self._acortar(estado['nombre_b'], limite_nombres // 2)
        pareja = f'{nombre_a}  ↔  {nombre_b}' if nombre_a or nombre_b else 'Preparando comparación de pares'
        pareja = self._acortar(pareja, max(12, ancho_terminal - 12))

        rich = [
            '[bold dodger_blue2]COMPARACIÓN DE VÍDEOS[/] '
            f'[bright_cyan]• Vídeos completos: {videos_txt}[/]',
            f'[spring_green2]{"█" * llenos}[/]'
            f'[grey50]{"░" * vacios}[/] '
            f'[bold gold1]{fraccion:5.1%}[/]  '
            f'[white]Pares: {pares_txt}[/]  '
            f'[turquoise2]ETA: {eta_texto}[/]',
            f'[cyan]Par actual:[/] [white]{rich_escape(pareja)}[/]',
        ]

        c = self._colores_ansi()
        ansi = [
            f"{c['bold']}{c['cyan']}COMPARACIÓN DE VÍDEOS{c['reset']} "
            f"{c['cyan']}• Vídeos completos: {videos_txt}{c['reset']}",
            f"{c['green']}{'█' * llenos}{c['grey']}{'░' * vacios}{c['reset']} "
            f"{c['bold']}{c['yellow']}{fraccion:5.1%}{c['reset']}  "
            f"Pares: {pares_txt}  {c['magenta']}ETA: {eta_texto}{c['reset']}",
            f"{c['cyan']}Par actual:{c['reset']} {pareja}",
        ]
        return rich, ansi

    def _escribir_linea(self, rich: str, ansi: str) -> None:
        if self._console is not None:
            self._console.print(rich, end='', no_wrap=True, crop=True)
        else:
            self._stream.write(ansi)

    def _dibujar_locked(self) -> None:
        if not self._activo or self._terminada:
            return

        rich, ansi = self._construir_lineas()
        anteriores = self._lineas_dibujadas
        if self._panel_activo and anteriores:
            if anteriores > 1:
                self._stream.write(f'\033[{anteriores - 1}A\r')
            else:
                self._stream.write('\r')
        else:
            self._stream.write('\r')

        total_lineas = len(rich)
        for indice, (linea_rich, linea_ansi) in enumerate(zip(rich, ansi)):
            self._stream.write('\033[2K')
            self._escribir_linea(linea_rich, linea_ansi)
            if indice < total_lineas - 1:
                self._stream.write('\n')

        # Si el nuevo panel tiene menos líneas, borrar las sobrantes y
        # regresar el cursor a la última línea visible del panel.
        if anteriores > total_lineas:
            diferencia = anteriores - total_lineas
            for _ in range(diferencia):
                self._stream.write('\n\033[2K')
            self._stream.write(f'\033[{diferencia}A')

        self._stream.flush()
        self._panel_activo = True
        self._lineas_dibujadas = total_lineas

    def _limpiar_locked(self) -> None:
        if self._activo and self._panel_activo and self._lineas_dibujadas > 0:
            for _ in range(self._lineas_dibujadas - 1):
                self._stream.write('\r\033[2K\033[1A')
            self._stream.write('\r\033[2K')
            self._stream.flush()
        self._panel_activo = False
        self._lineas_dibujadas = 0

    def actualizar_videos(self, actual: int, total: int) -> None:
        with self._lock:
            self.videos_completos = max(0, int(actual))
            self.total_videos = max(0, int(total))
            # El siguiente callback de pares refresca el panel con estos datos;
            # no redibujar aquí evita una escritura por cada par procesado.

    def actualizar(
        self,
        actual: float,
        total: int,
        nombre_a: str = '',
        nombre_b: str = '',
    ) -> None:
        with self._lock:
            total_real = max(1, int(total))
            self._estado.update(
                actual=max(0.0, float(actual)),
                total=total_real,
                nombre_a=nombre_a,
                nombre_b=nombre_b,
            )
            if not self._activo or self._terminada:
                return

            ahora = time.monotonic()
            fraccion = min(1.0, max(0.0, float(actual) / total_real))
            if fraccion < 1.0 and ahora - self._ultimo_momento < 0.10:
                return
            self._ultimo_momento = ahora
            self._dibujar_locked()

    def mostrar_mensaje(
        self,
        lineas_rich: List[str],
        lineas_ansi: List[str],
    ) -> None:
        """Imprime un hallazgo fuera del panel y restaura la barra debajo."""
        with self._lock:
            self._limpiar_locked()
            for indice, linea in enumerate(lineas_rich):
                fallback = lineas_ansi[indice] if indice < len(lineas_ansi) else linea
                if self._console is not None:
                    # Los datos estáticos (especialmente rutas) pueden ocupar
                    # varias columnas/líneas; no se recortan como la barra.
                    self._console.print(linea)
                else:
                    self._stream.write(fallback + '\n')
            self._stream.flush()
            self._dibujar_locked()

    def limpiar(self) -> None:
        """Elimina todas las líneas del panel; se puede llamar más de una vez."""
        with self._lock:
            self._limpiar_locked()
            self._terminada = True


def _fmt_eta_corto(segundos: float) -> str:
    try:
        s = int(max(0, segundos))
    except Exception:
        return "calculando"
    if s < 3600:
        return f"{s//60:d}m{s%60:02d}s"
    if s < 86400:
        h, rem = divmod(s, 3600)
        return f"{h:d}h{rem//60:02d}m"
    d, rem = divmod(s, 86400)
    return f"{d:d}d{rem//3600:02d}h"


class Dashboard:
    def __init__(self, num_workers: int, tty: bool = True):
        self._lock = threading.Lock()
        self._num_workers = max(1, num_workers)
        self._tty = tty
        self._workers: Dict[int, Any] = {}
        self._thread_slots: Dict[int, int] = {}
        self._completados = 0
        self._errores = 0
        self._total = 0
        self._inicio_batch = 0.0
        self._lineas_dibujadas = 0

    def iniciar(self, total: int) -> None:
        with self._lock:
            self._total = total
            self._completados = 0
            self._errores = 0
            self._inicio_batch = time.monotonic()
            self._workers = {i: None for i in range(self._num_workers)}
            self._thread_slots = {}
            self._lineas_dibujadas = 0
            lineas_necesarias = 1 + (self._num_workers * 3)
            if self._tty:
                sys.stdout.write('\n' * lineas_necesarias)
                sys.stdout.flush()
                self._lineas_dibujadas = lineas_necesarias
            self._dibujar()

    def detener(self) -> None:
        with self._lock:
            if self._tty and self._lineas_dibujadas > 0:
                sys.stdout.write(f'\033[{self._lineas_dibujadas}F')
                sys.stdout.write('\033[J')
                sys.stdout.flush()
            self._workers = {}
            self._thread_slots = {}
            self._lineas_dibujadas = 0

    def _get_slot(self, tid: int) -> int:
        if tid not in self._thread_slots:
            slot = len(self._thread_slots)
            if slot < self._num_workers:
                self._thread_slots[tid] = slot
            else:
                return 0
        return self._thread_slots[tid]

    def actualizar_worker_datos(self, tid: int, datos: dict) -> None:
        with self._lock:
            if not self._workers:
                return
            slot = self._get_slot(tid)
            self._workers[slot] = datos
            self._dibujar()

    def actualizar_worker(self, tid: int, texto: str) -> None:
        with self._lock:
            if not self._workers:
                return
            slot = self._get_slot(tid)
            self._workers[slot] = {'texto_simple': texto}
            self._dibujar()

    def marcar_completado(self) -> None:
        with self._lock:
            self._completados += 1
            self._dibujar()

    def marcar_error(self) -> None:
        with self._lock:
            self._errores += 1
            self._dibujar()

    def imprimir_mensaje(self, mensaje: str) -> None:
        with self._lock:
            if self._tty and self._lineas_dibujadas > 0:
                sys.stdout.write(f'\033[{self._lineas_dibujadas}F')
                sys.stdout.write('\033[J')
            print(mensaje)
            if self._workers:
                lineas_necesarias = 1 + (self._num_workers * 3)
                if self._tty:
                    sys.stdout.write('\n' * lineas_necesarias)
                    self._lineas_dibujadas = lineas_necesarias
                self._dibujar()

    def _dibujar(self) -> None:
        if not self._tty or self._lineas_dibujadas == 0:
            return

        linea_completados = f"\033[2K\033[36;1m▶ Completados: \033[1;37m{self._completados}\033[0m\033[36m / {self._total}\033[0m"
        if self._errores > 0:
            linea_completados += f" \033[36m| Errores: \033[31;1m{self._errores}\033[0m"

        lineas = [linea_completados]

        for i in range(self._num_workers):
            d = self._workers.get(i)
            if isinstance(d, dict) and 'nombre' in d:
                w_nombre = d['nombre']
                fase = d.get('fase', 'Procesando')
                fraccion = max(0.0, min(1.0, d.get('fraccion', 0.0)))
                tiempo_actual = d.get('tiempo_actual', 0.0)
                dur_total = d.get('dur_total', 0.0)
                speed = d.get('speed', 0.0)
                eta_str = d.get('eta_texto', '--:--')

                llenos_w = int(10 * fraccion)
                barra_w = f"[\033[92m{'█' * llenos_w}\033[90m{'░' * (10 - llenos_w)}\033[0m]"

                l1 = f"\033[2K\033[36;1mT{i + 1}:\033[0m \033[37;1m{w_nombre}\033[0m"
                l2 = f"\033[2K{fase} {barra_w} \033[93m{fraccion * 100:4.1f} %\033[0m"
                l3 = f"\033[2K{_fmt_hms_ms(tiempo_actual)}/{_fmt_hms_ms(dur_total)} | \033[35m{speed:5.2f}x\033[0m | \033[36mETA: {eta_str}\033[0m"
                lineas.extend([l1, l2, l3])
            elif isinstance(d, dict) and 'texto_simple' in d:
                l1 = f"\033[2K\033[36;1mT{i + 1}:\033[0m {d['texto_simple']}"
                l2 = f"\033[2K[\033[90m░░░░░░░░░░\033[0m] \033[90m0.0 %\033[0m"
                l3 = f"\033[2K\033[90m00:00:00.00/00:00:00.00 | 0.00x | ETA: --:--\033[0m"
                lineas.extend([l1, l2, l3])
            else:
                l1 = f"\033[2K\033[36;1mT{i + 1}:\033[0m \033[90mEsperando asignación de vídeo\033[0m"
                l2 = f"\033[2K[\033[90m░░░░░░░░░░\033[0m] \033[90m0.0 %\033[0m"
                l3 = f"\033[2K\033[90m00:00:00.00/00:00:00.00 | 0.00x | ETA: --:--\033[0m"
                lineas.extend([l1, l2, l3])

        sys.stdout.write(f'\033[{self._lineas_dibujadas}A\033[0J')
        sys.stdout.write('\n'.join(lineas) + '\n')
        sys.stdout.flush()
        self._lineas_dibujadas = len(lineas)


def _fmt_hms_ms(segundos: float) -> str:
    """Formatea segundos a hh:mm:ss.ms siempre con 2 dígitos de horas."""
    try:
        s = max(0.0, float(segundos or 0))
    except Exception:
        return "00:00:00.00"
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    sec_int = int(sec)
    ms = int((sec - sec_int) * 100)
    return f"{int(h):02d}:{int(m):02d}:{sec_int:02d}.{ms:02d}"


class ProgresoFingerprintWorker:
    def __init__(self, dashboard: Dashboard, worker_id: int, nombre_video: str, duracion: float = 0.0):
        self._dashboard = dashboard
        self._worker_id = worker_id
        self._nombre = nombre_video
        self.duracion = duracion
        self._inicio_trabajo = time.monotonic()
        self._ultimo_momento = 0.0

    def __call__(self, fase: str, fraccion: float, eta: float, duracion: float = 0.0) -> None:
        fraccion = max(0.0, min(1.0, fraccion))
        ahora = time.monotonic()
        if ahora - self._ultimo_momento < 0.15 and fraccion < 1.0:
            return
        self._ultimo_momento = ahora
        eta_texto = _fmt_eta_corto(eta) if eta >= 0 else 'calculando'

        dur_total = duracion or self.duracion
        tiempo_actual = fraccion * dur_total if dur_total > 0 else 0.0
        transcurrido = max(0.001, ahora - self._inicio_trabajo)
        speed = (tiempo_actual / transcurrido) if (transcurrido > 0 and tiempo_actual > 0) else 0.0

        datos = {
            'nombre': self._nombre,
            'fase': fase,
            'fraccion': fraccion,
            'tiempo_actual': tiempo_actual,
            'dur_total': dur_total,
            'speed': speed,
            'eta_texto': eta_texto,
        }
        self._dashboard.actualizar_worker_datos(self._worker_id, datos)

    def cerrar(self) -> None:
        pass


def _fmt_tamanio_estadisticas(numero: int) -> str:
    """Tamaño en unidades binarias, utilizado solamente por el menú 7."""
    numero = max(0, int(numero or 0))
    if numero >= 1024 ** 4:
        return f'{numero / (1024 ** 4):,.2f} TB'
    if numero >= 1024 ** 3:
        return f'{numero / (1024 ** 3):,.2f} GB'
    if numero >= 1024 ** 2:
        return f'{numero / (1024 ** 2):,.1f} MB'
    if numero >= 1024:
        return f'{numero / 1024:,.1f} KB'
    return f'{numero} B'


def _mostrar_desglose_estadisticas(dispositivos: List[Dict[str, Any]]) -> None:
    """Muestra dispositivos y carpetas (incluyendo SHA256, Visual y Audio), tanto con Rich como sin él."""
    if not dispositivos:
        imprimir('[yellow]No hay vídeos registrados para desglosar.[/]')
        return

    # En Termux las tablas de ocho columnas parten nombres, rutas y valores.
    # Usar fichas verticales Rich con color semántico para pantallas angostas.
    from core.interfaz import ancho_terminal
    if CONSOLA is not None and Panel is not None and ancho_terminal() < 100:
        from rich.text import Text
        from rich.console import Group
        for disp in dispositivos:
            cabecera = Text()
            cabecera.append(str(disp['dispositivo']), style='bold plum1')
            cabecera.append('\nID: ' + str(disp['dispositivo_id']), style='deep_sky_blue1')
            if not disp['id_verificado']:
                cabecera.append(' · ID no verificado', style='bold orange1')
            cabecera.append(f"\nVídeos: {disp['total_videos']:,}", style='bold bright_white')
            cabecera.append(f" · Tamaño: {_fmt_tamanio_estadisticas(disp['tamanio_bytes'])}", style='turquoise2')
            cabecera.append(f"\nSHA256: {disp['con_sha256']:,}", style='spring_green2')
            cabecera.append(f" · Visual: {disp['con_huella_visual']:,}", style='gold1')
            cabecera.append(f" · Audio: {disp.get('con_huella_audio', 0):,}", style='medium_orchid1')
            cabecera.append(f"\nErrores: {disp['con_error']:,}", style='indian_red1')
            CONSOLA.print(Panel(cabecera, title='DISPOSITIVO', border_style='medium_purple',
                                box=rich_box.ROUNDED))
            for carpeta in disp['carpetas']:
                texto = Text()
                texto.append(str(carpeta['ruta']) + '\n', style='bright_white')
                texto.append(f"{carpeta['total_videos']:,} vídeos", style='bold spring_green2')
                texto.append(f" · {_fmt_tamanio_estadisticas(carpeta['tamanio_bytes'])}\n", style='turquoise2')
                texto.append(f"SHA256 {carpeta['con_sha256']:,}", style='spring_green2')
                texto.append(f" · Visual {carpeta['con_huella_visual']:,}", style='gold1')
                texto.append(f" · Audio {carpeta.get('con_huella_audio', 0):,}", style='medium_orchid1')
                CONSOLA.print(Panel(texto, title='CARPETA', border_style='dark_turquoise',
                                    title_align='left', box=rich_box.SIMPLE,
                                    padding=(0, 1)))
        return

    if CONSOLA is not None and Table is not None and rich_box is not None:
        resumen = Table(title='Vídeos registrados por dispositivo',
                        box=rich_box.ROUNDED, border_style='bright_magenta')
        resumen.add_column('Dispositivo', style='plum1', overflow='fold')
        resumen.add_column('ID', style='deep_sky_blue1', overflow='fold')
        resumen.add_column('Vídeos', justify='right', style='bright_white')
        resumen.add_column('Tamaño', justify='right', style='cyan')
        resumen.add_column('SHA256', justify='right', style='green')
        resumen.add_column('Visual', justify='right', style='gold1')
        resumen.add_column('Audio', justify='right', style='magenta')
        resumen.add_column('Errores', justify='right', style='red')
        for disp in dispositivos:
            resumen.add_row(
                rich_escape(disp['dispositivo']),
                rich_escape(disp['dispositivo_id']) if disp['id_verificado']
                    else '[yellow]SIN ID[/]',
                f'{disp["total_videos"]:,}',
                _fmt_tamanio_estadisticas(disp['tamanio_bytes']),
                f'{disp["con_sha256"]:,}',
                f'{disp["con_huella_visual"]:,}',
                f'{disp.get("con_huella_audio", 0):,}',
                f'{disp["con_error"]:,}',
            )
        CONSOLA.print(resumen)

        for disp in dispositivos:
            nombre = rich_escape(disp['dispositivo'])
            identificador = (rich_escape(disp['dispositivo_id']) if disp['id_verificado']
                             else '[yellow]SIN ID; identidad no verificable[/]')
            CONSOLA.print()
            CONSOLA.print(f'[bold bright_cyan]{nombre}[/]  |  ID: {identificador}')
            CONSOLA.print(
                f'  [bright_white]{disp["total_videos"]:,} vídeos[/]  ·  '
                f'[cyan]{_fmt_tamanio_estadisticas(disp["tamanio_bytes"])}[/]  ·  '
                f'[green]SHA256: {disp["con_sha256"]:,}[/]  ·  '
                f'[gold1]Visual: {disp["con_huella_visual"]:,}[/]  ·  '
                f'[magenta]Audio: {disp.get("con_huella_audio", 0):,}[/]  ·  '
                f'[red]Errores: {disp["con_error"]:,}[/]'
            )
            if len(disp['nombres_registrados']) > 1:
                otros = ', '.join(rich_escape(n) for n in disp['nombres_registrados'])
                CONSOLA.print(f'  [yellow]Nombres asociados a este ID:[/] {otros}')
            tabla_rutas = Table(title='Desglose por carpeta (sin incluir subcarpetas)',
                                box=rich_box.SIMPLE, border_style='cyan',
                                show_lines=False)
            tabla_rutas.add_column('Carpeta registrada', style='bright_white', overflow='fold')
            tabla_rutas.add_column('Vídeos', justify='right', style='green')
            tabla_rutas.add_column('Tamaño', justify='right', style='cyan')
            tabla_rutas.add_column('SHA256', justify='right', style='green')
            tabla_rutas.add_column('Visual', justify='right', style='gold1')
            tabla_rutas.add_column('Audio', justify='right', style='magenta')
            for carpeta in disp['carpetas']:
                tabla_rutas.add_row(
                    rich_escape(carpeta['ruta']),
                    f'{carpeta["total_videos"]:,}',
                    _fmt_tamanio_estadisticas(carpeta['tamanio_bytes']),
                    f'{carpeta["con_sha256"]:,}',
                    f'{carpeta["con_huella_visual"]:,}',
                    f'{carpeta.get("con_huella_audio", 0):,}',
                )
            CONSOLA.print(tabla_rutas)
    else:
        print('\n VÍDEOS REGISTRADOS POR DISPOSITIVO')
        print(' ' + '=' * 76)
        for disp in dispositivos:
            id_disp = disp['dispositivo_id'] if disp['id_verificado'] else 'SIN ID (no verificable)'
            print(f'\n Dispositivo : {disp["dispositivo"]} (ID {id_disp})')
            print(f' Vídeos      : {disp["total_videos"]:,}')
            print(f' Tamaño      : {_fmt_tamanio_estadisticas(disp["tamanio_bytes"])}')
            print(f' SHA256      : {disp["con_sha256"]:,}')
            print(f' Visual      : {disp["con_huella_visual"]:,}')
            print(f' Audio       : {disp.get("con_huella_audio", 0):,}')
            print(f' Con error   : {disp["con_error"]:,}')
            if len(disp['nombres_registrados']) > 1:
                print(f' Nombres de este ID: {", ".join(disp["nombres_registrados"])}')
            print('  Carpetas registradas (solo vídeos directamente contenidos):')
            for carpeta in disp['carpetas']:
                print(f'    {carpeta["ruta"]}  ->  {carpeta["total_videos"]:,} vídeos; '
                      f'{_fmt_tamanio_estadisticas(carpeta["tamanio_bytes"])}; '
                      f'SHA256 {carpeta["con_sha256"]:,}; visual {carpeta["con_huella_visual"]:,}; '
                      f'audio {carpeta.get("con_huella_audio", 0):,}')
        print()


# ============================================================================
# Barra de progreso (sincronización con la nube)
# ============================================================================
class BarraSincronizacion:
    """Panel de progreso para la sincronización entre la base local y la nube.

    Subir y bajar son acciones distintas, por eso cada dirección tiene su
    propia instancia con su propio total, su propio contador y su propia
    velocidad en MB/s. El diseño (panel multilínea redibujable, bloques
    █/░, colores y ETA) es el mismo que el de BarraProgreso para que no
    desentone con el resto de la herramienta.

    En terminales no interactivos no se emiten secuencias ANSI: se escribe
    una línea de resumen cada 10 % para no ensuciar los registros.
    """

    ETIQUETAS = {
        'subida': 'SUBIDA A LA NUBE',
        'bajada': 'BAJADA DESDE LA NUBE',
    }

    def __init__(self, direccion: str = 'subida', ancho: int = 32):
        self.direccion = direccion
        self.ancho = max(10, int(ancho))
        self._stream = sys.stderr
        self._activo = bool(getattr(self._stream, 'isatty', lambda: False)())
        self._usar_color = self._activo and os.environ.get('NO_COLOR') is None
        self._inicio = time.monotonic()
        self._ultimo_momento = 0.0
        self._terminada = False
        self._panel_activo = False
        self._lineas_dibujadas = 0
        self._lock = threading.RLock()
        self._estado = {
            'actual': 0.0,
            'total': 1,
            'bytes': 0,
            'fase': '',
        }
        self._ultimo_decimil_log = -1
        self._console = (
            Console(file=self._stream, highlight=False)
            if TIENE_RICH and Console is not None
            else None
        )

    @staticmethod
    def _acortar(texto: str, limite: int) -> str:
        texto = str(texto or '').replace('\r', ' ').replace('\n', ' ').strip()
        if limite <= 0:
            return ''
        if len(texto) <= limite:
            return texto
        if limite <= 3:
            return texto[:limite]
        return texto[:limite - 3] + '...'

    def _colores_ansi(self) -> Dict[str, str]:
        if not self._usar_color:
            return {k: '' for k in ('reset', 'bold', 'red', 'yellow', 'green', 'cyan', 'grey', 'magenta')}
        return {
            'reset': '\033[0m', 'bold': '\033[1m',
            'red': '\033[91m', 'yellow': '\033[93m',
            'green': '\033[92m', 'cyan': '\033[96m',
            'grey': '\033[90m', 'magenta': '\033[95m',
        }

    def _velocidad_mb_s(self, transcurrido: float) -> float:
        bytes_movidos = max(0, int(self._estado.get('bytes') or 0))
        if transcurrido <= 0:
            return 0.0
        # Se usa una ventana mínima de medio segundo: si no, el primer
        # dibujado reportaría velocidades absurdas (miles de MB/s) porque
        # apenas han pasado unos milisegundos desde el arranque.
        ventana = max(transcurrido, 0.5)
        return (bytes_movidos / ventana) / (1024 * 1024)

    def _construir_lineas(self) -> tuple[List[str], List[str]]:
        estado = self._estado
        total = max(1, int(estado['total']))
        actual = max(0.0, min(float(estado['actual']), float(total)))
        fraccion = max(0.0, min(1.0, actual / total))
        llenos = min(self.ancho, int(self.ancho * fraccion))
        vacios = self.ancho - llenos
        transcurrido = max(1e-6, time.monotonic() - self._inicio)
        eta = transcurrido * (1.0 - fraccion) / fraccion if fraccion > 0 else 0.0
        eta_texto = _fmt_seg(eta) if fraccion > 0 else 'calculando'
        mb_s = self._velocidad_mb_s(transcurrido)
        bytes_txt = _fmt_bytes(max(0, int(estado.get('bytes') or 0)))
        par_texto = f'{min(int(actual), total):,}/{total:,}'
        titulo = self.ETIQUETAS.get(self.direccion, self.direccion.upper())
        fase = self._acortar(estado.get('fase') or 'Preparando…', 70)

        rich = [
            f'[bold dodger_blue2]SINCRONIZACIÓN · {titulo}[/] '
            f'[bright_cyan]• Operaciones: {par_texto}[/]',
            f'[spring_green2]{"█" * llenos}[/]'
            f'[grey50]{"░" * vacios}[/] '
            f'[bold gold1]{fraccion:5.1%}[/]  '
            f'[white]Datos: {bytes_txt}[/]  '
            f'[turquoise2]{mb_s:6.2f} MB/s[/]  '
            f'[magenta]ETA: {eta_texto}[/]',
            f'[cyan]Fase:[/] [white]{rich_escape(fase)}[/]',
        ]

        c = self._colores_ansi()
        ansi = [
            f"{c['bold']}{c['cyan']}SINCRONIZACIÓN · {titulo}{c['reset']} "
            f"{c['cyan']}• Operaciones: {par_texto}{c['reset']}",
            f"{c['green']}{'█' * llenos}{c['grey']}{'░' * vacios}{c['reset']} "
            f"{c['bold']}{c['yellow']}{fraccion:5.1%}{c['reset']}  "
            f"Datos: {bytes_txt}  {c['cyan']}{mb_s:6.2f} MB/s{c['reset']}  "
            f"{c['magenta']}ETA: {eta_texto}{c['reset']}",
            f"{c['cyan']}Fase:{c['reset']} {fase}",
        ]
        return rich, ansi

    def _escribir_linea(self, rich: str, ansi: str) -> None:
        if self._console is not None:
            self._console.print(rich, end='', no_wrap=True, crop=True)
        else:
            self._stream.write(ansi)

    def _dibujar_locked(self) -> None:
        if not self._activo or self._terminada:
            return

        rich, ansi = self._construir_lineas()
        anteriores = self._lineas_dibujadas
        if self._panel_activo and anteriores:
            if anteriores > 1:
                self._stream.write(f'\033[{anteriores - 1}A\r')
            else:
                self._stream.write('\r')
        else:
            self._stream.write('\r')

        total_lineas = len(rich)
        for indice, (linea_rich, linea_ansi) in enumerate(zip(rich, ansi)):
            self._stream.write('\033[2K')
            self._escribir_linea(linea_rich, linea_ansi)
            if indice < total_lineas - 1:
                self._stream.write('\n')

        if anteriores > total_lineas:
            diferencia = anteriores - total_lineas
            for _ in range(diferencia):
                self._stream.write('\n\033[2K')
            self._stream.write(f'\033[{diferencia}A')

        self._stream.flush()
        self._panel_activo = True
        self._lineas_dibujadas = total_lineas

    def _limpiar_locked(self) -> None:
        if self._activo and self._panel_activo and self._lineas_dibujadas > 0:
            for _ in range(self._lineas_dibujadas - 1):
                self._stream.write('\r\033[2K\033[1A')
            self._stream.write('\r\033[2K')
            self._stream.flush()
        self._panel_activo = False
        self._lineas_dibujadas = 0

    def actualizar(
        self,
        actual: float,
        total: int,
        bytes_movidos: int = 0,
        fase: str = '',
    ) -> None:
        """Actualiza el panel. `bytes_movidos` son los bytes acumulados."""
        with self._lock:
            total_real = max(1, int(total))
            self._estado.update(
                actual=max(0.0, float(actual)),
                total=total_real,
                bytes=max(0, int(bytes_movidos or 0)),
                fase=fase or self._estado.get('fase', ''),
            )
            if self._terminada:
                return

            ahora = time.monotonic()
            fraccion = min(1.0, max(0.0, float(actual) / total_real))

            if not self._activo:
                # Registro plano: una línea por cada 10 % completado.
                decimil = int(fraccion * 10)
                if decimil > self._ultimo_decimil_log or fraccion >= 1.0:
                    self._ultimo_decimil_log = decimil
                    titulo = self.ETIQUETAS.get(self.direccion, self.direccion.upper())
                    transcurrido = max(1e-6, ahora - self._inicio)
                    mb_s = self._velocidad_mb_s(transcurrido)
                    print(
                        f'  {titulo}: {int(actual):,}/{total_real:,} ({fraccion * 100:.1f} %)'
                        f' | {mb_s:.2f} MB/s'
                        f' | {self._estado.get("fase") or ""}',
                        flush=True,
                    )
                return

            if fraccion < 1.0 and ahora - self._ultimo_momento < 0.10:
                return
            self._ultimo_momento = ahora
            self._dibujar_locked()

    def mostrar_mensaje(self, lineas_rich: List[str], lineas_ansi: List[str]) -> None:
        """Imprime un aviso fuera del panel y restaura la barra debajo."""
        with self._lock:
            self._limpiar_locked()
            for indice, linea in enumerate(lineas_rich):
                fallback = lineas_ansi[indice] if indice < len(lineas_ansi) else linea
                if self._console is not None:
                    self._console.print(linea)
                else:
                    self._stream.write(fallback + '\n')
            self._stream.flush()
            self._dibujar_locked()

    def cerrar(self) -> None:
        """Elimina el panel de la pantalla dejando el resumen en el log."""
        with self._lock:
            self._terminada = True
            self._limpiar_locked()
