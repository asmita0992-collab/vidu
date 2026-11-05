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
from typing import Any, Dict, List, Optional

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

CONSOLA: Optional["Console"] = (
    Console(highlight=False, soft_wrap=True) if TIENE_RICH else None
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
        f'    [grey]Duración        :[/] {_fmt_seg(info.get("duracion", 0.0)) if info.get("duracion", 0.0) else 'N/D'}',
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
        f'    {C.GRIS}Duración        : {_fmt_seg(info.get("duracion", 0.0)) if info.get("duracion", 0.0) else 'N/D'}{C.RESET}',
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
        f'[cyan]Fecha:[/] {datetime.now(ZONA_MEXICO).strftime("%d/%m/%Y %H:%M:%S")}\n'
        f'[cyan]Dispositivo:[/] {info_disp["dispositivo"]} '
        f'[bright_black](ID {info_disp["dispositivo_id"]})[/]\n'
        f'[cyan]Plataforma:[/] {info_disp["plataforma"]} ({info_disp["maquina"]})'
    )
    if total_videos:
        CONSOLA.print(f'[cyan]Vídeos analizados:[/] {total_videos}')
    if tiempo_seg:
        CONSOLA.print(f'[cyan]Tiempo de análisis:[/] {_fmt_seg(tiempo_seg)}')
    CONSOLA.print(f'[cyan]Hallazgos totales:[/] [bold]{len(resultados)}[/]')
    CONSOLA.print(Rule(style='bright_cyan'))

    if not resultados:
        CONSOLA.print('\n[green]No se encontraron duplicados ni fragmentos.[/]\n')
        return

    # Resumen por tipo
    tabla = Table(title='Resumen', box=rich_box.ROUNDED, border_style='magenta')
    tabla.add_column('Tipo', style='bold')
    tabla.add_column('Cantidad', justify='right')
    for tipo in orden_tipos:
        if not por_tipo[tipo]:
            continue
        label, color = _TIPO_META.get(tipo, (tipo.upper(), 'grey'))
        tabla.add_row(f'[{color}]{label}[/]', str(len(por_tipo[tipo])))
        if tipo == 'duplicado_exacto':
            subgrupos_count = {}
            for r in por_tipo[tipo]:
                d1 = str(r.info_a.get('dispositivo') or 'N/D')
                d2 = str(r.info_b.get('dispositivo') or 'N/D')
                sg = " y ".join(sorted([d1, d2]))
                subgrupos_count[sg] = subgrupos_count.get(sg, 0) + 1
            for sg, cant in subgrupos_count.items():
                tabla.add_row(f'    [grey70]Duplicados de {rich_escape(sg)}[/]', str(cant))
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
                    CONSOLA.print(f'[grey58]----------DUPLICADOS DE {rich_escape(subgrupo)} ({count_sub})---------------------[/]')
                    ultimo_subgrupo = subgrupo
                    
            CONSOLA.print(_bloque_rich(r, indice))
            indice += 1

    CONSOLA.print(Rule(style='bright_cyan'))

#    indice = 1
#    for tipo in orden_tipos:
#        grupo = por_tipo[tipo]
#        if not grupo:
#            continue
#        label, color = _TIPO_META.get(tipo, (tipo.upper(), 'grey'))
#        CONSOLA.print(Rule(
#            f'[{color}][bold]{label} ({len(grupo)})[/][/]',
#            style=color,
#        ))
#        for r in grupo:
#            CONSOLA.print(_bloque_rich(r, indice))
#            indice += 1
#
#    CONSOLA.print(Rule(style='bright_cyan'))


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
    
#    indice = 1
#    for tipo in orden_tipos:
#        grupo = por_tipo[tipo]
#        if not grupo:
#            continue
#        label, color = _TIPO_META.get(tipo, (tipo.upper(), 'grey'))
#        mapa = {
#            'red': C.ROJO, 'yellow': C.AMARILLO, 'cyan': C.CIAN,
#            'magenta': C.MAGENTA, 'grey': C.GRIS,
#        }
#        c = mapa.get(color, C.GRIS)
#        print(f'\n{c}{C.NEGRITA}  {"─" * 10} {label} ({len(grupo)}) {"─" * 10}{C.RESET}')
#        for r in grupo:
#            print(_bloque_ansi(r, indice))
#            indice += 1
#
#    print('\n' + sep + '\n')


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

    # Capturamos la salida ANSI para el texto plano (sin colores)
    salida = io.StringIO()
    with redirect_stdout(salida):
        _imprimir_reporte_ansi(ordenados, total_videos, tiempo_seg)
    texto_terminal = _sin_color(salida.getvalue())

    meta = dict(metadatos or {})
    meta.setdefault('dispositivo', info_dispositivo_actual())

    # Añadimos el resumen de comparativas y metadatos al texto plano
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

    if CONSOLA is not None:
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

    # Fila resumen con el texto completo de terminal (incluso sin hallazgos)
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


# ============================================================================
# Resumen de escaneo
# ============================================================================
def imprimir_resumen_escaneo(
    procesados: int,
    errores: int,
    omitidos: int,
    tiempo_seg: float,
) -> None:
    if CONSOLA is not None:
        tabla = Table(title='Escaneo completado', box=rich_box.ROUNDED,
                      border_style='green', show_header=False)
        tabla.add_column('Campo', style='cyan')
        tabla.add_column('Valor', style='white')
        tabla.add_row('Procesados', f'[green]{procesados}[/]')
        if errores:
            tabla.add_row('Con error', f'[red]{errores}[/]')
        tabla.add_row('Tiempo', _fmt_seg(tiempo_seg))
        CONSOLA.print(tabla)
    else:
        print()
        print(f'{C.GRIS}{"─" * 72}{C.RESET}')
        print(f'  {C.NEGRITA}Escaneo completado{C.RESET}')
        print(f'  Procesados : {C.VERDE}{procesados}{C.RESET}')
        if errores:
            print(f'  Con error  : {C.ROJO}{errores}{C.RESET}')
        print(f'  Tiempo     : {_fmt_seg(tiempo_seg)}')
        print(f'{C.GRIS}{"─" * 72}{C.RESET}')
        print()


# ============================================================================
# Barra de progreso (comparación de pares)
# ============================================================================
class BarraProgreso:
    """Barra de progreso para comparación de pares.

    Con rich usa una Progress de rich; sin rich usa la barra ANSI clásica.
    """

    def __init__(self, total: int, ancho: int = 40, total_videos: int = 0):
        self.total = max(1, total)
        self.total_videos = total_videos
        self.videos_completos = 0
        self.ancho = ancho
        self._activo = sys.stderr.isatty()
        self._inicio = time.monotonic()
        self._ultimo_momento = 0.0
        self._terminada = False

    def actualizar_videos(self, actual: int, total: int) -> None:
        self.videos_completos = actual
        self.total_videos = total

    def actualizar(
        self,
        actual: float,
        total: int,
        nombre_a: str = '',
        nombre_b: str = '',
    ) -> None:
        if not self._activo or self._terminada:
            return

        ahora = time.monotonic()
        total_real = max(1, total)

        if actual < total_real and ahora - self._ultimo_momento < 0.10:
            return

        self._ultimo_momento = ahora
        fraccion = max(0.0, min(1.0, actual / total_real))
        llenos = int(self.ancho * fraccion)
        barra = '=' * llenos + '-' * (self.ancho - llenos)

        if fraccion > 0:
            transcurrido = ahora - self._inicio
            eta = transcurrido * (1.0 - fraccion) / fraccion
            eta_texto = _fmt_seg(eta)
        else:
            eta_texto = 'calculando'

        nombres = ''
        if nombre_a:
            nombres = f'  {nombre_a[:18]} / {nombre_b[:18]}'

        linea = (
            f'\r\033[2K  [{barra}] {fraccion:5.1%} '
            f'{self.videos_completos}/{self.total_videos} vídeos completos | '
            f'{min(int(actual), total)}/{total} pares ETA: {eta_texto}{nombres}'
        )
        print(linea, end='', file=sys.stderr, flush=True)

        if fraccion >= 1:
            print(file=sys.stderr)
            self._terminada = True

    def limpiar(self) -> None:
        if self._activo and not self._terminada:
            print('\r\033[2K', end='', file=sys.stderr, flush=True)


# ============================================================================
# Progreso de fingerprint (usado por fingerprint.py)
# ============================================================================
class ProgresoFingerprint:
    """Progreso por fase de procesamiento de un video."""

    def __init__(self):
        self._fase: Optional[str] = None
        self._ultimo_momento = 0.0
        self._ultimo_hito = -1
        self._tty = sys.stderr.isatty()

    def __call__(self, fase: str, porcentaje: float, eta: float) -> None:
        porcentaje = max(0.0, min(1.0, porcentaje))
        ahora = time.monotonic()

        if fase != self._fase:
            if self._fase is not None and self._tty:
                print(file=sys.stderr)
            self._fase = fase
            self._ultimo_momento = 0.0
            self._ultimo_hito = -1

        eta_texto = _fmt_seg(eta) if eta >= 0 else 'calculando'

        if self._tty:
            if porcentaje < 1 and ahora - self._ultimo_momento < 0.15:
                return
            self._ultimo_momento = ahora
            print(
                f'\r\033[2K    {fase:<12} {porcentaje:6.1%}  ETA: {eta_texto}',
                end='', file=sys.stderr, flush=True,
            )
        else:
            hito = min(4, int(porcentaje * 4))
            if hito > self._ultimo_hito:
                self._ultimo_hito = hito
                print(
                    f'    {fase}: {porcentaje:.0%} (ETA: {eta_texto})',
                    file=sys.stderr, flush=True,
                )

    def cerrar(self) -> None:
        if self._tty and self._fase is not None:
            print(file=sys.stderr)


class Dashboard:
    def __init__(self, num_workers: int, tty: bool = True):
        self._lock = threading.Lock()
        self._num_workers = max(1, num_workers)
        self._tty = tty
        self._workers: Dict[int, str] = {}
        self._thread_slots: Dict[int, int] = {}
        self._completados = 0
        self._errores = 0
        self._total = 0
        self._lineas_dibujadas = 0

    def iniciar(self, total: int) -> None:
        with self._lock:
            self._total = total
            self._completados = 0
            self._errores = 0
            self._workers = {i: 'Esperando asignación...' for i in range(self._num_workers)}
            self._thread_slots = {}
            self._lineas_dibujadas = 0
            if self._tty:
                sys.stdout.write('\n' * (self._num_workers + 1))
                sys.stdout.flush()
                self._lineas_dibujadas = self._num_workers + 1
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

    def actualizar_worker(self, tid: int, texto: str) -> None:
        with self._lock:
            if not self._workers:
                return
            slot = self._get_slot(tid)
            self._workers[slot] = texto
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
                if self._tty:
                    sys.stdout.write('\n' * (self._num_workers + 1))
                    self._lineas_dibujadas = self._num_workers + 1
                self._dibujar()

    def _dibujar(self) -> None:
        if not self._tty or self._lineas_dibujadas == 0:
            return
        sys.stdout.write(f'\033[{self._lineas_dibujadas}F')
        lineas = [
            f'\033[2K{AZUL}▶ Completados: {NEGRITA}{self._completados}{NC}{AZUL} / {self._total}{NC}'
            f'{AZUL} | Errores: {ROJO}{self._errores}{NC}'
        ]
        for i in range(self._num_workers):
            estado = self._workers.get(i, 'Esperando asignación...')
            lineas.append(f'\033[2K{AMARILLO}Worker {i + 1}:{NC} {estado}')
        sys.stdout.write('\n'.join(lineas) + '\n')
        sys.stdout.flush()
        self._lineas_dibujadas = len(lineas)

class ProgresoFingerprintWorker:
    def __init__(self, dashboard: Dashboard, worker_id: int, nombre_video: str):
        self._dashboard = dashboard
        self._worker_id = worker_id
        self._nombre = nombre_video
        self._ultimo_texto = ''
        self._ultimo_momento = 0.0

    def __call__(self, fase: str, fraccion: float, eta: float) -> None:
        fraccion = max(0.0, min(1.0, fraccion))
        ahora = time.monotonic()
        if ahora - self._ultimo_momento < 0.15 and fraccion < 1.0:
            return
        self._ultimo_momento = ahora
        eta_texto = _fmt_seg(eta) if eta >= 0 else 'calculando'
        
        ancho_barra = 10
        llenos = int(ancho_barra * fraccion)
        barra = '█' * llenos + '░' * (ancho_barra - llenos)

        ancho = shutil.get_terminal_size(fallback=(100, 24)).columns
        max_nombre = max(15, ancho - 90)
        nombre = self._nombre[: max_nombre - 1] + '…' if len(self._nombre) > max_nombre else self._nombre
        
        texto = f'{nombre} | {fase:<15} | {barra} {fraccion:5.1%} | ETA: {eta_texto}'
        if texto != self._ultimo_texto:
            self._ultimo_texto = texto
            self._dashboard.actualizar_worker(self._worker_id, texto)

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
    """Muestra dispositivos y carpetas, tanto con Rich como sin él."""
    if not dispositivos:
        imprimir('[yellow]No hay vídeos registrados para desglosar.[/]')
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
                f'[magenta]Audio: {disp["con_huella_audio"]:,}[/]  ·  '
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
            for carpeta in disp['carpetas']:
                tabla_rutas.add_row(
                    rich_escape(carpeta['ruta']), f'{carpeta["total_videos"]:,}',
                    _fmt_tamanio_estadisticas(carpeta['tamanio_bytes']),
                    f'{carpeta["con_sha256"]:,}', f'{carpeta["con_huella_visual"]:,}',
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
            print(f' Audio       : {disp["con_huella_audio"]:,}')
            print(f' Con error   : {disp["con_error"]:,}')
            if len(disp['nombres_registrados']) > 1:
                print(f' Nombres de este ID: {", ".join(disp["nombres_registrados"])}')
            print('  Carpetas registradas (solo vídeos directamente contenidos):')
            for carpeta in disp['carpetas']:
                print(f'    {carpeta["ruta"]}  ->  {carpeta["total_videos"]:,} vídeos; '
                      f'{_fmt_tamanio_estadisticas(carpeta["tamanio_bytes"])}; '
                      f'SHA256 {carpeta["con_sha256"]:,}; visual {carpeta["con_huella_visual"]:,}')
        print()
