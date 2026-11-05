#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Utilitarios para notificaciones por Telegram."""

from __future__ import annotations

import os
import socket
from typing import Any

from .horario import fmt_ahora_utc6
from .plataforma import so_legible
from .presentacion import imprimir


# ============================================================================
# Configuración de Telegram desde .env
# ============================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_TIMEOUT = 10
TELEGRAM_HABILITADO = os.environ.get("TELEGRAM_HABILITADO", "1").strip().lower() not in ('0', 'false', 'no', 'desactivado')
# Constantes adicionales (valores vacíos en producción)
TELEGRAM_TOKEN_CONST = ""
TELEGRAM_CHAT_ID_CONST = ""

if not TELEGRAM_TOKEN:
    TELEGRAM_TOKEN = TELEGRAM_TOKEN_CONST
if not TELEGRAM_CHAT_ID:
    TELEGRAM_CHAT_ID = TELEGRAM_CHAT_ID_CONST


def recargar_config_telegram() -> None:
    """Recarga la configuración de Telegram desde las variables de entorno."""
    global TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_HABILITADO
    TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    hab = os.environ.get("TELEGRAM_HABILITADO", "1").strip().lower()
    TELEGRAM_HABILITADO = hab not in ('0', 'false', 'no', 'desactivado')


# ============================================================================
# Funciones de Telegram
# ============================================================================
def verificar_red_telegram() -> bool:
    """Verifica si hay conexión a internet para Telegram."""
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=0.8)
        return True
    except OSError:
        try:
            socket.create_connection(("api.telegram.org", 443), timeout=0.8)
            return True
        except OSError:
            return False


def configurado() -> bool:
    """Retorna True si Telegram está configurado y hay conexión."""
    if not (TELEGRAM_HABILITADO and bool(str(TELEGRAM_TOKEN).strip()) and bool(str(TELEGRAM_CHAT_ID).strip())):
        return False
    return verificar_red_telegram()


def escapar(texto: Any) -> str:
    """Escapa caracteres especiales para Telegram HTML."""
    s = '' if texto is None else str(texto)
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def enviar(mensaje: str) -> bool:
    """Envía un mensaje a Telegram. Retorna True si se envió exitosamente."""
    if not configurado():
        return False

    import urllib.parse
    import urllib.request

    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    datos = urllib.parse.urlencode({
        'chat_id': TELEGRAM_CHAT_ID,
        'text': mensaje,
        'parse_mode': 'HTML',
        'disable_web_page_preview': 'true',
    }).encode('utf-8')

    try:
        with urllib.request.urlopen(url, data=datos, timeout=TELEGRAM_TIMEOUT) as respuesta:
            return respuesta.status == 200
    except Exception as exc:
        imprimir(f'   [yellow]⚠  Telegram: no se pudo enviar la notificación ({exc}).[/]')
        return False


def notificar_inicio(titulo: str, detalle: str = '') -> None:
    """Notifica el inicio de un proceso."""
    if not configurado():
        return
    msg = (
        f'▶ <b>{escapar(titulo)}</b> iniciado\n'
        f'🖥 {escapar(so_legible())}\n'
    )
    if detalle:
        msg += f'📄 {escapar(detalle)}\n'
    msg += f'🕒 {fmt_ahora_utc6()}'
    enviar(msg)


def notificar_fin(titulo: str, ok: bool, detalle: str = '', procesados: int = -1, errores: int = -1) -> None:
    """Notifica el fin de un proceso."""
    if not configurado():
        return
    if procesados == 0 and errores > 0:
        icono = '🛑'
        estado_texto = 'falló (0 procesados)'
    elif ok:
        icono = '✅'
        estado_texto = 'finalizado'
    else:
        icono = '❌'
        estado_texto = 'terminó con errores'

    msg = (
        f'{icono} <b>{escapar(titulo)}</b> {estado_texto}\n'
        f'🖥 {escapar(so_legible())}\n'
    )
    if detalle:
        msg += f'📄 {escapar(detalle)}\n'
    msg += f'🕒 {fmt_ahora_utc6()}'
    enviar(msg)


def notificar_error(titulo: str, detalle: str) -> None:
    """Notifica un error."""
    if not configurado():
        return
    msg = (
        f'🛑 <b>Error en {escapar(titulo)}</b>\n'
        f'📄 {escapar(detalle)}\n'
        f'🕒 {fmt_ahora_utc6()}'
    )
    enviar(msg)


def probar() -> None:
    """Prueba la conexión a Telegram."""
    if not configurado():
        imprimir('[yellow]Telegram no está configurado o no hay conexión a internet. Define TELEGRAM_TOKEN y TELEGRAM_CHAT_ID en el archivo .env junto a vidup.py y verifica tu red.[/]')
        return
    imprimir('[cyan]Enviando mensaje de prueba a Telegram...[/]')
    msg = f'✅ Prueba de Telegram desde VIDUP\n🕒 {fmt_ahora_utc6()}\n🖥 {so_legible()}'
    if enviar(msg):
        imprimir('[spring_green2]✓ Mensaje enviado correctamente[/]')
    else:
        imprimir('[red]✗ Error al enviar el mensaje[/]')
