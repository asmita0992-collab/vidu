from __future__ import annotations
import argparse
import csv
import json
import math
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from core.horario import ahora_utc6, fmt_ahora_utc6, id_utc6, ZONA_MEXICO
from core.plataforma import (
    detectar_so, so_legible, distro_linux, es_android,
    arquitectura_maquina, es_windows, es_macos, es_linux
)
from core.presentacion import (
    imprimir, regla, panel, pedir_si_no, pedir_numero, pedir_texto,
    pedir_ruta, pedir_limite_escaneo, RegresarAlMenu, _PALABRAS_ESCAPE, _MAX_INTENTOS,
    linea_dep, linea_opcional, escapar_para_telegram,
    CONSOLA, TIENE_RICH, Panel, Rule,
    NC, NEGRITA, AZUL, AMARILLO, VERDE, ROJO, GRIS, CIAN, BLANCO, MAGENTA,
)
from core.reporter import (
    Dashboard, ProgresoFingerprintWorker, _fmt_seg, _mostrar_desglose_estadisticas,
)
from core.telegram import (
    configurado as telegram_configurado, enviar as telegram_enviar,
    notificar_inicio, notificar_fin, notificar_error, probar as probar_telegram
)
from core.dependencias import verificar_obligatorias, EXTENSIONES_VIDEO
from core.almacenamiento import (
    carpeta_reportes as _carpeta_reportes,
    ruta_errores_jsonl as _ruta_errores_jsonl,
    ruta_errores_txt as _ruta_errores_txt,
    asegurar_estructura_reportes as _asegurar_estructura_reportes,
    base_reciente_por_defecto as _base_reciente_por_defecto,
    carpeta_salida_videos as _carpeta_salida_videos,
    _rutas_con_error_registrado, _registrar_error_video, _quitar_error_registrado,
    _archivos_recientes, elegir_archivo_reciente,
)
from core.plataforma import set_high_priority, _carpetas_android_candidatas
from core.inventario import _es_video, _listar_videos
from core.datos import (
    _guardar_resumen_json,
    _filtrar_videos_pendientes, _cargar_hashes_previos, _consultar_hashes_previos,
    cmd_exportar, cmd_importar, cmd_descargar_neon, cmd_stats,
    cmd_limpiar, cmd_eliminar_por_dispositivo,
)
_ahora_utc6 = ahora_utc6
_fmt_ahora_utc6 = fmt_ahora_utc6
_id_utc6 = id_utc6
_notificar_inicio = notificar_inicio
_notificar_fin = notificar_fin
_notificar_error = notificar_error
DIR_SCRIPT: Path = Path(__file__).resolve().parent.parent
ENV_PATH: Path = DIR_SCRIPT / ".env"
try:
    from dotenv import load_dotenv
    if ENV_PATH.is_file():
        load_dotenv(dotenv_path=ENV_PATH, override=False)
except ImportError:
    pass
try:
    from rich.table import Table
    from rich.markup import escape as rich_escape
    from rich import box as rich_box
    from rich.progress import (
        Progress, SpinnerColumn, BarColumn, TextColumn,
        TimeElapsedColumn, TimeRemainingColumn, MofNCompleteColumn,
    )
except ImportError:
    Table = None  # type: ignore
    rich_box = None  # type: ignore
    rich_escape = lambda texto: str(texto)
    Progress = None  # type: ignore
_PALABRAS_SALIR = {'0', '00', 'salir', 'exit', 'quit', 'q'}


def cambiar_rutas_interactivo(db: str) -> None:
    """Reubicar inventario sin volver a escanear ni mover archivos físicos."""
    from core import cambiar_rutas
    from core.database import conectar, cerrar, estadisticas_por_dispositivo
    conn = conectar(db)
    try:
        dispositivos = [d for d in estadisticas_por_dispositivo(conn) if d['id_verificado']]
        if not dispositivos:
            imprimir('[yellow]No hay dispositivos con ID verificable.[/]')
            return
        for i, disp in enumerate(dispositivos, 1):
            imprimir(f'  [cyan]{i}.[/] {rich_escape(disp["dispositivo"])} '
                     f'({rich_escape(disp["dispositivo_id"])}) — {disp["total_videos"]} vídeos')
        n = int(pedir_numero('Selecciona el dispositivo', 1, 1, len(dispositivos), entero=True)) - 1
        elegido = dispositivos[n]
        es_carpeta = pedir_si_no('¿Reubicar una carpeta y todas sus subcarpetas?', True)
        origen = pedir_texto('Ruta completa ACTUAL registrada en la base', obligatorio=True)
        destino = pedir_texto('Nueva ruta completa', obligatorio=True)
        plan = cambiar_rutas.preparar(conn, elegido['dispositivo_id'], origen, destino, es_carpeta)
        imprimir(f'[yellow]Dispositivo: {rich_escape(elegido["dispositivo"])} '
                 f'(ID {rich_escape(elegido["dispositivo_id"])})[/]')
        imprimir(f'[yellow]Registros que cambiarán: {len(plan)}[/]')
        for item in plan[:10]:
            imprimir(f'[cyan]{rich_escape(item["anterior"])}[/] → '
                     f'[green]{rich_escape(item["nueva"])}[/]')
        if len(plan) > 10:
            imprimir(f'[grey70]... y {len(plan)-10} cambios adicionales.[/]')
        imprimir('[yellow]Esto SOLO modifica rutas en la base; no mueve vídeos ni recalcula hashes.[/]')
        if not pedir_si_no('¿Son correctos el dispositivo y las rutas?', False):
            return
        if pedir_texto('Escribe CAMBIAR RUTAS para confirmar', obligatorio=True) != 'CAMBIAR RUTAS':
            imprimir('[yellow]Confirmación incorrecta. Cancelado.[/]')
            return
        def progreso(i, total, ruta):
            if i == 1 or i % 20 == 0 or i == total:
                imprimir(f'[cyan]Actualizando rutas: {i}/{total} — {rich_escape(ruta)}[/]')
        resultado = cambiar_rutas.ejecutar(
            conn,elegido['dispositivo_id'],elegido['dispositivo'],origen,destino,
            es_carpeta,_carpeta_reportes() / 'cambios_rutas',progreso)
        imprimir(f'[green]Registros actualizados: {resultado["actualizados"]}[/]')
        for formato,ruta in resultado['reportes'].items():
            imprimir(f'[green]Respaldo {formato.upper()}: {rich_escape(ruta)}[/]')
    finally:
        cerrar(conn)


def completar_metadatos_interactivo(db: str) -> None:
    """Recupera datos ligeros desde archivos del equipo actual solamente."""
    from core import completar_metadatos as cm, inventario
    from core.database import conectar, cerrar, info_dispositivo_actual
    ident = info_dispositivo_actual()
    did = ident['dispositivo_id']
    excl = inventario.cargar_exclusiones(DIR_SCRIPT, did)
    conn = conectar(db)
    try:
        candidatos = cm.incompletos(conn, did, excl)
        imprimir(f'[cyan]Dispositivo actual: {rich_escape(ident["dispositivo"])} (ID {did})[/]')
        imprimir(f'[yellow]Registros con metadatos incompletos: {len(candidatos)}[/]')
        imprimir('[grey70]Se usará ffprobe; no se calcula SHA256, no se modifican hashes ni datos existentes. ' 
                 'Solo se pueden consultar archivos accesibles desde este equipo.[/]')
        if not candidatos or not pedir_si_no('¿Completar únicamente los campos faltantes?', False):
            return
        def progreso(i, total, estado):
            imprimir(f'[cyan]Metadatos {i}/{total}[/] '
                     f'[yellow]{rich_escape(candidatos[i-1]["nombre"])}[/] '
                     f'[green]{rich_escape(estado)}[/]')
        resultado = cm.ejecutar(conn, did, _carpeta_reportes() / 'metadatos', excl, progreso)
        for clave, cantidad in resultado['conteo'].items():
            imprimir(f'[cyan]{clave}: {cantidad}[/]')
        imprimir(f'[green]Respaldo previo: {rich_escape(resultado["respaldo"])}[/]')
        for formato, ruta in resultado['reportes'].items():
            imprimir(f'[green]Reporte {formato.upper()}: {rich_escape(ruta)}[/]')
    finally:
        cerrar(conn)


def depurar_carpeta_por_dispositivo(db: str) -> None:
    """Depuración limitada a ID+carpeta, respaldando todos los hashes antes."""
    from core import inventario
    from core.database import conectar, cerrar, estadisticas_por_dispositivo
    conn = conectar(db)
    try:
        dispositivos = [d for d in estadisticas_por_dispositivo(conn) if d['id_verificado']]
        if not dispositivos:
            imprimir('[yellow]No hay dispositivos con ID verificable.[/]')
            return
        imprimir('\n[cyan]Selecciona el dispositivo propietario de las rutas:[/]')
        for i, d in enumerate(dispositivos, 1):
            imprimir(f'  [gold1]{i}.[/] {rich_escape(d["dispositivo"])} '
                     f'({rich_escape(d["dispositivo_id"])}) — {d["total_videos"]} vídeos')
        seleccionado = int(pedir_numero('Número del dispositivo', 1, 1,
                                       len(dispositivos), entero=True)) - 1
        disp = dispositivos[seleccionado]
        ruta = pedir_texto('Carpeta cuya información eliminarás (incluye subcarpetas)',
                          obligatorio=True)
        if not (Path(ruta).is_absolute() or inventario.es_windows(ruta)):
            imprimir('[red]La carpeta debe tener una ruta absoluta.[/]')
            return
        registros = inventario.registros_carpeta(conn, disp['dispositivo_id'], ruta)
        if not registros:
            imprimir('[yellow]No existe esa carpeta con registros en el dispositivo elegido. '
                     'No se ha eliminado nada.[/]')
            return
        imprimir(f'[yellow]Dispositivo: {rich_escape(disp["dispositivo"])} '
                 f'(ID {rich_escape(disp["dispositivo_id"])})[/]')
        imprimir(f'[yellow]Carpeta: {rich_escape(ruta)} (y subcarpetas)[/]')
        imprimir(f'[yellow]Registros afectados: {len(registros)}[/]')
        imprimir('[grey70]Se eliminarán REGISTROS de la base, no vídeos del disco. '
                 'Se guardará un respaldo completo de sus huellas.[/]')
        if not pedir_si_no('¿Confirmas que es el dispositivo y la carpeta correctos?', False):
            return
        respuesta = pedir_texto('Escribe ELIMINAR REGISTROS para confirmar', obligatorio=True)
        if respuesta != 'ELIMINAR REGISTROS':
            imprimir('[yellow]No coincide la frase de confirmación. Cancelado.[/]')
            return
        # Respaldar primero el conjunto exacto de filas y sus huellas de frames.
        completos = inventario.agregar_huellas_frames(conn, registros)
        carpeta_salida = _carpeta_reportes() / 'depuraciones'
        destinos = inventario.reportar_depuracion(
            carpeta_salida, disp['dispositivo'], disp['dispositivo_id'], ruta,
            completos, 'RESPALDO PREVIO - ELIMINACION PENDIENTE')
        total = inventario.eliminar_registros_exactos(conn, disp['dispositivo_id'], registros)
        try:
            inventario.reportar_depuracion(
                carpeta_salida, disp['dispositivo'], disp['dispositivo_id'], ruta,
                completos, 'ELIMINADO DE LA BASE', destinos)
        except Exception as exc:
            imprimir(f'[yellow]Datos eliminados, pero no se pudo actualizar '
                     f'el estado del reporte: {rich_escape(str(exc))}[/]')
        imprimir(f'[green]Se eliminaron {total} registros del dispositivo seleccionado.[/]')
        for formato, destino in destinos.items():
            imprimir(f'[cyan]Respaldo {formato.upper()}: {rich_escape(str(destino))}[/]')
    finally:
        cerrar(conn)


def comprobar_disco_bd_interactivo(db: str) -> None:
    """Compara disco y base de datos en ambas direcciones (antes en menú 7, opción 2)."""
    from core import inventario
    from core.database import cerrar, conectar, info_dispositivo_actual
    actual = info_dispositivo_actual()
    dispositivo_id = actual['dispositivo_id']
    excluidas = inventario.cargar_exclusiones(DIR_SCRIPT, dispositivo_id)
    imprimir('[cyan]La comprobación solo verifica rutas del dispositivo ACTUAL, '
             'no archivos de otros dispositivos que compartan letra de unidad.[/]')
    imprimir(f'[cyan]Dispositivo: {rich_escape(actual["dispositivo"])} '
             f'(ID {rich_escape(dispositivo_id)})[/]')
    imprimir('[grey70]Es rápida: compara nombres y rutas. No calcula hashes.[/]')
    if excluidas:
        imprimir(f'[yellow]Exclusiones activas: {len(excluidas)}[/]')
    adicionales = []
    while pedir_si_no('¿Añadir una carpeta para buscar vídeos que aún no están en la base?', False):
        adicionales.append(pedir_ruta('Carpeta adicional', existente=True, directorio=True))
    recursivo = (pedir_si_no('¿Incluir subcarpetas de las carpetas adicionales?', True)
                 if adicionales else False)
    conn = conectar(db)
    try:
        registros = inventario.registros_dispositivo(conn, dispositivo_id)
    finally:
        cerrar(conn)
    if not registros and not adicionales:
        imprimir('[yellow]No hay registros de este dispositivo. '
                 'Añade una carpeta adicional si quieres buscar archivos sin registro.[/]')
        return
    resultado = inventario.verificar_bidireccional(
        registros, dispositivo_id, excluidas, adicionales, recursivo)
    destinos = inventario.reportar_verificacion(
        _carpeta_reportes() / 'verificaciones', resultado)
    imprimir(f'[green]Registrados presentes: {resultado["presentes"]}[/]')
    imprimir(f'[yellow]En BD, pero ausentes de su ruta: {len(resultado["faltantes"])}[/]')
    imprimir(f'[cyan]En disco, pero sin ruta en BD: {len(resultado["sin_registro"])}[/]')
    imprimir(f'[yellow]No verificados (carpetas inaccesibles): '
             f'{len(resultado["no_verificados"])}[/]')
    if resultado['carpetas_no_verificables']:
        imprimir('[yellow]Algunas carpetas no pudieron leerse: no se contaron '
                 'sus vídeos como ausentes.[/]')
    for formato, destino in destinos.items():
        imprimir(f'[green]Comprobación {formato.upper()}: {rich_escape(str(destino))}[/]')


def eliminar_faltantes_interactivo(db_defecto: str) -> str:
    """Elimina huellas marcadas como faltantes en un reporte y guarda reporte JSON, TXT y CSV."""
    from core.database import _usa_neon, cerrar, conectar, eliminar_por_ruta, info_dispositivo_actual, describir_conexion
    if _usa_neon():
        db = 'neon_cloud'
    else:
        db = elegir_archivo_reciente(
            'Base de datos SQLite',
            ['*.db'],
            ['respaldos/db'],
            defecto=db_defecto,
        )
    archivo_json = elegir_archivo_reciente(
        'Reporte JSON de verificación bidireccional',
        ['verificacion_*.json', '*.json'],
        ['verificaciones/json'],
    )
    if not archivo_json or not Path(archivo_json).is_file():
        imprimir('[yellow]No se seleccionó un archivo válido.[/]')
        return db_defecto

    with Path(archivo_json).open(encoding='utf-8-sig') as f:
        datos = json.load(f)

    faltantes = datos.get('faltantes', [])
    if not faltantes:
        imprimir('[yellow]El reporte no contiene vídeos marcados como "faltantes" '
                 '(ausentes de su ruta).[/]')
        return db_defecto

    imprimir(f'\n[cyan]Se encontraron {len(faltantes)} vídeos faltantes en el reporte.[/]')
    if not pedir_si_no('¿Estás seguro de que deseas eliminar permanentemente estas huellas de la base de datos?', False):
        imprimir('[yellow]Operación cancelada.[/]')
        return db_defecto

    texto = pedir_texto('Escribe ELIMINAR FALTANTES para confirmar', obligatorio=True)
    if texto != 'ELIMINAR FALTANTES':
        imprimir('[yellow]Confirmación incorrecta. Cancelado.[/]')
        return db_defecto

    conn = conectar(db)
    eliminados = 0
    errores = 0
    registros_log = []
    try:
        for item in faltantes:
            ruta = item.get('ruta')
            disp_id = item.get('dispositivo_id')
            nombre = item.get('nombre') or (Path(ruta).name if ruta else '')
            sha = item.get('sha256', '')
            tam = item.get('tamanio_bytes', '')
            if ruta:
                if eliminar_por_ruta(conn, ruta, disp_id):
                    eliminados += 1
                    registros_log.append({
                        'ruta': ruta, 'nombre': nombre, 'estado': 'eliminado',
                        'dispositivo_id': disp_id, 'sha256': sha, 'tamanio_bytes': tam,
                    })
                else:
                    errores += 1
                    registros_log.append({
                        'ruta': ruta, 'nombre': nombre, 'estado': 'error',
                        'dispositivo_id': disp_id, 'sha256': sha, 'tamanio_bytes': tam,
                    })
    finally:
        cerrar(conn)

    if registros_log:
        carpeta_out = _carpeta_reportes() / 'depuraciones'
        for ext in ('json', 'txt', 'csv'):
            (carpeta_out / ext).mkdir(parents=True, exist_ok=True)
        sello = id_utc6()
        fecha = fmt_ahora_utc6()
        disp = info_dispositivo_actual()

        resumen = {
            'fecha_utc6': fecha,
            'operacion': 'Eliminación de faltantes desde reporte de verificación',
            'reporte_origen': str(archivo_json),
            'base_datos': str(db),
            'conexion': describir_conexion(),
            'dispositivo_ejecutor': disp,
            'eliminados': eliminados,
            'errores': errores,
            'registros': registros_log,
        }
        ruta_rep_json = carpeta_out / 'json' / f'eliminacion_faltantes_{sello}.json'
        ruta_rep_txt = carpeta_out / 'txt' / f'eliminacion_faltantes_{sello}.txt'
        ruta_rep_csv = carpeta_out / 'csv' / f'eliminacion_faltantes_{sello}.csv'

        ruta_rep_json.write_text(json.dumps(resumen, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

        with ruta_rep_txt.open('w', encoding='utf-8') as f:
            f.write(f'ELIMINACIÓN DE FALTANTES — {fecha}\n')
            f.write(f'Dispositivo ejecutor: {disp["dispositivo"]} (ID {disp["dispositivo_id"]})\n')
            f.write(f'Reporte de origen   : {archivo_json}\n')
            f.write(f'Base de datos       : {db}\n')
            f.write(f'Eliminados          : {eliminados} | Errores: {errores}\n\n')
            for r in registros_log:
                f.write(f"{r['estado'].upper()} | {r['ruta']} (ID {r.get('dispositivo_id', 'N/D')})\n")

        with ruta_rep_csv.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['estado', 'ruta', 'nombre', 'dispositivo_id', 'sha256', 'tamanio_bytes'])
            writer.writeheader()
            for r in registros_log:
                writer.writerow(r)

    imprimir(f'[spring_green2]Se eliminaron {eliminados} huellas de la base de datos.[/]')
    if errores > 0:
        imprimir(f'[yellow]No se pudieron encontrar/eliminar {errores} huellas '
                 f'(quizá ya estaban borradas).[/]')
    if registros_log:
        imprimir(f'[cyan]Reporte JSON: {rich_escape(str(ruta_rep_json))}[/]')
        imprimir(f'[cyan]Reporte TXT : {rich_escape(str(ruta_rep_txt))}[/]')
        imprimir(f'[cyan]Reporte CSV : {rich_escape(str(ruta_rep_csv))}[/]')
    return db


def buscar_ubicacion_interactivo(db: str) -> None:
    """Busca registros cuyo archivo ya no está en su ruta, en otras raíces de almacenamiento."""
    from collections import Counter
    from core import inventario, ubicacion
    from core.database import cerrar, conectar, info_dispositivo_actual

    actual = info_dispositivo_actual()
    dispositivo_id = actual['dispositivo_id']
    excluidas = inventario.cargar_exclusiones(DIR_SCRIPT, dispositivo_id)

    conn = conectar(db)
    try:
        faltantes = ubicacion.faltantes_desde_base(conn, dispositivo_id, excluidas)
    finally:
        cerrar(conn)

    if not faltantes:
        imprimir('[green]No hay registros cuyo archivo haya dejado de estar en su ruta.[/]')
        return

    imprimir(f'[yellow]Registros cuyo archivo ya no está en su ruta: {len(faltantes)}[/]')
    por_carpeta = Counter(os.path.dirname(f['ruta']) for f in faltantes)
    for carpeta, cantidad in por_carpeta.most_common(15):
        imprimir(f'  {rich_escape(carpeta)} ({cantidad})')
    if len(por_carpeta) > 15:
        imprimir(f'  ... y {len(por_carpeta) - 15} carpeta(s) más')

    avisos_raices: List[str] = []
    detectadas = ubicacion.raices_almacenamiento(avisos_raices)
    if detectadas:
        imprimir('[cyan]Raíces detectadas (se buscan automáticamente):[/]')
        for raiz in detectadas:
            imprimir(f'  {rich_escape(raiz)}')
    else:
        imprimir('[yellow]No se detectaron raíces automáticamente.[/]')
    for aviso in avisos_raices:
        imprimir(f'[yellow]  ⚠ {rich_escape(aviso)}[/]')

    extra: List[str] = []
    while pedir_si_no(
        '¿Añadir otra raíz de almacenamiento para buscar? '
        '(ej: D:\\ en Windows, /mnt/disco en Linux o /storage/9F36-61B5 en Android; NO una subcarpeta)',
        False,
    ):
        extra.append(pedir_ruta(
            'Raíz de almacenamiento (ej: D:\\ o /storage/9F36-61B5)',
            existente=True, directorio=True,
        ))

    imprimir('[cyan]Buscando la misma ruta relativa en otras raíces...[/]')
    imprimir('[grey70]Solo se calcula SHA256 de los archivos cuyo tamaño coincide.[/]')

    def _progreso_ubicacion(fase: str, hechos: int, total: int) -> None:
        if total:
            imprimir(f'[cyan]{rich_escape(fase)}[/] [grey37]({hechos}/{total})[/]')

    def _progreso_bytes(ruta: str, leidos: int, total: int) -> None:
        if total:
            pct = leidos * 100 / total
            sys.stdout.write(
                f'\r\033[2K[grey37]  SHA256 {rich_escape(Path(ruta).name[:40])}: '
                f'{pct:5.1f}%[/]'
            )
            sys.stdout.flush()
            if leidos >= total:
                sys.stdout.write('\n')
                sys.stdout.flush()

    try:
        resultado = ubicacion.buscar_reubicaciones(
            faltantes, extra, excluidas,
            progreso=_progreso_ubicacion,
            progreso_bytes=_progreso_bytes,
        )
    except ubicacion.InterrupcionBusqueda:
        imprimir('[yellow]Búsqueda cancelada.[/]')
        return

    if resultado.get('avisos'):
        imprimir(f'[yellow]Avisos ({len(resultado["avisos"])}):[/]')
        for aviso in resultado['avisos'][:10]:
            imprimir(f'  [yellow]• {rich_escape(aviso)}[/]')
        if len(resultado['avisos']) > 10:
            imprimir(f'  [grey37]... y {len(resultado["avisos"]) - 10} más (ver el TXT)[/]')

    destinos = ubicacion.reportar_ubicaciones(_carpeta_reportes() / 'ubicaciones', resultado)
    imprimir(f'[green]Coincidencias confirmadas por SHA256: {len(resultado["coincidencias"])}[/]')
    imprimir(f'[yellow]Probables o ambiguas (no se aplican solas): {len(resultado["probables"])}[/]')
    imprimir(f'[yellow]Sin coincidencia: {len(resultado["sin_coincidencia"])}[/]')
    if resultado['no_legibles']:
        imprimir(f'[yellow]Rutas no legibles: {len(resultado["no_legibles"])} (revisa el TXT)[/]')
    for formato, destino in destinos.items():
        imprimir(f'[green]Ubicación {formato.upper()}: {rich_escape(str(destino))}[/]')


def aplicar_ubicacion_interactivo(db: str) -> None:
    """Aplica al inventario las coincidencias confirmadas de un reporte de ubicación."""
    from core import ubicacion
    from core.database import cerrar, conectar, info_dispositivo_actual
    actual = info_dispositivo_actual()

    archivo = elegir_archivo_reciente(
        'Reporte JSON de comprobación de ubicación',
        ['ubicaciones_*.json', '*.json'],
        ['ubicaciones/json'],
    )
    if not archivo or not Path(archivo).is_file():
        imprimir('[yellow]No se seleccionó un reporte válido.[/]')
        return

    coincidencias, rechazadas = ubicacion.cargar_coincidencias_con_errores(archivo)
    if rechazadas:
        imprimir(f'[yellow]Coincidencias descartadas por validación: {len(rechazadas)}[/]')
        for r in rechazadas[:10]:
            imprimir(f'  [yellow]• {rich_escape(r.get("ruta_nueva", ""))}: '
                 f'{rich_escape(r.get("error", ""))}[/]')
    if not coincidencias:
        imprimir('[yellow]El reporte no contiene coincidencias confirmadas y válidas para aplicar.[/]')
        return

    imprimir(f'\n[cyan]Se cambiará la ruta de {len(coincidencias)} vídeo(s) en la base.[/]')
    for c in coincidencias[:20]:
        imprimir(f'  {rich_escape(c["ruta_anterior"])}')
        imprimir(f'    -> {rich_escape(c["ruta_nueva"])}')
    if len(coincidencias) > 20:
        imprimir(f'  ... y {len(coincidencias) - 20} más (ver el reporte)')
    imprimir('[grey70]Solo cambia la ruta en la base. No mueve ni copia archivos. '
             'Se respalda cada cambio antes de aplicarlo.[/]')

    if not pedir_si_no('¿Aplicar estos cambios de ruta?', False):
        imprimir('[yellow]Operación cancelada.[/]')
        return
    texto = pedir_texto('Escribe APLICAR UBICACION para confirmar', obligatorio=True)
    if texto != 'APLICAR UBICACION':
        imprimir('[yellow]Confirmación incorrecta. Cancelado.[/]')
        return

    conn = conectar(db)
    try:
        resultado = ubicacion.aplicar_reubicaciones(
            conn, actual['dispositivo_id'], actual['dispositivo'],
            coincidencias, _carpeta_reportes() / 'cambios_rutas')
    finally:
        cerrar(conn)

    imprimir(f'[spring_green2]Rutas actualizadas: {len(resultado["actualizados"] if "actualizados" in resultado else resultado["aplicadas"])}[/]')
    if resultado['fallidas']:
        imprimir(f'[yellow]No se pudieron aplicar: {len(resultado["fallidas"])}[/]')
        for f in resultado['fallidas'][:10]:
            imprimir(f'  {rich_escape(f["ruta_anterior"])}: {rich_escape(f["error"])}')
    imprimir('[grey70]Respaldos de cada cambio en cambios_rutas (json, txt y csv).[/]')


def eliminar_desde_reporte_eliminaciones_interactivo(db_defecto: str) -> None:
    """Elimina huellas de la base de datos a partir de un reporte de eliminaciones del submenú 3 (json/jsonl) y guarda reporte."""
    from core.database import conectar, cerrar, eliminar_por_ruta, _usa_neon, info_dispositivo_actual, describir_conexion

    archivo = elegir_archivo_reciente(
        'Reporte de eliminaciones del submenú 3 (eliminaciones_*.jsonl o .json)',
        ['eliminaciones_*.jsonl', 'eliminaciones_*.json', '*.jsonl', '*.json'],
        ['movimientos/jsonl', 'movimientos/json'],
    )
    if not archivo or not Path(archivo).is_file():
        imprimir('[yellow]No se seleccionó un archivo de reporte válido.[/]')
        return

    disp_actual = info_dispositivo_actual()
    disp_id = disp_actual['dispositivo_id']

    path_obj = Path(archivo)
    registros_elim = []

    try:
        if path_obj.suffix.lower() == '.jsonl':
            with path_obj.open('r', encoding='utf-8') as f:
                for linea in f:
                    if linea.strip():
                        try:
                            reg = json.loads(linea)
                            registros_elim.append(reg)
                        except Exception:
                            pass
        else:
            with path_obj.open('r', encoding='utf-8') as f:
                datos = json.load(f)
                if isinstance(datos, dict):
                    registros_elim = datos.get('registros') or datos.get('resultados') or []
                elif isinstance(datos, list):
                    registros_elim = datos
    except Exception as exc:
        imprimir(f'[red][ERROR] No se pudo leer el reporte: {exc}[/]')
        return

    rutas_a_borrar = []
    for reg in registros_elim:
        if isinstance(reg, dict):
            st = reg.get('estado')
            if st in ('eliminado', 'exito', None):
                ruta = reg.get('origen') or reg.get('ruta')
                reg_disp = reg.get('dispositivo_id')
                sha = reg.get('sha256_comprobado') or reg.get('sha256_esperado') or reg.get('sha256') or ''
                if ruta:
                    rutas_a_borrar.append((ruta, reg_disp, sha))

    if not rutas_a_borrar:
        imprimir('[yellow]El reporte no contiene registros de vídeos eliminados válidos.[/]')
        return

    imprimir(f'\n[cyan]Se eliminarán {len(rutas_a_borrar)} registros de la base de datos basándose en el reporte.[/]')
    for r, _, _ in rutas_a_borrar[:15]:
        imprimir(f'  • {rich_escape(r)}')
    if len(rutas_a_borrar) > 15:
        imprimir(f'  ... y {len(rutas_a_borrar) - 15} más.')

    if not pedir_si_no('¿Confirmas eliminar estos registros de la base de datos?', False):
        imprimir('[yellow]Operación cancelada.[/]')
        return

    if _usa_neon():
        db = 'neon_cloud'
    else:
        db = elegir_archivo_reciente(
            'Base de datos SQLite donde aplicar la depuración',
            ['*.db'],
            ['respaldos/db'],
            defecto=db_defecto,
        )

    conn = conectar(db)
    borrados = 0
    no_encontrados = 0
    registros_log = []
    try:
        for r, reg_disp, sha in rutas_a_borrar:
            did_obj = reg_disp or disp_id
            if eliminar_por_ruta(conn, r, did_obj):
                borrados += 1
                registros_log.append({
                    'estado': 'eliminado',
                    'ruta': r,
                    'nombre': Path(r).name,
                    'dispositivo_id': did_obj,
                    'sha256': sha,
                })
            else:
                no_encontrados += 1
                registros_log.append({
                    'estado': 'no_encontrado',
                    'ruta': r,
                    'nombre': Path(r).name,
                    'dispositivo_id': did_obj,
                    'sha256': sha,
                })
    finally:
        cerrar(conn)

    carpeta_out = _carpeta_reportes() / 'depuraciones'
    for ext in ('json', 'txt', 'csv'):
        (carpeta_out / ext).mkdir(parents=True, exist_ok=True)
    sello = id_utc6()
    fecha = fmt_ahora_utc6()

    resumen = {
        'fecha_utc6': fecha,
        'operacion': 'Depuración desde reporte de eliminaciones del submenú 3',
        'reporte_origen': str(archivo),
        'base_datos': str(db),
        'conexion': describir_conexion(),
        'dispositivo_ejecutor': disp_actual,
        'eliminados': borrados,
        'no_encontrados': no_encontrados,
        'registros': registros_log,
    }
    ruta_rep_json = carpeta_out / 'json' / f'depuracion_reporte_elim_{sello}.json'
    ruta_rep_txt = carpeta_out / 'txt' / f'depuracion_reporte_elim_{sello}.txt'
    ruta_rep_csv = carpeta_out / 'csv' / f'depuracion_reporte_elim_{sello}.csv'

    ruta_rep_json.write_text(json.dumps(resumen, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with ruta_rep_txt.open('w', encoding='utf-8') as f:
        f.write(f'DEPURACIÓN DESDE REPORTE DE ELIMINACIONES — {fecha}\n')
        f.write(f'Dispositivo ejecutor: {disp_actual["dispositivo"]} (ID {disp_id})\n')
        f.write(f'Reporte de origen   : {archivo}\n')
        f.write(f'Base de datos       : {db}\n')
        f.write(f'Eliminados de BD    : {borrados} | No encontrados: {no_encontrados}\n\n')
        for item in registros_log:
            f.write(f"{item['estado'].upper()} | {item['ruta']} (ID {item['dispositivo_id']})\n")

    with ruta_rep_csv.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['estado', 'ruta', 'nombre', 'dispositivo_id', 'sha256'])
        writer.writeheader()
        for item in registros_log:
            writer.writerow(item)

    imprimir(f'[spring_green2]✓ Depuración completada: {borrados} registros eliminados de la base de datos.[/]')
    if no_encontrados:
        imprimir(f'[yellow]No encontrados en la base (ya eliminados previamente): {no_encontrados}[/]')
    imprimir(f'[cyan]Reporte JSON: {rich_escape(str(ruta_rep_json))}[/]')
    imprimir(f'[cyan]Reporte TXT : {rich_escape(str(ruta_rep_txt))}[/]')
    imprimir(f'[cyan]Reporte CSV : {rich_escape(str(ruta_rep_csv))}[/]')