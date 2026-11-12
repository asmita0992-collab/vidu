# -*- coding: utf-8 -*-

"""Módulo para gestionar la configuración de VIDUP (BD en línea, Telegram, Lotes y Sincronización)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Any

from core import __version__
from core.presentacion import (
    imprimir, pedir_si_no, pedir_texto, pedir_numero, RegresarAlMenu,
    CONSOLA, Panel
)
try:
    from rich.table import Table
    from rich import box as rich_box
except ImportError:
    Table = None
    rich_box = None
from core.database import _usa_neon, describir_conexion, reset_modo_conexion
from core.telegram import (
    TELEGRAM_HABILITADO, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    configurado as telegram_configurado, recargar_config_telegram, probar as probar_telegram
)
from core.sincronizacion import sincronizar_local_y_nube
from core.plataforma import (
    obtener_prioridad_configurada, aplicar_prioridad, OPCIONES_PRIORIDAD
)

DIR_SCRIPT: Path = Path(__file__).resolve().parent.parent
ENV_PATH: Path = DIR_SCRIPT / ".env"


def actualizar_env(cambios: Dict[str, str]) -> None:
    """Actualiza o agrega variables clave=valor en el archivo .env."""
    lineas = []
    if ENV_PATH.is_file():
        try:
            with ENV_PATH.open('r', encoding='utf-8') as f:
                lineas = f.readlines()
        except OSError:
            lineas = []

    claves_pendientes = set(cambios.keys())
    nuevas_lineas = []

    for linea in lineas:
        linea_strip = linea.strip()
        if not linea_strip or linea_strip.startswith('#'):
            nuevas_lineas.append(linea)
            continue
        if '=' in linea:
            clave, _ = linea.split('=', 1)
            clave = clave.strip()
            if clave in cambios:
                nuevas_lineas.append(f'{clave}={cambios[clave]}\n')
                claves_pendientes.discard(clave)
                continue
        nuevas_lineas.append(linea)

    for clave in claves_pendientes:
        nuevas_lineas.append(f'{clave}={cambios[clave]}\n')

    with ENV_PATH.open('w', encoding='utf-8') as f:
        f.writelines(nuevas_lineas)

    for k, v in cambios.items():
        os.environ[k] = str(v)

    reset_modo_conexion()
    recargar_config_telegram()


def submenu_configuracion(db_defecto: str) -> str:
    """Submenú interactivo para configurar la base de datos en línea, Telegram, lotes y sincronización."""
    while True:
        modo_neon = "Desactivada (Solo SQLite local)" if os.environ.get('VIDUP_FORZAR_OFFLINE', '').strip() in ('1', 'true', 'yes', 'sí') else ("Activada (" + describir_conexion() + ")")
        hab_tg = os.environ.get('TELEGRAM_HABILITADO', '1').strip().lower() not in ('0', 'false', 'no', 'desactivado')
        estado_tg = "Activado" if hab_tg else "Desactivado"
        pausa_min = float(os.environ.get('VIDUP_TIEMPO_PAUSA_LOTES', '3') or '3')
        pausa_texto = f'{pausa_min:g}'
        prio_actual = obtener_prioridad_configurada()

        if CONSOLA is not None and Panel is not None and Table is not None and rich_box is not None:
            CONSOLA.print()
            CONSOLA.print(Panel.fit(
                f'[bold bright_white]Configuración general de VIDUP[/]\n'
                f'[cyan]Base de datos en línea (Neon) :[/] {modo_neon}\n'
                f'[cyan]Notificaciones de Telegram     :[/] {estado_tg}\n'
                f'[cyan]Pausa entre lotes de escaneo   :[/] {pausa_texto} min\n'
                f'[cyan]Prioridad del proceso (OS)    :[/] [gold1]{prio_actual}[/]',
                title=f'[bold plum1]VIDUP v{__version__}[/] [gold1]· Ajustes (Submenú 9)[/]',
                border_style='bright_cyan',
            ))
            tabla = Table(box=rich_box.ROUNDED, border_style='cyan', show_header=False)
            tabla.add_column('Nº', style='bold gold1', width=4)
            tabla.add_column('Opción', style='white')
            tabla.add_row('1', 'Activar / Desactivar BD en línea (Neon)')
            tabla.add_row('2', 'Configurar URL de conexión de Neon')
            tabla.add_row('3', 'Activar / Desactivar notificaciones de Telegram')
            tabla.add_row('4', 'Configurar Token y Chat ID de Telegram')
            tabla.add_row('5', 'Probar envío de mensaje por Telegram')
            tabla.add_row('6', 'Configurar tiempo de pausa entre lotes de escaneo (en minutos)')
            tabla.add_row('7', 'Sincronizar bases de datos ahora (Local <-> Neon)')
            tabla.add_row('8', 'Establecer nivel de prioridad del proceso (Windows / Linux / macOS / Android)')
            tabla.add_row('0', 'Regresar al menú principal')
            CONSOLA.print(tabla)
        else:
            print(f'\nVIDUP v{__version__} — Configuración general')
            print(f'  BD en línea (Neon) : {modo_neon}')
            print(f'  Telegram           : {estado_tg}')
            print(f'  Pausa entre lotes  : {pausa_texto} min')
            print(f'  Prioridad proceso  : {prio_actual}')
            print('  1. Activar / Desactivar BD en línea (Neon)')
            print('  2. Configurar URL de conexión de Neon')
            print('  3. Activar / Desactivar notificaciones de Telegram')
            print('  4. Configurar Token y Chat ID de Telegram')
            print('  5. Probar envío de mensaje por Telegram')
            print('  6. Configurar tiempo de pausa entre lotes de escaneo (en minutos)')
            print('  7. Sincronizar bases de datos ahora (Local <-> Neon)')
            print('  8. Establecer nivel de prioridad del proceso (Windows / Linux / macOS / Android)')
            print('  0. Regresar al menú principal')

        try:
            opc = pedir_texto('Elige una opción', '0').strip()
        except RegresarAlMenu:
            break

        if opc in ('0', '00', 'salir', 'q'):
            break

        if opc == '1':
            actual_off = os.environ.get('VIDUP_FORZAR_OFFLINE', '').strip() in ('1', 'true', 'yes', 'sí')
            nuevo_estado = not actual_off
            if nuevo_estado:
                actualizar_env({'VIDUP_FORZAR_OFFLINE': '1'})
                imprimir('[yellow]BD en línea (Neon) DESACTIVADA. VIDUP funcionará solo en modo local.[/]')
            else:
                actualizar_env({'VIDUP_FORZAR_OFFLINE': '0'})
                url_actual = os.environ.get('NEON_DATABASE_URL', '').strip()
                if not url_actual:
                    imprimir('[yellow]No hay URL de Neon configurada.[/]')
                    nova_url = pedir_texto('Introduce la URL de conexión a Neon PostgreSQL', '')
                    if nova_url:
                        actualizar_env({'NEON_DATABASE_URL': nova_url})
                imprimir('[spring_green2]BD en línea (Neon) ACTIVADA.[/]')

        elif opc == '2':
            url_actual = os.environ.get('NEON_DATABASE_URL', '').strip()
            imprimir(f'[cyan]URL actual:[/] {url_actual or "ninguna"}')
            nova_url = pedir_texto('Nueva URL de conexión de Neon (Enter para mantener)', url_actual)
            if nova_url:
                actualizar_env({'NEON_DATABASE_URL': nova_url, 'VIDUP_FORZAR_OFFLINE': '0'})
                imprimir('[spring_green2]✓ URL de Neon actualizada.[/]')

        elif opc == '3':
            actual_hab = os.environ.get('TELEGRAM_HABILITADO', '1').strip().lower() not in ('0', 'false', 'no', 'desactivado')
            nuevo_hab = not actual_hab
            if nuevo_hab:
                actualizar_env({'TELEGRAM_HABILITADO': '1'})
                tok = os.environ.get('TELEGRAM_TOKEN', '').strip()
                cid = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
                if not tok or not cid:
                    imprimir('[yellow]Faltan credenciales de Telegram.[/]')
                    if not tok:
                        tok = pedir_texto('Token del Bot de Telegram', '')
                    if not cid:
                        cid = pedir_texto('Chat ID de Telegram', '')
                    actualizar_env({'TELEGRAM_TOKEN': tok, 'TELEGRAM_CHAT_ID': cid})
                imprimir('[spring_green2]Telegram ACTIVADO.[/]')
            else:
                actualizar_env({'TELEGRAM_HABILITADO': '0'})
                imprimir('[yellow]Telegram DESACTIVADO.[/]')

        elif opc == '4':
            tok_actual = os.environ.get('TELEGRAM_TOKEN', '').strip()
            cid_actual = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
            nuevo_tok = pedir_texto('Token del Bot de Telegram', tok_actual)
            nuevo_cid = pedir_texto('Chat ID de Telegram', cid_actual)
            actualizar_env({
                'TELEGRAM_TOKEN': nuevo_tok,
                'TELEGRAM_CHAT_ID': nuevo_cid,
                'TELEGRAM_HABILITADO': '1'
            })
            imprimir('[spring_green2]✓ Credenciales de Telegram actualizadas.[/]')

        elif opc == '5':
            probar_telegram()

        elif opc == '6':
            actual = float(os.environ.get('VIDUP_TIEMPO_PAUSA_LOTES', '3') or '3')
            nuevo = pedir_numero('Minutos de pausa entre lotes', actual, 0.1, 120)
            nuevo_texto = f'{nuevo:g}'
            actualizar_env({'VIDUP_TIEMPO_PAUSA_LOTES': nuevo_texto})
            imprimir(f'[spring_green2]✓ Tiempo de pausa entre lotes actualizado a {nuevo_texto} min.[/]')

        elif opc == '7':
            reset_modo_conexion()
            try:
                subidos, descargados, elim = sincronizar_local_y_nube(confirmar=True)
            except Exception as exc:
                imprimir(f'[bold red]Sincronización no aplicada/completada: {exc}[/]')
            # El motor anuncia el resultado; no convertir errores/cancelaciones en éxito.

        elif opc == '8':
            imprimir('\n[bold cyan]Niveles de prioridad del proceso:[/]')
            for k, item in OPCIONES_PRIORIDAD.items():
                imprimir(f'  [gold1]{k}.[/] [bright_white]{item["etiqueta"]}[/]')
            sel = pedir_texto('Elige nivel de prioridad (1-4)', '2').strip()
            if sel in OPCIONES_PRIORIDAD:
                nombre_prio = OPCIONES_PRIORIDAD[sel]['nombre']
                actualizar_env({'VIDUP_PRIORIDAD': nombre_prio})
                aplicar_prioridad(nombre_prio)
                imprimir(f'[spring_green2]✓ Prioridad del proceso guardada y aplicada: {nombre_prio}[/]')
            else:
                imprimir('[yellow]Opción de prioridad inválida.[/]')

    return db_defecto