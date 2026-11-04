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
    CONSOLA.print(tabla)

    indice = 1
    for tipo in orden_tipos:
        grupo = por_tipo[tipo]
        if not grupo:
            continue
        label, color = _TIPO_META.get(tipo, (tipo.upper(), 'grey'))
        CONSOLA.print(Rule(
            f'[{color}][bold]{label} ({len(grupo)})[/][/]',
            style=color,
        ))
        for r in grupo:
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

    indice = 1
    for tipo in orden_tipos:
        grupo = por_tipo[tipo]
        if not grupo:
            continue
        label, color = _TIPO_META.get(tipo, (tipo.upper(), 'grey'))
        mapa = {
            'red': C.ROJO, 'yellow': C.AMARILLO, 'cyan': C.CIAN,
            'magenta': C.MAGENTA, 'grey': C.GRIS,
        }
        c = mapa.get(color, C.GRIS)
        print(f'\n{c}{C.NEGRITA}  {"─" * 10} {label} ({len(grupo)}) {"─" * 10}{C.RESET}')
        for r in grupo:
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
        if omitidos:
            tabla.add_row('Omitidos', f'[grey]{omitidos}[/]  [bright_black](ya tenían huella)[/]')
        if errores:
            tabla.add_row('Con error', f'[red]{errores}[/]')
        tabla.add_row('Tiempo', _fmt_seg(tiempo_seg))
        CONSOLA.print(tabla)
    else:
        print()
        print(f'{C.GRIS}{"─" * 72}{C.RESET}')
        print(f'  {C.NEGRITA}Escaneo completado{C.RESET}')
        print(f'  Procesados : {C.VERDE}{procesados}{C.RESET}')
        if omitidos:
            print(f'  Omitidos   : {C.GRIS}{omitidos}{C.RESET}  (ya tenían huella)')
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