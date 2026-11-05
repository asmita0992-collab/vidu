from __future__ import annotations
import argparse
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
from core.flujo_escaneo import (gestionar_exclusiones)
from core.utilidades_interactivas import (aplicar_ubicacion_interactivo, buscar_ubicacion_interactivo, cambiar_rutas_interactivo, completar_metadatos_interactivo, comprobar_disco_bd_interactivo, depurar_carpeta_por_dispositivo, eliminar_faltantes_interactivo)


def _cabecera_submenu(titulo: str) -> None:
    from core.database import describir_conexion, info_dispositivo_actual
    info = info_dispositivo_actual()
    if CONSOLA is not None and Panel is not None:
        CONSOLA.print()
        CONSOLA.print(Panel.fit(
            f'[bold bright_white]{titulo}[/]\n'
            f'[deep_sky_blue1]Dispositivo:[/] [spring_green2]{info["dispositivo"]}[/]  [grey37](ID {info["dispositivo_id"]})[/]\n'
            f'[deep_sky_blue1]Plataforma :[/] [turquoise2]{info["plataforma"]}[/] ({info["maquina"]})\n'
            f'[deep_sky_blue1]Conexión   :[/] [gold1]{describir_conexion()}[/]\n'
            f'[deep_sky_blue1]Telegram   :[/] {"[spring_green2]activo[/]" if telegram_configurado() else "[orange3]desactivado[/]"}',
            title='[bold plum1]VIDUP[/]',
            border_style='bright_cyan',
        ))
    else:
        print(f'\n{"═" * 72}')
        print(f'{NEGRITA}{titulo}{NC}')
        print(f'{"═" * 72}')
        print(f'Dispositivo: {info["dispositivo"]} (ID {info["dispositivo_id"]})')
        print(f'Plataforma : {info["plataforma"]} ({info["maquina"]})')
        print(f'Conexión   : {describir_conexion()}')
        print(f'Telegram   : {"activo" if telegram_configurado() else "desactivado"}')


def _pedir_opcion(mensaje: str, opciones_validas: Sequence[str], defecto: str = '') -> str:
    intentos = 0
    while True:
        try:
            valor = pedir_texto(mensaje, defecto, permitir_escape=False).strip().lower()
        except RegresarAlMenu:
            return '00'
        if valor in opciones_validas:
            return valor
        intentos += 1
        if intentos >= _MAX_INTENTOS:
            return '00'
        imprimir(f'[yellow]Elige una opción válida: {", ".join(opciones_validas)}.[/]')


def _seleccionar_base_lectura(db_defecto: str, titulo: str = 'Base de datos SQLite') -> str:
    from core.database import _usa_neon
    if _usa_neon():
        return 'neon_cloud'
    return elegir_archivo_reciente(titulo, ['*.db'], ['respaldos/db'], defecto=db_defecto)


def submenu_estadisticas(db_defecto: str) -> str:
    """Menú 7: estadísticas, exportación, exclusiones y metadatos."""
    from core.database import cerrar, conectar, estadisticas, estadisticas_por_dispositivo, obtener_informe_huellas_faltantes
    from core.database import info_dispositivo_actual
    from core.reporter import exportar_reporte_huellas_faltantes
    from core import inventario
    while True:
        _cabecera_submenu('Estadísticas e inventario')
        imprimir('  [gold1]1.[/] Mostrar estadísticas generales, por dispositivo y ruta')
        imprimir('  [gold1]2.[/] Exportar estadísticas a TXT, JSON y CSV')
        imprimir('  [gold1]3.[/] Generar reporte de vídeos faltantes de huellas (TXT, JSON y CSV)')
        imprimir('  [gold1]4.[/] Gestionar carpetas excluidas del escaneo')
        imprimir('  [gold1]5.[/] Completar metadatos faltantes (FFprobe)')
        imprimir('  [gold1]6.[/] Cambiar la ruta de un vídeo o carpeta en la base')
        imprimir('  [gold1]00.[/] Volver al menú principal')
        opcion = _pedir_opcion('¿Qué quieres hacer? (1-6 / 00)',
                               ('1', '2', '3', '4', '5', '6', '00', '0'), '1')
        if opcion in ('00', '0'):
            return db_defecto
        if opcion == '4':
            gestionar_exclusiones()
            continue
        db = _seleccionar_base_lectura(db_defecto, 'Base de datos para el inventario')
        db_defecto = db
        if opcion == '5':
            completar_metadatos_interactivo(db)
        elif opcion == '6':
            cambiar_rutas_interactivo(db)
        elif opcion == '1':
            cmd_stats(argparse.Namespace(db=db))
        elif opcion == '2':
            conn = conectar(db)
            try:
                generales = estadisticas(conn)
                dispositivos = estadisticas_por_dispositivo(conn)
            finally:
                cerrar(conn)
            if generales['total_videos'] != sum(d['total_videos'] for d in dispositivos):
                raise RuntimeError('Totales inconsistentes; no se generó el reporte.')
            destinos = inventario.reportar_estadisticas(
                _carpeta_reportes() / 'estadisticas', generales, dispositivos, db)
            for formato, destino in destinos.items():
                imprimir(f'[green]Estadísticas {formato.upper()}: {rich_escape(str(destino))}[/]')
        elif opcion == '3':
            conn = conectar(db)
            try:
                informe = obtener_informe_huellas_faltantes(conn)
            finally:
                cerrar(conn)
            identificador = _id_utc6()
            destinos = exportar_reporte_huellas_faltantes(
                informe, _carpeta_reportes() / 'estadisticas', identificador, db)
            imprimir(f'\n[spring_green2]✓ Reporte de vídeos faltantes de huellas generado ({informe["total_videos_faltantes_huellas"]} de {informe["total_videos_analizados"]} vídeos requieren huellas):[/]')
            for formato, destino in destinos.items():
                imprimir(f'  [green]• {formato.upper()}: {rich_escape(str(destino))}[/]')


def submenu_mover(db_defecto: str) -> str:
    from core.mover import (mover_interactivo, mover_desde_sha256,
                            eliminar_desde_comparativa, eliminar_desde_sha256, vaciar_carpeta_eliminar)
    from core.sha256 import ultimo_reporte_comparacion as _ult

    while True:
        _cabecera_submenu('Mover vídeos')
        imprimir('\n  [gold1]1.[/] Mover vídeos de una comparativa (visual)')
        imprimir('  [gold1]2.[/] Mover nuevos/ya registrados desde JSON SHA256')
        imprimir('  [bold red]3.[/] Eliminar duplicados EXACTOS locales desde comparativa')
        imprimir('  [bold red]4.[/] Eliminar coincidencias SHA256 locales')
        imprimir('  [bold red]5.[/] Vaciar carpeta de revisión/eliminar (borrado físico y de BD)')
        imprimir('  [gold1]00.[/] Volver al menú principal\n')
        opcion = _pedir_opcion('¿Qué quieres hacer? (1-5 / 00)', ('1', '2', '3', '4', '5', '00', '0'), '1')
        if opcion in ('00', '0'):
            return db_defecto

        try:
            if opcion == '1':
                ruta_reporte = elegir_archivo_reciente(
                    'JSON de comparación visual',
                    ['comparacion_*.json'],
                    ['comparativas/json'],
                )
                destino_def = str(_carpeta_salida_videos())
                destino = pedir_ruta('Directorio de destino para los vídeos', destino_def)
                mover_interactivo(ruta_reporte, destino, _carpeta_reportes() / 'movimientos', db_defecto)
            elif opcion == '2':
                sugerido = _ult(DIR_SCRIPT) or ''
                if sugerido:
                    ruta_json = elegir_archivo_reciente(
                        'JSON SHA256 de comparación',
                        ['sha256_*.json'],
                        ['sha256/json'],
                        defecto=sugerido,
                    )
                else:
                    ruta_json = pedir_ruta('JSON SHA256 de comparación', existente=True)

                imprimir('\n[deep_sky_blue1]¿Qué vídeos quieres mover?[/]')
                imprimir('  1. Solo los nuevos (no están en ninguna base)')
                imprimir('  2. Solo los ya registrados aquí')
                imprimir('  3. Ambos (nuevos + ya registrados aquí)')
                imprimir('  4. Solo coincidencias (ya copiados en otros dispositivos)')
                imprimir('  0. Cancelar')
                grupos = None
                while grupos is None:
                    eleccion = _pedir_opcion('Opción (0-4)', ('0', '1', '2', '3', '4'), '3')
                    if eleccion == '0':
                        grupos = 'cancelar'
                    elif eleccion == '1':
                        grupos = ('nuevos',)
                    elif eleccion == '2':
                        grupos = ('ya_registrados_aqui',)
                    elif eleccion == '3':
                        grupos = ('nuevos', 'ya_registrados_aqui')
                    elif eleccion == '4':
                        grupos = ('coincidencias',)
                if grupos == 'cancelar':
                    continue
                destino_def = str(_carpeta_salida_videos())
                destino = pedir_ruta('Directorio de destino para los vídeos', destino_def)
                Path(destino).mkdir(parents=True, exist_ok=True)
                mover_desde_sha256(ruta_json, destino, _carpeta_reportes() / 'movimientos', grupos=grupos, db_path=db_defecto)
            elif opcion == '3':
                ruta = elegir_archivo_reciente(
                    'Comparativa visual con duplicados exactos',
                    ['comparacion_*.json'], ['comparativas/json'])
                eliminar_desde_comparativa(ruta, _carpeta_reportes() / 'movimientos')
            elif opcion == '4':
                ruta = elegir_archivo_reciente(
                    'Comparativa SHA256 con coincidencias',
                    ['sha256_comparar*.json'], ['sha256/json'])
                eliminar_desde_sha256(ruta, _carpeta_reportes() / 'movimientos')
            elif opcion == '5':
                destino_def = str(_carpeta_salida_videos() / 'eliminar')
                carpeta = pedir_ruta('Carpeta que contiene los vídeos a borrar', destino_def, existente=True, directorio=True)
                vaciar_carpeta_eliminar(carpeta, db_defecto)
        except RegresarAlMenu:
            imprimir('[yellow]Operación cancelada.[/]')
        except Exception as exc:
            imprimir(f'[red][ERROR] {exc}[/]')
    return db_defecto


def submenu_respaldos(db_defecto: str) -> str:
    from core.database import _usa_neon

    while True:
        _cabecera_submenu('Respaldos')
        imprimir('\n  [gold1]1.[/] Exportar base de datos (JSON y TXT)')
        imprimir('  [gold1]2.[/] Importar base de datos JSON')
        imprimir('  [gold1]3.[/] Descargar respaldo .db desde Neon')
        imprimir('  [bold red]4.[/] REEMPLAZAR Neon por una SQLite local (respaldo previo)')
        imprimir('  [bold red]5.[/] Combinar base de datos externa (.db o .json) con la actual')
        imprimir('  [gold1]00.[/] Volver al menú principal\n')
        opcion = _pedir_opcion('¿Qué quieres hacer? (1-5 / 00)', ('1', '2', '3', '4', '5', '00', '0'), '1')
        if opcion in ('00', '0'):
            return db_defecto

        try:
            if opcion == '1':
                if _usa_neon():
                    db = 'neon_cloud'
                else:
                    db = elegir_archivo_reciente(
                        'Base de datos SQLite a exportar',
                        ['*.db'],
                        ['respaldos/db'],
                        defecto=db_defecto,
                    )
                nombre = pedir_texto('Nombre de la exportación', f'base_de_datos_{_id_utc6()}')
                destino = _carpeta_reportes() / 'exportaciones' / 'json' / (Path(nombre).stem + '.json')
                args = argparse.Namespace(db=db, salida=str(destino))
                cmd_exportar(args)
                db_defecto = db
            elif opcion == '2':
                archivo = elegir_archivo_reciente(
                    'JSON de huellas exportadas',
                    ['base_de_datos_*.json', '*.json'],
                    ['exportaciones/json'],
                )
                if _usa_neon():
                    db = 'neon_cloud'
                else:
                    db = elegir_archivo_reciente(
                        'Base de datos SQLite destino (o nueva ruta si no existe)',
                        ['*.db'],
                        ['respaldos/db'],
                        defecto=db_defecto,
                    )
                forzar = pedir_si_no('¿Sustituir registros que ya existen?', False)
                args = argparse.Namespace(db=db, archivo=archivo, forzar=forzar)
                cmd_importar(args)
                db_defecto = db
            elif opcion == '3':
                if not _usa_neon():
                    imprimir('[yellow][AVISO] No hay conexión Neon activa; nada que descargar.[/]')
                    continue
                nombre = pedir_texto('Nombre del archivo de respaldo',
                                     f'respaldo_neon_{_id_utc6()}.db')
                destino = _carpeta_reportes() / 'respaldos' / 'db' / nombre
                args = argparse.Namespace(db='neon_cloud', salida=str(destino))
                cmd_descargar_neon(args)
            elif opcion == '4':
                from core.database import reemplazar_neon_desde_sqlite, estado_conexion
                if estado_conexion()['modo'] != 'neon':
                    imprimir('[red]Neon no está en línea. Sustitución deshabilitada.[/]')
                    continue
                origen = elegir_archivo_reciente(
                    'SQLite local con la que se desea sustituir Neon',
                    ['*.db'], ['respaldos/db'], defecto=db_defecto)
                imprimir('[bold red]Esta operación sustituirá TODOS los vídeos y huellas en Neon.[/]')
                imprimir('[yellow]Primero se respaldará Neon en SQLite. La sustitución es transaccional.[/]')
                imprimir(f'[cyan]Fuente: {origen}[/]')
                if not pedir_si_no('¿Es correcta la base seleccionada?', False):
                    continue
                texto = pedir_texto('Escribe REEMPLAZAR NEON para confirmar')
                if texto != 'REEMPLAZAR NEON':
                    imprimir('[yellow]Operación cancelada.[/]')
                    continue
                resultado = reemplazar_neon_desde_sqlite(
                    origen, str(_carpeta_reportes() / 'respaldos' / 'db'))
                imprimir(f'[bold green]Neon sustituida: {resultado["videos"]} vídeos y {resultado["frames"]} frames.[/]')
                imprimir(f'[cyan]Respaldo previo: {resultado["respaldo_previo"]}[/]')
            elif opcion == '5':
                from core.database import conectar, cerrar, obtener_todos, existe_ruta, guardar_fingerprint, importar_json
                
                archivo_ext = pedir_ruta('Ruta de la base de datos externa a combinar (.db o .json)', existente=True)
                es_json = Path(archivo_ext).suffix.lower() == '.json'
                
                if _usa_neon():
                    db_destino = 'neon_cloud'
                else:
                    db_destino = elegir_archivo_reciente(
                        'Base de datos SQLite destino (actual)',
                        ['*.db'],
                        ['respaldos/db'],
                        defecto=db_defecto,
                    )
                
                imprimir(f'\n[cyan]Origen : {archivo_ext}[/]')
                imprimir(f'[cyan]Destino: {db_destino}[/]')
                forzar = pedir_si_no('¿Sobrescribir registros si ya existen en la base actual?', False)
                
                if not pedir_si_no('¿Comenzar la combinación?', False):
                    continue
                    
                conn_dest = conectar(db_destino)
                try:
                    if es_json:
                        conteo = importar_json(conn_dest, archivo_ext, omitir_existentes=not forzar)
                        imprimir(f'[spring_green2]Importados: {conteo["importados"]}; omitidos: {conteo["omitidos"]}; errores: {conteo["errores"]}[/]')
                    else:
                        conn_ext = conectar(archivo_ext)
                        try:
                            videos_ext = obtener_todos(conn_ext)
                        finally:
                            cerrar(conn_ext)
                            
                        importados = 0
                        omitidos = 0
                        errores = 0
                        
                        for fp in videos_ext:
                            if not forzar and existe_ruta(conn_dest, fp.ruta, fp.dispositivo_id):
                                omitidos += 1
                                continue
                            
                            vid_id = guardar_fingerprint(
                                conn_dest, fp, 
                                dispositivo=fp.dispositivo, 
                                dispositivo_id=fp.dispositivo_id
                            )
                            if vid_id is not None:
                                importados += 1
                            else:
                                errores += 1
                        
                        imprimir(f'[spring_green2]Combinados: {importados}; omitidos: {omitidos}; errores: {errores}[/]')
                finally:
                    cerrar(conn_dest)
                
                if db_destino != 'neon_cloud':
                    db_defecto = db_destino
                    
        except RegresarAlMenu:
            imprimir('[yellow]Operación cancelada.[/]')
        except Exception as exc:
            imprimir(f'[red][ERROR] {exc}[/]')
    return db_defecto


def submenu_verificacion(db_defecto: str) -> str:
    """Menú 8: comprobación de disco, ubicación de archivos y reparación de rutas."""
    while True:
        _cabecera_submenu('Verificación y ubicación de archivos')
        imprimir('  [gold1]1.[/] Comprobar disco ↔ base de datos (ambas direcciones)')
        imprimir('  [gold1]2.[/] Buscar ubicación de archivos faltantes (interna y tarjeta)')
        imprimir('  [bold red]3.[/] Eliminar huellas \"faltantes\" desde reporte de verificación')
        imprimir('  [gold1]4.[/] Cambiar ruta según reporte de ubicación')
        imprimir('  [bold red]5.[/] Eliminar huellas desde reporte de eliminaciones del submenú 3')
        imprimir('  [gold1]00.[/] Volver al menú principal')
        opcion = _pedir_opcion('¿Qué quieres hacer? (1-5 / 00)',
                               ('1', '2', '3', '4', '5', '00', '0'), '1')
        if opcion in ('00', '0'):
            return db_defecto
        try:
            if opcion == '3':
                db_defecto = eliminar_faltantes_interactivo(db_defecto)
            elif opcion == '5':
                from core.utilidades_interactivas import eliminar_desde_reporte_eliminaciones_interactivo
                eliminar_desde_reporte_eliminaciones_interactivo(db_defecto)
            else:
                db = _seleccionar_base_lectura(db_defecto, 'Base de datos para la verificación')
                db_defecto = db
                if opcion == '1':
                    comprobar_disco_bd_interactivo(db)
                elif opcion == '2':
                    buscar_ubicacion_interactivo(db)
                elif opcion == '4':
                    aplicar_ubicacion_interactivo(db)
        except RegresarAlMenu:
            imprimir('[yellow]Operación cancelada.[/]')
        except Exception as exc:
            imprimir(f'[red][ERROR] {exc}[/]')


def submenu_depurar(db_defecto: str) -> str:
    from core.database import _usa_neon, cerrar, conectar, estadisticas, info_dispositivo_actual

    while True:
        _cabecera_submenu('Depurar base de datos')
        imprimir('\n  [gold1]1.[/] Eliminar una huella por ruta')
        imprimir('  [gold1]2.[/] Eliminar huellas por ID de dispositivo')
        imprimir('  [gold1]3.[/] Reasignar huellas de un ID antiguo a este dispositivo')
        imprimir('  [gold1]4.[/] Eliminar registros por dispositivo y carpeta, con respaldo')
        #imprimir('  [bold red]5.[/] Eliminar huellas "faltantes" desde reporte de verificación')
        imprimir('  [gold1]00.[/] Volver al menú principal\n')
        opcion = _pedir_opcion('¿Qué quieres hacer? (1-4 / 00)', ('1', '2', '3', '4', '00', '0'), '1')
        if opcion in ('00', '0'):
            return db_defecto

        try:
            if opcion == '4':
                db = _seleccionar_base_lectura(db_defecto)
                depurar_carpeta_por_dispositivo(db)
                db_defecto = db
            elif opcion == '1':
                from core.database import eliminar_por_ruta
                if _usa_neon():
                    db = 'neon_cloud'
                else:
                    db = elegir_archivo_reciente(
                        'Base de datos SQLite',
                        ['*.db'],
                        ['respaldos/db'],
                        defecto=db_defecto,
                    )
                ruta = pedir_ruta('Ruta del vídeo cuya huella quieres eliminar')
                if pedir_si_no('¿Eliminar esa huella de la base de datos? El vídeo se conserva', False):
                    args = argparse.Namespace(db=db, ruta=ruta)
                    cmd_limpiar(args)
                db_defecto = db
            elif opcion == '2':
                from core.database import eliminar_por_dispositivo_id
                if _usa_neon():
                    db = 'neon_cloud'
                else:
                    db = elegir_archivo_reciente(
                        'Base de datos SQLite',
                        ['*.db'],
                        ['respaldos/db'],
                        defecto=db_defecto,
                    )
                conn = conectar(db)
                try:
                    datos = estadisticas(conn)
                finally:
                    cerrar(conn)
                imprimir('\n[cyan]Dispositivos registrados:[/]')
                for d in datos['dispositivos']:
                    imprimir(f'  • {d}', estilo='white')
                actual = info_dispositivo_actual()
                disp_id = pedir_texto('ID de dispositivo a eliminar', actual['dispositivo_id'])
                if disp_id and pedir_si_no(f'¿Eliminar todas las huellas del ID "{disp_id}"?', False):
                    args = argparse.Namespace(db=db, dispositivo_id=disp_id, listar=False)
                    cmd_eliminar_por_dispositivo(args)
                db_defecto = db
            elif opcion == '3':
                from core.database import reasignar_dispositivo_id
                if _usa_neon():
                    db = 'neon_cloud'
                else:
                    db = elegir_archivo_reciente(
                        'Base de datos SQLite',
                        ['*.db'],
                        ['respaldos/db'],
                        defecto=db_defecto,
                    )

                actual = info_dispositivo_actual()
                id_actual = actual['dispositivo_id']

                imprimir(f'\n[cyan]Dispositivo actual: {actual["dispositivo"]}[/]')
                imprimir(f'[cyan]ID actual        : {id_actual}[/]')
                imprimir('\n[white]A continuación se muestra la lista de IDs registrados en esta base.[/]')
                imprimir('[white]Escribe el ID antiguo que quieres reasignar.[/]')
                imprimir('[grey37](Los reportes anteriores con IDs como c57dd6358be7 suelen ser los afectados.)\n[/]')

                conn = conectar(db)
                try:
                    if _usa_neon():
                        import psycopg2.extras
                        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                            cur.execute("""
                                SELECT dispositivo_id, COUNT(*) AS cantidad,
                                       MIN(fecha_escaneo) AS primera,
                                       MAX(fecha_escaneo) AS ultima
                                FROM videos
                                WHERE dispositivo_id <> ''
                                GROUP BY dispositivo_id
                                ORDER BY cantidad DESC
                            """)
                            filas = [dict(r) for r in cur.fetchall()]
                    else:
                        filas = [dict(f) for f in conn.execute("""
                            SELECT dispositivo_id, COUNT(*) AS cantidad,
                                   MIN(fecha_escaneo) AS primera,
                                   MAX(fecha_escaneo) AS ultima
                            FROM videos
                            WHERE dispositivo_id <> ''
                            GROUP BY dispositivo_id
                            ORDER BY cantidad DESC
                        """).fetchall()]
                finally:
                    cerrar(conn)

                if not filas:
                    imprimir('[yellow]No hay IDs registrados en esta base.[/]')
                    continue

                if CONSOLA is not None and Table is not None and rich_box is not None:
                    tabla = Table(title='IDs registrados en la base',
                                  box=rich_box.ROUNDED, border_style='cyan')
                    tabla.add_column('ID dispositivo', style='bold')
                    tabla.add_column('Filas', justify='right')
                    tabla.add_column('Primera', style='grey37')
                    tabla.add_column('Última', style='grey37')
                    for f in filas:
                        marca = ' [green](actual)[/]' if f['dispositivo_id'] == id_actual else ''
                        tabla.add_row(
                            f'{f["dispositivo_id"]}{marca}',
                            str(f['cantidad']),
                            (f.get('primera') or '')[:19],
                            (f.get('ultima') or '')[:19],
                        )
                    CONSOLA.print(tabla)
                else:
                    print()
                    for f in filas:
                        marca = '  <-- actual' if f['dispositivo_id'] == id_actual else ''
                        print(f'  {f["dispositivo_id"]:<20} {f["cantidad"]:>6} filas{marca}')

                id_antiguo = pedir_texto('\nID antiguo a reasignar', '').strip()
                if not id_antiguo:
                    imprimir('[yellow]Cancelado.[/]')
                    continue

                if id_antiguo == id_actual:
                    imprimir('[yellow]El ID indicado ya es el actual. Nada que hacer.[/]')
                    continue

                conn = conectar(db)
                try:
                    if _usa_neon():
                        with conn.cursor() as cur:
                            cur.execute("SELECT COUNT(*) FROM videos WHERE dispositivo_id = %s",
                                        (id_antiguo,))
                            afectadas = int(cur.fetchone()[0] or 0)
                    else:
                        afectadas = int(conn.execute(
                            "SELECT COUNT(*) FROM videos WHERE dispositivo_id = ?",
                            (id_antiguo,)).fetchone()[0] or 0)
                finally:
                    cerrar(conn)

                if afectadas == 0:
                    imprimir(f'[yellow]No hay filas con el ID "{id_antiguo}".[/]')
                    continue

                imprimir(f'\n[yellow]Se reasignarán {afectadas} fila(s) del ID "{id_antiguo}" al ID actual "{id_actual}".[/]')
                imprimir('[grey37]La operación actualiza dispositivo_id y clave_unica. No elimina vídeos.[/]')

                if not pedir_si_no('¿Continuar?', False):
                    imprimir('[yellow]Operación cancelada.[/]')
                    continue

                conn = conectar(db)
                try:
                    actualizadas = reasignar_dispositivo_id(conn, id_antiguo, id_actual)
                finally:
                    cerrar(conn)

                imprimir(f'\n[spring_green2]Reasignadas: {actualizadas} fila(s).[/]' if actualizadas > 0 else '[yellow]No se reasignaron filas.[/]')
                db_defecto = db
        except RegresarAlMenu:
            imprimir('[yellow]Operación cancelada.[/]')
        except Exception as exc:
            imprimir(f'[red][ERROR] {exc}[/]')
    return db_defecto
