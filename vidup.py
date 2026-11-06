#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""VIDUP — detector de vídeos duplicados y fragmentos.

Ejecuta python vidup.py y elige una operación en el menú interactivo.
Los reportes se guardan junto al programa, dentro de reportes/.
"""

from __future__ import annotations

__version__ = "2.3.0"
__desc_herramienta__ = "Detector de vídeos duplicados y fragmentos"

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

# ============================================================================
# Sincronización de __main__ con sys.modules['vidup'] para módulos externos
# ============================================================================
sys.modules['vidup'] = sys.modules[__name__]

# ============================================================================
# .env: se carga SOLO desde la carpeta del propio vidup.py
# ============================================================================
DIR_SCRIPT: Path = Path(__file__).resolve().parent
ENV_PATH: Path = DIR_SCRIPT / ".env"

try:
    from dotenv import load_dotenv
    if ENV_PATH.is_file():
        load_dotenv(dotenv_path=ENV_PATH, override=False)
except ImportError:
    pass

# ============================================================================
# Importar módulos de utilidades desde core
# ============================================================================
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

# Aliases de compatibilidad (para código antiguo que usa _prefijo)
_ahora_utc6 = ahora_utc6
_fmt_ahora_utc6 = fmt_ahora_utc6
_id_utc6 = id_utc6
_notificar_inicio = notificar_inicio
_notificar_fin = notificar_fin
_notificar_error = notificar_error

# ============================================================================
# Importar ricos y tables desde rich si están disponibles
# ============================================================================
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
from core.flujo_comparacion import (comparar_interactivo)
from core.flujo_escaneo import (_tras_menu_sha256, escanear_interactivo, submenu_escaneo)
from core.menus import (submenu_depurar, submenu_estadisticas, submenu_mover, submenu_respaldos, submenu_verificacion)
from core.configuracion import submenu_configuracion


def menu_interactivo() -> int:
    from core.database import _usa_neon, info_dispositivo_actual, describir_conexion, conectar, cerrar

    _asegurar_estructura_reportes()

    if _usa_neon():
        try:
            conn = conectar('neon_cloud')
            cerrar(conn)
        except Exception:
            pass

    if _usa_neon():
        db_defecto = 'neon_cloud'
    else:
        db_defecto = _base_reciente_por_defecto()

    info_disp = info_dispositivo_actual()

    while True:
        if _usa_neon():
            db_defecto = 'neon_cloud'

        if CONSOLA is not None and Panel is not None and Table is not None and rich_box is not None:
            CONSOLA.print()
            CONSOLA.print(Panel.fit(
                f'[bold bright_white]VIDUP — Menú principal (v{__version__})[/]\n'
                f'[deep_sky_blue1]Dispositivo:[/] [spring_green2]{info_disp["dispositivo"]}[/]\n'
                f'[deep_sky_blue1]ID         :[/] [turquoise2]{info_disp["dispositivo_id"]}[/]\n'
                f'[deep_sky_blue1]Plataforma :[/] [gold1]{info_disp["plataforma"]}[/] ({info_disp["maquina"]})\n'
                f'[deep_sky_blue1]Conexión   :[/] [gold1]{describir_conexion()}[/]\n'
                f'[deep_sky_blue1]Telegram   :[/] {"[spring_green2]activo[/]" if telegram_configurado() else "[orange3]desactivado[/]"}',
                title=f'[bold plum1]VIDUP: versión {__version__}[/]',
                border_style='bright_cyan',
            ))
            tabla = Table(box=rich_box.ROUNDED, border_style='cyan', show_header=False)
            tabla.add_column('Nº', style='bold gold1', width=4)
            tabla.add_column('Opción', style='white')
            tabla.add_row('1', 'Escanear vídeos')
            tabla.add_row('2', 'Comparar una o varias bases de datos')
            tabla.add_row('3', 'Mover vídeos')
            tabla.add_row('4', 'Respaldos')
            tabla.add_row('5', 'Depurar base de datos')
            tabla.add_row('6', 'Operaciones SHA256 (varias carpetas en un catálogo)')
            tabla.add_row('7', 'Estadísticas, verificación bidireccional y reportes')
            tabla.add_row('8', 'Verificación y ubicación de archivos')
            tabla.add_row('9', 'Configuración')
            tabla.add_row('0', 'Salir')
            CONSOLA.print(tabla)
        else:
            print(f'\n{MAGENTA}{NEGRITA}VIDUP v{__version__} — Menú principal{NC}')
            print(f'{CIAN}Dispositivo: {info_disp["dispositivo"]} (ID {info_disp["dispositivo_id"]}){NC}')
            print(f'{CIAN}Plataforma : {info_disp["plataforma"]} ({info_disp["maquina"]}){NC}')
            print(f'{CIAN}Conexión   : {describir_conexion()}{NC}')
            print(f'{MAGENTA}Telegram   : {"activo" if telegram_configurado() else "desactivado"}{NC}')
            print()
            print('  1. Escanear vídeos')
            print('  2. Comparar una o varias bases de datos')
            print('  3. Mover vídeos')
            print('  4. Respaldos')
            print('  5. Depurar base de datos')
            print('  6. Operaciones SHA256 (varias carpetas en un catálogo)')
            print('  7. Estadísticas, verificación bidireccional y reportes')
            print('  8. Verificación y ubicación de archivos')
            print('  9. Configuración')
            print('  0. Salir')

        try:
            opcion = pedir_texto('¿Qué quieres hacer?', '0', permitir_escape=False).strip().lower()
        except RegresarAlMenu:
            continue

        if opcion in _PALABRAS_SALIR:
            return 0

        try:
            if opcion == '1':
                db_defecto = submenu_escaneo(db_defecto)
            elif opcion == '2':
                db_defecto = comparar_interactivo(db_defecto)
            elif opcion == '3':
                db_defecto = submenu_mover(db_defecto)
            elif opcion == '4':
                db_defecto = submenu_respaldos(db_defecto)
            elif opcion == '5':
                db_defecto = submenu_depurar(db_defecto)
            elif opcion == '6':
                from core.sha256 import menu as menu_sha256
                db_defecto = _tras_menu_sha256(menu_sha256(DIR_SCRIPT, db_defecto), db_defecto)
            elif opcion == '7':
                db_defecto = submenu_estadisticas(db_defecto)
            elif opcion == '8':
                db_defecto = submenu_verificacion(db_defecto)
            elif opcion == '9':
                db_defecto = submenu_configuracion(db_defecto)
            else:
                imprimir('[yellow]Elige una opción entre 0 y 9.[/]')
        except EOFError:
            raise
        except RegresarAlMenu:
            imprimir('[yellow]↩ Operación cancelada.[/]')
            continue
        except (OSError, ValueError) as exc:
            imprimir(f'[red][ERROR] {exc}[/]')
        except Exception as exc:
            imprimir(f'[red][ERROR] {exc}[/]')
            if os.environ.get('vidup_debug'):
                import traceback
                traceback.print_exc()


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='vidup', description=f"{__desc_herramienta__}: versión {__version__}")
    parser.add_argument('-ver', '--version', action='version', version=f'%(prog)s {__version__}', help='Muestra la versión de la herramienta')
    parser.add_argument('-mod', '--modo', choices=['estandar', 'previo', 'codec'], default=None, help='Modo de escaneo a ejecutar (estandar, previo, codec)')
    parser.add_argument('-dir', '--directorio', type=str, default=None, help='Directorio de vídeos a procesar')
    parser.add_argument('-db', '--base-datos', type=str, default=None, help='Ruta de la base de datos SQLite')
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    if os.name == 'nt':
        os.system('')
    args = construir_parser().parse_args(argv)

    try:
        from core.database import _detectar_modo_conexion_una_vez
        _detectar_modo_conexion_una_vez()
        from core.sincronizacion import sincronizar_al_iniciar
        sincronizar_al_iniciar()
        from core.sincronizar_codigo import sincronizar_archivos_python
        sincronizar_archivos_python()
    except Exception:
        pass

    if not verificar_obligatorias():
        return 1

    set_high_priority()

    if args.modo:
        db_defecto = args.base_datos or _base_reciente_por_defecto()
        if args.modo == 'estandar':
            escanear_interactivo(db_defecto)
        elif args.modo == 'previo':
            from core.flujo_escaneo import escaneo_previo_codecs_interactivo
            escaneo_previo_codecs_interactivo(db_defecto)
        elif args.modo == 'codec':
            from core.flujo_escaneo import escanear_por_codec_interactivo
            escanear_por_codec_interactivo(db_defecto)
        return 0

    try:
        return menu_interactivo()
    except (KeyboardInterrupt, EOFError):
        imprimir('\n[cyan]VIDUP finalizado.[/]')
        return 0


if __name__ == '__main__':
    sys.exit(main())
