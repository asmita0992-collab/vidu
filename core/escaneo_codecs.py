"""Módulo para el escaneo previo de códecs de vídeo y selección de aceleración."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.almacenamiento import carpeta_reportes, obtener_rutas_historial_escaneo, guardar_ruta_historial_escaneo
from core.database import info_dispositivo_actual, _usa_neon
from core.dependencias import EXTENSIONES_VIDEO
from core.datos import _filtrar_videos_pendientes
from core.horario import ahora_utc6, fmt_ahora_utc6, id_utc6
from core.inventario import _listar_videos, cargar_exclusiones, excluido
from core.metadata import extraer_metadatos
from core.presentacion import (
    imprimir, pedir_ruta, pedir_si_no, pedir_numero, CONSOLA, TIENE_RICH,
    NC, NEGRITA, AZUL, AMARILLO, VERDE, ROJO, GRIS, CIAN, BLANCO, MAGENTA
)
from core.plataforma import es_android, _carpetas_android_candidatas
from core.reporter import ProgresoMultilinea

try:
    from rich.table import Table
    from rich.markup import escape as rich_escape
    from rich import box as rich_box
except ImportError:
    Table = None  # type: ignore
    rich_box = None  # type: ignore
    rich_escape = lambda texto: str(texto)


def normalizar_nombre_codec(codec: str) -> str:
    """Normaliza el nombre del códec a una versión estándar (h264, h265, mpeg4, etc.)."""
    c = (codec or '').strip().lower()
    if c in ('h264', 'avc', 'avc1', 'x264'):
        return 'h264'
    if c in ('hevc', 'h265', 'x265'):
        return 'h265'
    if c in ('vp8', 'vp9'):
        return c
    if c in ('av1', 'av01'):
        return 'av1'
    return c or 'desconocido'


def determinar_aceleracion_por_codec(codec: str) -> str:
    """Determina si utilizar aceleración por software o hardware según el códec."""
    codec_norm = normalizar_nombre_codec(codec)
    if codec_norm == 'h264':
        return 'software'
    if codec_norm == 'h265':
        return 'auto'  # auto utiliza aceleración por hardware según la plataforma
    return 'software'


def obtener_codecs_videos_pendientes(videos: List[Path]) -> List[Dict[str, Any]]:
    """Inspecciona una lista de archivos de vídeo y obtiene la información de sus códecs."""
    resultados: List[Dict[str, Any]] = []
    total = len(videos)
    imprimir(f'[cyan]Analizando metadatos de {total} vídeo(s)...[/]')
    # El reporte de códecs puede tardar en ffprobe: la barra estándar
    # mantiene un avance de varias líneas legible también en Termux.
    with ProgresoMultilinea(total, 'Escaneo previo de códecs') as barra:
        for i, video in enumerate(videos, 1):
            barra.actualizar(i - 1, total, video.name)
            meta = extraer_metadatos(video)
            codec_video = meta.get('codec_video', '') or ''
            codec_norm = normalizar_nombre_codec(codec_video)
            aceleracion_sugerida = determinar_aceleracion_por_codec(codec_norm)

            ancho = meta.get('ancho', 0)
            alto = meta.get('alto', 0)
            resolucion_str = f'{ancho}x{alto}' if ancho and alto else 'N/D'

            resultados.append({
                'ruta': str(video.resolve()),
                'nombre': video.name,
                'codec_video': codec_video or 'desconocido',
                'codec_normalizado': codec_norm,
                'codec_audio': meta.get('codec_audio', '') or 'desconocido',
                'duracion': meta.get('duracion', 0.0),
                'resolucion': resolucion_str,
                'aceleracion_sugerida': aceleracion_sugerida,
            })
            barra.actualizar(i, total, video.name)
    return resultados


def guardar_reporte_escaneo_previo_codecs(
    directorio: str, resultados: List[Dict[str, Any]], total_pendientes: int
) -> Tuple[Path, Path]:
    """Guarda los resultados del escaneo previo de códecs en archivos JSON y TXT."""
    ident = info_dispositivo_actual()
    estampa_id = id_utc6()
    fecha_fmt = fmt_ahora_utc6()
    
    desglose: Dict[str, int] = {}
    desglose_acel: Dict[str, int] = {'software': 0, 'hardware (auto)': 0}
    
    for item in resultados:
        cn = item['codec_normalizado']
        desglose[cn] = desglose.get(cn, 0) + 1
        if item['aceleracion_sugerida'] == 'software':
            desglose_acel['software'] += 1
        else:
            desglose_acel['hardware (auto)'] += 1
            
    datos_reporte = {
        'fecha_utc6': fecha_fmt,
        'dispositivo': ident['dispositivo'],
        'dispositivo_id': ident['dispositivo_id'],
        'directorio': directorio,
        'total_videos_pendientes': total_pendientes,
        'desglose_codecs': desglose,
        'desglose_aceleracion': desglose_acel,
        'videos': resultados,
    }
    
    dir_json = carpeta_reportes() / 'escaneos' / 'json'
    dir_txt = carpeta_reportes() / 'escaneos' / 'txt'
    dir_json.mkdir(parents=True, exist_ok=True)
    dir_txt.mkdir(parents=True, exist_ok=True)
    
    ruta_json = dir_json / f'escaneo_previo_codecs_{estampa_id}.json'
    ruta_ultimo_json = dir_json / 'ultimo_escaneo_previo_codecs.json'
    ruta_txt = dir_txt / f'escaneo_previo_codecs_{estampa_id}.txt'
    
    # Escribir JSON
    with ruta_json.open('w', encoding='utf-8') as f:
        json.dump(datos_reporte, f, indent=2, ensure_ascii=False)
    with ruta_ultimo_json.open('w', encoding='utf-8') as f:
        json.dump(datos_reporte, f, indent=2, ensure_ascii=False)
        
    # Escribir TXT
    lineas_txt = [
        '========================================================================',
        '               VIDUP — Reporte de Escaneo Previo de Códecs',
        '========================================================================',
        f'Fecha           : {fecha_fmt}',
        f'Dispositivo     : {ident["dispositivo"]} (ID: {ident["dispositivo_id"]})',
        f'Directorio      : {directorio}',
        f'Total pendientes: {total_pendientes}',
        '------------------------------------------------------------------------',
        'Desglose de vídeos por códec:',
    ]
    for cdec, cant in desglose.items():
        lineas_txt.append(f'  - {cdec.upper()}: {cant} vídeo(s)')
    lineas_txt.append('------------------------------------------------------------------------')
    lineas_txt.append('Desglose de aceleración recomendada:')
    lineas_txt.append(f'  - Software (h264 / otros): {desglose_acel["software"]} vídeo(s)')
    lineas_txt.append(f'  - Hardware (h265):         {desglose_acel["hardware (auto)"]} vídeo(s)')
    lineas_txt.append('------------------------------------------------------------------------')
    lineas_txt.append('Detalle por vídeo:')
    lineas_txt.append(f'{"#":<4} {"Códec":<10} {"Aceleración":<12} {"Resolución":<12} {"Vídeo"}')
    lineas_txt.append('-' * 72)
    
    for idx, item in enumerate(resultados, 1):
        acel_tag = 'software' if item['aceleracion_sugerida'] == 'software' else 'hardware'
        lineas_txt.append(
            f'{idx:<4} {item["codec_normalizado"].upper():<10} {acel_tag:<12} {item["resolucion"]:<12} {item["nombre"]}'
        )
    lineas_txt.append('========================================================================')
    
    with ruta_txt.open('w', encoding='utf-8') as f:
        f.write('\n'.join(lineas_txt))
        
    return ruta_json, ruta_txt


def mostrar_resumen_escaneo_previo_codecs(
    resultados: List[Dict[str, Any]], ruta_json: Path, ruta_txt: Path
) -> None:
    """Muestra una tabla legible con la información de los códecs detectados y los reportes guardados."""
    if not resultados:
        imprimir('[yellow]No hay resultados para mostrar.[/]')
        return
        
    desglose: Dict[str, int] = {}
    desglose_acel: Dict[str, int] = {'software': 0, 'hardware': 0}
    for item in resultados:
        cn = item['codec_normalizado']
        desglose[cn] = desglose.get(cn, 0) + 1
        if item['aceleracion_sugerida'] == 'software':
            desglose_acel['software'] += 1
        else:
            desglose_acel['hardware'] += 1
            
    imprimir('\n[bold cyan]=== RESUMEN DE ESCANEO PREVIO DE CÓDECS ===[/]')
    imprimir(f'[bright_white]Total de vídeos pendientes analizados:[/] [bold gold1]{len(resultados)}[/]')
    
    imprimir('\n[bold deep_sky_blue1]Desglose por códec:[/]')
    for cdec, cant in desglose.items():
        imprimir(f'  • [bold spring_green2]{cdec.upper()}:[/] {cant} vídeo(s)')
        
    imprimir('\n[bold deep_sky_blue1]Modo de aceleración asignado:[/]')
    imprimir(f'  • [turquoise2]Software (h264 / otros):[/] {desglose_acel["software"]} vídeo(s)')
    imprimir(f'  • [turquoise2]Hardware (h265):[/]         {desglose_acel["hardware"]} vídeo(s)')

    if CONSOLA is not None and Table is not None and rich_box is not None:
        imprimir('')
        tabla = Table(title='Detalle de vídeos pendientes', box=rich_box.ROUNDED, border_style='cyan')
        tabla.add_column('Nº', style='bold gold1', width=4)
        tabla.add_column('Códec', style='spring_green2', width=10)
        tabla.add_column('Aceleración', style='turquoise2', width=12)
        tabla.add_column('Resolución', style='white', width=12)
        tabla.add_column('Nombre del archivo', style='bright_white')
        
        for idx, item in enumerate(resultados, 1):
            acel_str = 'software' if item['aceleracion_sugerida'] == 'software' else 'hardware'
            tabla.add_row(
                str(idx),
                item['codec_normalizado'].upper(),
                acel_str,
                item['resolucion'],
                item['nombre']
            )
        CONSOLA.print(tabla)
    else:
        print('\nDetalle de vídeos:')
        print(f'{"#":<4} {"Códec":<10} {"Aceleración":<12} {"Resolución":<12} {"Nombre"}')
        print('-' * 70)
        for idx, item in enumerate(resultados, 1):
            acel_str = 'software' if item['aceleracion_sugerida'] == 'software' else 'hardware'
            print(f'{idx:<4} {item["codec_normalizado"].upper():<10} {acel_str:<12} {item["resolucion"]:<12} {item["nombre"]}')

    imprimir(f'\n[green]Reporte JSON guardado en:[/] [cyan]{ruta_json}[/]')
    imprimir(f'[green]Reporte TXT guardado en :[/] [cyan]{ruta_txt}[/]')


def cargar_ultimo_escaneo_previo_codecs() -> Optional[Dict[str, Any]]:
    """Carga el reporte del último escaneo previo de códecs si existe."""
    ruta_ultimo = carpeta_reportes() / 'escaneos' / 'json' / 'ultimo_escaneo_previo_codecs.json'
    if not ruta_ultimo.is_file():
        return None
    try:
        with ruta_ultimo.open('r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None
