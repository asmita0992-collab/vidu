# -*- coding: utf-8 -*-
"""Interfaz Rich centralizada de VIDUP para Android/Termux, Linux y Windows.

Aplica una paleta de colores estándar Rich (8 bits), menús responsivos,
indicadores y ayuda visual. Mantiene alternativa textual sin Rich y no cambia
la lógica de las opciones ni ejecuta acciones sobre las bases de datos.
"""
from __future__ import annotations

import shutil
from typing import Sequence
from core.presentacion import CONSOLA, rich_escape, imprimir

try:
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.console import Group
    from rich import box
except ImportError:
    Panel = Table = Text = Group = box = None

# Nombres y colores verificados contra «Standard Colors» de Rich.
PALETA = {
    'titulo': 'plum1',
    'info': 'deep_sky_blue1',
    'exito': 'spring_green2',
    'accion': 'turquoise2',
    'numero': 'gold1',
    'aviso': 'orange1',
    'peligro': 'indian_red1',
    'secundario': 'grey70',
    'borde': 'steel_blue1',
    'detalles': 'light_sky_blue1',
}


def ancho_terminal() -> int:
    """Ancho efectivo: nunca asumir columnas de PC en un Termux estrecho."""
    try:
        if CONSOLA is not None:
            return max(25, int(CONSOLA.size.width))
    except Exception:
        pass
    return max(25, shutil.get_terminal_size(fallback=(78, 24)).columns)


def menu_rich(titulo: str, opciones: Sequence[tuple[str, str]], *,
              subtitulo: str = '', peligrosas: Sequence[str] = (),
              contexto: str = '') -> None:
    """Menú de alto contraste, visible de arriba abajo incluso en 45 columnas.

    No usa tablas apretadas. En pantallas angostas cada opción usa hasta dos
    líneas con sangría. En pantallas grandes permite textos largos en una línea.
    """
    if not CONSOLA or not Panel or not Text or not Group:
        print(f'\n{titulo}')
        if subtitulo:
            print(subtitulo)
        if contexto:
            print(contexto)
        for n, descripcion in opciones:
            print(f'  {n:>2}  {descripcion}')
        return

    ancho = ancho_terminal()
    encabezado = Text()
    encabezado.append(titulo, style='bold plum1')
    if subtitulo:
        encabezado.append('\n' + subtitulo, style='light_sky_blue1')
    if contexto:
        encabezado.append('\n' + contexto, style='grey70')
    CONSOLA.print(Panel(encabezado, border_style='medium_purple',
                       title='VIDUP · NAVEGACIÓN', title_align='left',
                       box=box.ROUNDED, padding=(0, 1)))

    paletas = ('turquoise2', 'spring_green2', 'cornflower_blue',
               'light_sky_blue1', 'plum1', 'aquamarine3', 'gold1')
    lineas = []
    para_moviles = ancho < 80
    for indice, (numero, descripcion) in enumerate(opciones):
        peligro = numero in peligrosas
        color = ('indian_red1' if peligro else ('grey70' if numero in ('0', '00')
                 else paletas[indice % len(paletas)]))
        texto = Text()
        texto.append(f' {numero:>2} ', style='bold black on gold1' if not peligro
                     else 'bold white on red3')
        if para_moviles:
            texto.append('\n    ')
        else:
            texto.append('  ')
        texto.append(descripcion, style=f'bold {color}' if not peligro else 'bold indian_red1')
        lineas.append(texto)
    CONSOLA.print(Panel(Group(*lineas), border_style='dark_turquoise',
                       title='ACCIONES DISPONIBLES', title_align='left',
                       box=box.ROUNDED, padding=(0, 1)))


def mensaje_estado(titulo: str, descripcion: str, estado: str = 'info') -> None:
    """Presenta información/errores/avisos con colores semánticos consistentes."""
    color = {'ok':'spring_green2', 'warning':'orange1',
             'error':'indian_red1'}.get(estado, 'deep_sky_blue1')
    if CONSOLA is not None and Panel:
        CONSOLA.print(Panel(Text(str(descripcion)), title=f'[{color}]{rich_escape(titulo)}[/]',
                           border_style=color, box=box.ROUNDED))
    else:
        imprimir(f'{titulo}: {descripcion}')
