#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Funciones de presentación, salida enriquecida y entrada interactiva."""

from __future__ import annotations

import math
import sys
import re
import unicodedata
from pathlib import Path
from typing import Any, Optional

# Rich es opcional
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from rich.rule import Rule
    from rich.markup import escape as rich_escape
    from rich import box as rich_box
    from rich.progress import (
        Progress, SpinnerColumn, BarColumn, TextColumn,
        TimeElapsedColumn, TimeRemainingColumn, MofNCompleteColumn,
    )
    TIENE_RICH = True
except ImportError:
    Console = None  # type: ignore
    Panel = None    # type: ignore
    Text = None     # type: ignore
    Rule = None     # type: ignore
    rich_box = None # type: ignore
    rich_escape = lambda texto: str(texto)
    TIENE_RICH = False


CONSOLA: Optional[Any] = (
    Console(highlight=False, soft_wrap=True) if Console is not None else None
)


# ============================================================================
# Colores ANSI (fallback cuando rich no está)
# ============================================================================
NC = '\033[0m'
NEGRITA = '\033[1m'
AZUL = '\033[94m'
AMARILLO = '\033[93m'
VERDE = '\033[92m'
ROJO = '\033[91m'
GRIS = '\033[90m'
CIAN = '\033[96m'
BLANCO = '\033[97m'
MAGENTA = '\033[95m'


# ============================================================================
# Funciones de impresión enriquecidas
# ============================================================================
def imprimir(mensaje: str = "", *, estilo: str = "", markup: bool = True) -> None:
    """Imprime un mensaje usando Rich si está disponible, sino con ANSI puro."""
    if CONSOLA is not None:
        CONSOLA.print(mensaje, style=estilo or None, markup=markup)
    else:
        # Sin Rich, no mostrar etiquetas como [grey37] o [/]: solo el texto.
        # Mantener anotaciones de negocio como [ERROR] y [AVISO].
        limpio = re.sub(r'\[(?:/|/?(?:[a-z][a-z0-9_]*(?:[ ]+[a-z0-9_#]+)*|#[a-fA-F0-9]{6}))\]',
                        '', str(mensaje))
        ansi = {'red': ROJO, 'yellow': AMARILLO, 'green': VERDE,
                'cyan': CIAN, 'magenta': MAGENTA, 'white': BLANCO}
        prefijo = ansi.get(estilo, '') if sys.stdout.isatty() else ''
        print(f'{prefijo}{limpio}{NC if prefijo else ""}')


def regla(titulo: str = "", estilo: str = "bright_cyan") -> None:
    """Imprime una línea divisora (regla) con título opcional."""
    if CONSOLA is not None:
        CONSOLA.rule(titulo, style=estilo)
    else:
        print()
        if titulo:
            print(f'{CIAN}{NEGRITA}{titulo}{NC}')
        print('─' * 72)


def panel(cuerpo: str, titulo: str = "", estilo: str = "bright_cyan") -> None:
    """Imprime un panel enriquecido con borde y título opcional."""
    if CONSOLA is not None and Panel is not None:
        CONSOLA.print(Panel.fit(cuerpo, title=titulo or None, border_style=estilo))
    else:
        print()
        print(f'{CIAN}{"═" * 72}{NC}')
        if titulo:
            print(f'{NEGRITA}{titulo}{NC}')
        print(cuerpo)
        print(f'{CIAN}{"═" * 72}{NC}')


def linea_dep(ok: bool, nombre: str, detalle: str = '') -> str:
    """Formatea una línea de dependencia (✓ o ✗)."""
    simbolo = '✓' if ok else '✗'
    color_ok = 'green' if ok else 'red'
    if CONSOLA is not None:
        return f'[{color_ok}]{simbolo}[/] [deep_sky_blue1]{nombre:<16}[/] {detalle}'
    return f'{VERDE if ok else ROJO}{simbolo}{NC} {nombre:<16}{detalle}'


def linea_opcional(ok: bool, nombre: str, detalle: str = '') -> str:
    """Formatea una línea de componente opcional (✓ o ○)."""
    if ok:
        simbolo = '✓'
        color = 'green'
    else:
        simbolo = '○'
        color = 'yellow'

    if CONSOLA is not None:
        return f'[{color}]{simbolo}[/] [deep_sky_blue1]{nombre:<16}[/] {detalle}'
    mapa = {'green': VERDE, 'yellow': AMARILLO}
    return f'{mapa.get(color, AMARILLO)}{simbolo}{NC} {nombre:<16}{detalle}'


# ============================================================================
# Entrada interactiva
# ============================================================================
_PALABRAS_ESCAPE = {'0', '00', 'inicio', 'salir', 'cancelar', 'menu', 'menú', 'volver'}
_MAX_INTENTOS = 5


class RegresarAlMenu(Exception):
    """Se lanza cuando el usuario pide volver al menú o agota intentos."""
    pass


def confirma_frase(respuesta: str, frase_esperada: str) -> bool:
    """Valida frases de seguridad sin distinguir mayúsculas/minúsculas.

    Conserva la confirmación completa (no basta responder "sí"); admite
    espacios repetidos y las variantes Unicode equivalentes, pero NO omite
    palabras ni suprime acentos. Ej.: "aplicar UBICACION" = "APLICAR UBICACION".
    """
    def normalizar(texto: str) -> str:
        return unicodedata.normalize('NFKC', ' '.join(str(texto or '').split())).casefold()
    return bool(frase_esperada) and normalizar(respuesta) == normalizar(frase_esperada)


def limpiar_ruta(valor: Any) -> str:
    """Elimina comillas envolventes (dobles y simples) y espacios sobrantes de una ruta."""
    if valor is None:
        return ""
    val = str(valor).strip()
    while (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        val = val[1:-1].strip()
    return val.strip('"').strip("'").strip()


def pedir_texto(mensaje: str, defecto: str = '', obligatorio: bool = False,
                permitir_escape: bool = True) -> str:
    intentos = 0
    while True:
        sufijo = f' [{defecto}]' if defecto else ''
        try:
            prompt = (f'[bold #87d7ff]{rich_escape(mensaje)}[/][#d7afff]{rich_escape(sufijo)}[/] [gold1]➜[/] '
                      if CONSOLA else f'{mensaje}{sufijo}: ')
            valor = (CONSOLA.input(prompt) if CONSOLA else input(prompt)).strip()
            valor = limpiar_ruta(valor)
        except (EOFError, KeyboardInterrupt):
            raise RegresarAlMenu()

        if permitir_escape and valor.lower() in _PALABRAS_ESCAPE:
            raise RegresarAlMenu()

        valor = valor or str(defecto)
        if valor or not obligatorio:
            return valor

        intentos += 1
        if intentos >= _MAX_INTENTOS:
            imprimir(f'[yellow]⚠ Demasiados intentos sin respuesta válida ({_MAX_INTENTOS}). Regresando...[/]')
            raise RegresarAlMenu()
        imprimir('[yellow]Escribe un valor válido o "inicio" para regresar.[/]')


def pedir_numero(mensaje: str, defecto: Any, minimo: float = 0,
                 maximo: Optional[float] = None, entero: bool = False) -> float:
    intentos = 0
    while True:
        try:
            raw = pedir_texto(mensaje, str(defecto))
            valor = int(raw) if entero else float(raw.replace(',', '.'))
            if not math.isfinite(valor) or valor < minimo or (maximo is not None and valor > maximo):
                raise ValueError
            return valor
        except RegresarAlMenu:
            raise
        except ValueError:
            intentos += 1
            if intentos >= _MAX_INTENTOS:
                imprimir(f'[yellow]⚠ Demasiados intentos fallidos ({_MAX_INTENTOS}). Regresando...[/]')
                raise RegresarAlMenu()
            rango = f'mínimo {minimo}' + (f', máximo {maximo}' if maximo is not None else '')
            imprimir(f'[yellow]Escribe un número válido ({rango}).[/]')


def pedir_limite_escaneo() -> Optional[int]:
    """T = todos; 0 sigue reservado para cancelar/volver al menú.

    No utiliza pedir_numero porque esa función interpreta 0 como salida.
    """
    while True:
        respuesta = pedir_texto(
            'Máximo de vídeos en este lote (T = todos; 0 = cancelar)', 't'
        ).strip().lower()
        if respuesta in ('t', 'todos'):
            return None
        if respuesta.isdecimal() and int(respuesta) > 0:
            return int(respuesta)
        imprimir('[yellow]Introduce T para todos o un entero mayor a cero.[/]')


def pedir_si_no(mensaje: str, defecto: bool = False) -> bool:
    intentos = 0
    while True:
        try:
            valor = pedir_texto(mensaje + ' (s/n)', 's' if defecto else 'n').lower()
        except RegresarAlMenu:
            raise
        if valor in ('s', 'si', 'sí', 'y', 'yes'):
            return True
        if valor in ('n', 'no'):
            return False
        intentos += 1
        if intentos >= _MAX_INTENTOS:
            imprimir(f'[yellow]⚠ Demasiados intentos fallidos ({_MAX_INTENTOS}). Regresando...[/]')
            raise RegresarAlMenu()
        imprimir('[yellow]Responde s o n.[/]')


_RUTAS_SESION: dict = {}


def pedir_ruta(mensaje: str, defecto: str = '', existente: bool = False,
               directorio: bool = False) -> str:
    """Pide una ruta. Recuerda la última usada para el mismo mensaje durante la sesión:
    basta con Enter para reutilizarla. Funciona igual en Windows y Linux."""
    intentos = 0
    recordada = _RUTAS_SESION.get(mensaje, '')
    while True:
        try:
            ruta = Path(pedir_texto(mensaje, recordada or defecto, obligatorio=True)).expanduser().resolve()
        except RegresarAlMenu:
            raise
        if existente and not (ruta.is_dir() if directorio else ruta.is_file()):
            imprimir(f'[yellow]No se encontró {"el directorio" if directorio else "el archivo"}: {ruta}[/]')
            intentos += 1
            if intentos >= _MAX_INTENTOS:
                imprimir(f'[yellow]⚠ Demasiados intentos ({_MAX_INTENTOS}). Regresando...[/]')
                raise RegresarAlMenu()
            continue
        _RUTAS_SESION[mensaje] = str(ruta)
        return str(ruta)


# ============================================================================
# Compatibilidad: alias para escapar HTML/XML
# ============================================================================
def escapar_para_telegram(texto: Any) -> str:
    """Escapa caracteres especiales para Telegram HTML."""
    s = '' if texto is None else str(texto)
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
