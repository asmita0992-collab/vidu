"""Operaciones de datos de VIDUP: exportar, importar, estadisticas, limpieza
y consultas de hashes previos. Extraidas de vidup.py sin cambios de logica."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from core.presentacion import imprimir, CONSOLA
from core.telegram import notificar_inicio, notificar_fin
from core.almacenamiento import (
    carpeta_reportes as _carpeta_reportes,
    _rutas_con_error_registrado, _quitar_error_registrado,
)
from core.reporter import _mostrar_desglose_estadisticas

try:
    from rich.table import Table
    from rich.markup import escape as rich_escape
    from rich import box as rich_box
except ImportError:
    Table = None  # type: ignore
    rich_box = None  # type: ignore
    rich_escape = lambda texto: str(texto)


def _tiene_huella_completa(conn, rr: str) -> bool:
    """True si el vídeo ya tiene huella visual completa (no solo SHA256).

    Compara normalizando separadores de ruta (\\ vs /) para evitar que
    diferencias entre Windows y Linux/Android provoquen re-procesamientos.
    """
    from core.database import _usa_neon
    rr_norm = rr.replace('\\', '/')
    if _usa_neon():
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT hash_rapido, EXISTS(SELECT 1 FROM frame_hashes WHERE video_id=videos.id) AS huella_visual "
                "FROM videos WHERE REPLACE(ruta, '\\\\', '/') = %s OR ruta = %s",
                (rr_norm, rr),
            )
            fila = cur.fetchone()
    else:
        fila = conn.execute(
            "SELECT hash_rapido, EXISTS(SELECT 1 FROM frame_hashes WHERE video_id=videos.id) AS huella_visual "
            "FROM videos WHERE REPLACE(ruta, '\\', '/') = ? OR ruta = ?",
            (rr_norm, rr),
        ).fetchone()
    return bool(fila and fila['hash_rapido'] != 'sha256_solo' and fila['huella_visual'])


def _filtrar_videos_pendientes(videos: List[Path], db: str, reintentar_errores: bool, forzar_registrados: bool) -> Tuple[List[Path], int]:
    if forzar_registrados and reintentar_errores:
        return videos, 0

    from core.database import cerrar, conectar

    pendientes: List[Path] = []
    omitidos = 0
    rutas_con_error = _rutas_con_error_registrado()
    completas_con_error: List[str] = []
    conn = conectar(db)

    try:
        for ruta in videos:
            rr = str(ruta.resolve())
            if _tiene_huella_completa(conn, rr):
                if rr in rutas_con_error:
                    completas_con_error.append(rr)
                if forzar_registrados:
                    pendientes.append(ruta)
                else:
                    omitidos += 1
                continue
            if rr in rutas_con_error and not reintentar_errores:
                omitidos += 1
                continue
            pendientes.append(ruta)
    finally:
        cerrar(conn)

    if completas_con_error:
        _quitar_error_registrado(completas_con_error)
    return pendientes, omitidos

def _cargar_hashes_previos(videos: List[Path], db: str) -> Dict[str, dict]:
    """Devuelve el SHA256 ya guardado por ruta, para no recalcularlo."""
    from core.database import _usa_neon, cerrar, conectar

    try:
        return _consultar_hashes_previos(videos, db)
    except Exception as exc:
        imprimir(f'[yellow][AVISO] No se pudo consultar el SHA256 previo: {exc}. Se recalculará.[/]')
        return {}

def _consultar_hashes_previos(videos: List[Path], db: str) -> Dict[str, dict]:
    """Consulta el SHA256 ya guardado por ruta."""
    from core.database import _usa_neon, cerrar, conectar

    previos: Dict[str, dict] = {}
    conn = conectar(db)
    try:
        if _usa_neon():
            import psycopg2.extras
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                for ruta in videos:
                    rr = str(ruta.resolve())
                    rr_norm = rr.replace('\\', '/')
                    cur.execute(
                        "SELECT hash_archivo, hash_rapido, tamanio_bytes FROM videos WHERE REPLACE(ruta, '\\\\', '/') = %s OR ruta = %s",
                        (rr_norm, rr),
                    )
                    fila = cur.fetchone()
                    if fila and fila['hash_archivo']:
                        previos[rr] = dict(fila)
        else:
            for ruta in videos:
                rr = str(ruta.resolve())
                rr_norm = rr.replace('\\', '/')
                fila = conn.execute(
                    "SELECT hash_archivo, hash_rapido, tamanio_bytes FROM videos WHERE REPLACE(ruta, '\\', '/') = ? OR ruta = ?",
                    (rr_norm, rr),
                ).fetchone()
                if fila and fila['hash_archivo']:
                    previos[rr] = dict(fila)
    finally:
        cerrar(conn)
    return previos

def cmd_exportar(args: argparse.Namespace) -> int:
    from core.database import _usa_neon, cerrar, conectar, exportar_json, exportar_txt
    notificar_inicio('Exportar base de datos', f'Base: {args.db}')
    if not _usa_neon() and not Path(args.db).is_file():
        imprimir(f'[red][ERROR] No existe la base de datos: {args.db}[/]')
        return 1
    conn = conectar(args.db)
    try:
        cantidad = exportar_json(conn, args.salida)
        ruta_txt = _carpeta_reportes() / 'exportaciones' / 'txt' / (Path(args.salida).stem + '.txt')
        exportar_txt(conn, str(ruta_txt))
    finally:
        cerrar(conn)
    imprimir(f'[spring_green2]Exportados: {cantidad} vídeos → {args.salida} (TXT en carpeta de txt)[/]')
    notificar_fin('Exportar base de datos', True, f'{cantidad} vídeos')
    return 0

def cmd_importar(args: argparse.Namespace) -> int:
    from core.database import _usa_neon, cerrar, conectar, importar_json
    notificar_inicio('Importar base de datos JSON', f'Archivo: {args.archivo}')
    if not Path(args.archivo).is_file():
        imprimir(f'[red][ERROR] No existe el archivo: {args.archivo}[/]')
        return 1
    conn = conectar(args.db)
    try:
        conteo = importar_json(conn, args.archivo, omitir_existentes=not args.forzar)
    finally:
        cerrar(conn)
    imprimir(f'Importados: {conteo["importados"]}; omitidos: {conteo["omitidos"]}; errores: {conteo["errores"]}',
             estilo='green' if conteo['errores'] == 0 else 'yellow')
    notificar_fin('Importar base de datos JSON', conteo['errores'] == 0,
                  f'{conteo["importados"]} importados, {conteo["errores"]} errores')
    return 0 if conteo['errores'] == 0 else 1

def cmd_descargar_neon(args: argparse.Namespace) -> int:
    from core.database import _usa_neon, cerrar, conectar, describir_conexion, volcar_todo_a_sqlite
    if not _usa_neon():
        imprimir('[yellow][AVISO] No hay conexión Neon activa; nada que descargar.[/]')
        return 1

    notificar_inicio('Descargar respaldo Neon', f'Destino: {args.salida}')
    ruta_destino = args.salida
    imprimir(f'[cyan]Modo de conexión: {describir_conexion()}[/]')
    imprimir(f'[cyan]Descargando desde Neon a: {ruta_destino}[/]', )

    conn = conectar('neon_cloud')
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM videos')
            total = int(cur.fetchone()[0] or 0)
    finally:
        cerrar(conn)

    if total == 0:
        imprimir('[yellow]La base Neon está vacía; nada que descargar.[/]')
        return 0

    imprimir(f'[cyan]Registros a descargar: {total}[/]')

    inicio = time.monotonic()
    ultimo_aviso = [0.0]

    def progreso(hechos, total_v, errores):
        ahora = time.monotonic()
        if ahora - ultimo_aviso[0] < 1.0 and hechos < total_v:
            return
        ultimo_aviso[0] = ahora
        pct = (hechos / total_v) * 100 if total_v else 0
        sys.stdout.write(f'\r Progreso: {hechos}/{total_v} ({pct:5.1f}%) | errores: {errores}')
        sys.stdout.flush()

    conteo = volcar_todo_a_sqlite(ruta_destino, lote=200, progreso=progreso)
    print()
    tiempo = time.monotonic() - inicio

    imprimir(f'[spring_green2]Respaldo guardado en: {ruta_destino}[/]')
    imprimir(f'[cyan]Vídeos copiados : {conteo["videos"]} / {total}[/]')
    imprimir(f'[cyan]Frames copiados : {conteo["frames"]}[/]')
    imprimir(f'[cyan]Errores         : {conteo["errores"]}[/]')
    imprimir(f'[cyan]Lotes fallidos  : {conteo["lotes_fallidos"]}[/]')
    imprimir(f'[cyan]Tiempo          : {tiempo:.1f} s[/]')

    ok = conteo['videos'] == total and conteo['lotes_fallidos'] == 0
    notificar_fin('Descargar respaldo Neon', ok, f'{conteo["videos"]}/{total} vídeos')
    return 0 if ok else 2

def cmd_stats(args: argparse.Namespace) -> int:
    from core.database import (
        _usa_neon, cerrar, conectar, estadisticas,
        estadisticas_por_dispositivo, describir_conexion,
    )
    if not _usa_neon() and not Path(args.db).is_file():
        imprimir(f'[red][ERROR] La base de datos no existe: {rich_escape(str(args.db))}[/]')
        return 1

    conn = conectar(args.db)
    try:
        datos = estadisticas(conn)
        desglose = estadisticas_por_dispositivo(conn)
    finally:
        cerrar(conn)

    # Este chequeo protege contra conteos incompletos por errores de consulta.
    total_desglosado = sum(d['total_videos'] for d in desglose)
    if total_desglosado != datos['total_videos']:
        imprimir('[red][ERROR] Los totales del desglose no coinciden con la base. '
                 'No se mostrará información potencialmente incompleta.[/]')
        return 2

    if CONSOLA is not None and Table is not None and rich_box is not None:
        tabla = Table(title='Estadísticas generales', box=rich_box.ROUNDED, border_style='cyan')
        tabla.add_column('Campo', style='deep_sky_blue1')
        tabla.add_column('Valor', style='white')
        tabla.add_row('Modo conexión', describir_conexion())
        tabla.add_row('Ruta de la BD', 'Neon PostgreSQL (Nube)' if _usa_neon() else rich_escape(str(args.db)))
        tabla.add_row('Vídeos totales', f'{datos["total_videos"]:,}')
        tabla.add_row('Procesados sin error', f'{datos["con_huella"]:,}')
        tabla.add_row('Con error', f'{datos["con_error"]:,}')
        tabla.add_row('Duración total', f'{datos["horas_totales"]:,.2f} h')
        tabla.add_row('Tamaño total', f'{datos["gb_totales"]:,.3f} GB')
        tabla.add_row('Dispositivos/grupos registrados', str(len(desglose)))
        CONSOLA.print(tabla)
    else:
        print()
        print(f' Modo de conexión : {describir_conexion()}')
        print(f' Ruta de la BD    : {"Neon PostgreSQL (Nube)" if _usa_neon() else args.db}')
        print(f' Vídeos totales   : {datos["total_videos"]:,}')
        print(f' Sin error        : {datos["con_huella"]:,}')
        print(f' Con error        : {datos["con_error"]:,}')
        print(f' Duración total   : {datos["horas_totales"]:,.2f} h')
        print(f' Tamaño total     : {datos["gb_totales"]:,.3f} GB')
        print(f' Grupos disp.     : {len(desglose)}')

    _mostrar_desglose_estadisticas(desglose)

    if datos.get('dispositivos_origen'):
        if CONSOLA is not None and Table is not None and rich_box is not None:
            tabla_origen = Table(title='Etiquetas de origen (referencias, no conteos adicionales)',
                                 box=rich_box.SIMPLE, border_style='grey37')
            tabla_origen.add_column('Origen registrado', style='grey70')
            for nombre in datos['dispositivos_origen']:
                tabla_origen.add_row(rich_escape(nombre))
            CONSOLA.print(tabla_origen)
        else:
            print(' Etiquetas de origen (no agregan vídeos):')
            for nombre in datos['dispositivos_origen']:
                print(f'  • {nombre}')

    imprimir('[grey70]Los conteos son registros de la base, no una comprobación '
             'de archivos existentes en el disco. Cada carpeta contiene únicamente '
             'los vídeos directamente ubicados en ella.[/]')
    return 0

def cmd_limpiar(args: argparse.Namespace) -> int:
    from core.database import _usa_neon, cerrar, conectar, eliminar_por_ruta
    if not _usa_neon() and not Path(args.db).is_file():
        imprimir(f'[red][ERROR] La base de datos no existe: {args.db}[/]')
        return 1
    ruta = str(Path(args.ruta).expanduser().resolve())
    conn = conectar(args.db)
    try:
        eliminado = eliminar_por_ruta(conn, ruta)
    finally:
        cerrar(conn)
    if eliminado:
        imprimir(f'[spring_green2]Huella eliminada: {ruta}[/]')
        return 0
    imprimir(f'[red]La ruta no estaba en la base de datos: {ruta}[/]')
    return 1

def cmd_eliminar_por_dispositivo(args: argparse.Namespace) -> int:
    from core.database import _usa_neon, cerrar, conectar, eliminar_por_dispositivo_id, estadisticas
    if not _usa_neon() and not Path(args.db).is_file():
        imprimir(f'[red][ERROR] La base de datos no existe: {args.db}[/]')
        return 1
    conn = conectar(args.db)
    try:
        if args.listar:
            datos = estadisticas(conn)
            imprimir('[cyan]Dispositivos registrados:[/]')
            for d in datos['dispositivos']:
                imprimir(f'  • {d}', estilo='white')
            return 0
        if not args.dispositivo_id:
            imprimir('[red][ERROR] Falta --dispositivo-id.[/]')
            return 2
        eliminados = eliminar_por_dispositivo_id(conn, args.dispositivo_id)
    finally:
        cerrar(conn)
    imprimir(f'[spring_green2]Eliminados: {eliminados} registro(s) del dispositivo {args.dispositivo_id}[/]')
    return 0


def _guardar_resumen_json(ruta: str, procesados: int, omitidos: int,
                          errores: list, tiempo_seg: float) -> None:
    from core.database import info_dispositivo_actual, describir_conexion
    destino = Path(ruta)
    destino.parent.mkdir(parents=True, exist_ok=True)
    datos = {
        'procesados': procesados,
        'omitidos': omitidos,
        'errores': errores,
        'tiempo_seg': tiempo_seg,
        'dispositivo': info_dispositivo_actual(),
        'conexion': describir_conexion(),
    }
    with destino.open('w', encoding='utf-8') as archivo:
        json.dump(datos, archivo, ensure_ascii=False, indent=4)
