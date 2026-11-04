#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Verificación y gestión de dependencias del sistema."""

from __future__ import annotations

import os
import shutil
from typing import List, Tuple

from .plataforma import detectar_so, so_legible, distro_linux, arquitectura_maquina
from .presentacion import (
    imprimir, panel, linea_dep, linea_opcional, Panel, CONSOLA,
    TIENE_RICH,
)


# ============================================================================
# Constantes: extensiones de video
# ============================================================================
EXTENSIONES_VIDEO = {
    '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v',
    '.mpg', '.mpeg', '.ts', '.3gp', '.ogv', '.rm', '.rmvb', '.vob',
    '.divx', '.m2ts',
}


# ============================================================================
# Helpers privados
# ============================================================================
def _version_de(modulo, nombre_pypi: str) -> str:
    """Extrae versión de un módulo Python."""
    v = getattr(modulo, '__version__', '')
    if v:
        return str(v)
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version(nombre_pypi)
    except Exception:
        return ''


def _pip_name(nombre: str) -> str:
    """Mapea nombres de módulos a nombres de paquetes pip."""
    mapa = {
        'dotenv': 'python-dotenv',
        'psutil': 'psutil',
        'Pillow': 'Pillow',
        'ImageHash': 'ImageHash',
        'numpy': 'numpy',
    }
    return mapa.get(nombre, nombre)


def _instrucciones_instalacion(faltantes: List[str]) -> str:
    """Genera instrucciones de instalación para dependencias faltantes."""
    so = detectar_so()
    lineas = ['', '[gold1]Para instalarlas:[/]', '']

    if so == 'windows':
        binarios = [f for f in faltantes if f in ('ffmpeg', 'ffprobe', 'exiftool')]
        python_pkgs = [f for f in faltantes if f not in ('ffmpeg', 'ffprobe', 'exiftool')]
        if 'ffmpeg' in binarios or 'ffprobe' in binarios:
            lineas.append('  Windows (PowerShell):')
            lineas.append('    winget install Gyan.FFmpeg')
        if 'exiftool' in binarios:
            lineas.append('    winget install OliverBetz.ExifTool')
        if python_pkgs:
            pkgs = ' '.join(_pip_name(p) for p in python_pkgs)
            lineas.append(f'    python -m pip install {pkgs}')
    elif so == 'macos':
        binarios = [f for f in faltantes if f in ('ffmpeg', 'ffprobe', 'exiftool')]
        python_pkgs = [f for f in faltantes if f not in ('ffmpeg', 'ffprobe', 'exiftool')]
        if binarios:
            lineas.append('  macOS (Homebrew):')
            for b in binarios:
                if b in ('ffmpeg', 'ffprobe'):
                    if 'ffmpeg' in binarios and 'ffprobe' in binarios:
                        binarios.remove('ffprobe')
                lineas.append(f'    brew install {b}')
        if python_pkgs:
            pkgs = ' '.join(_pip_name(p) for p in python_pkgs)
            lineas.append(f'    pip install {pkgs}')
    elif so == 'android':
        binarios = [f for f in faltantes if f in ('ffmpeg', 'ffprobe', 'exiftool')]
        python_pkgs = [f for f in faltantes if f not in ('ffmpeg', 'ffprobe', 'exiftool')]
        if 'ffmpeg' in binarios or 'ffprobe' in binarios:
            lineas.append('  Android (Termux):')
            lineas.append('    pkg install ffmpeg')
        if 'exiftool' in binarios:
            lineas.append('    pkg install exiftool')
        if python_pkgs:
            pkgs = ' '.join(_pip_name(p) for p in python_pkgs)
            lineas.append(f'    pip install {pkgs}')
    else:
        distro = distro_linux()
        gestor = 'apt' if distro in ('ubuntu', 'debian', 'linuxmint', 'pop') else (
            'dnf' if distro in ('fedora', 'rhel', 'centos') else (
                'pacman' if distro in ('arch', 'manjaro') else 'apt'))
        binarios = [f for f in faltantes if f in ('ffmpeg', 'ffprobe', 'exiftool')]
        python_pkgs = [f for f in faltantes if f not in ('ffmpeg', 'ffprobe', 'exiftool')]
        if binarios:
            lineas.append(f'  Linux ({distro}):')
            if gestor == 'apt':
                paquetes = []
                if 'ffmpeg' in binarios or 'ffprobe' in binarios:
                    paquetes.append('ffmpeg')
                if 'exiftool' in binarios:
                    paquetes.append('libimage-exiftool-perl')
                lineas.append(f'    sudo apt install {" ".join(paquetes)}')
            elif gestor == 'dnf':
                paquetes = []
                if 'ffmpeg' in binarios or 'ffprobe' in binarios:
                    paquetes.append('ffmpeg')
                if 'exiftool' in binarios:
                    paquetes.append('perl-Image-ExifTool')
                lineas.append(f'    sudo dnf install {" ".join(paquetes)}')
            elif gestor == 'pacman':
                paquetes = []
                if 'ffmpeg' in binarios or 'ffprobe' in binarios:
                    paquetes.append('ffmpeg')
                if 'exiftool' in binarios:
                    paquetes.append('perl-image-exiftool')
                lineas.append(f'    sudo pacman -S {" ".join(paquetes)}')
        if python_pkgs:
            pkgs = ' '.join(_pip_name(p) for p in python_pkgs)
            lineas.append(f'    pip install {pkgs}')

    return '\n'.join(lineas)


# ============================================================================
# Verificación de dependencias
# ============================================================================
def verificar_obligatorias() -> bool:
    """
    Verifica que todas las dependencias obligatorias estén instaladas.
    Si falta alguna, imprime un panel con instrucciones y retorna False.
    """
    if os.name == 'nt':
        os.system('')

    resultados: List[Tuple[bool, str, str]] = []
    opcionales: List[Tuple[bool, str, str]] = []

    for binario in ('ffmpeg', 'ffprobe', 'exiftool'):
        ruta = shutil.which(binario)
        resultados.append((bool(ruta), binario, ruta or 'NO ENCONTRADO'))

    try:
        import dotenv
        resultados.append((True, 'python-dotenv', _version_de(dotenv, 'python-dotenv')))
    except ImportError:
        resultados.append((False, 'dotenv', 'NO INSTALADO'))

    try:
        import psutil
        resultados.append((True, 'psutil', _version_de(psutil, 'psutil')))
    except ImportError:
        resultados.append((False, 'psutil', 'NO INSTALADO'))

    try:
        import PIL
        resultados.append((True, 'Pillow', _version_de(PIL, 'Pillow')))
    except ImportError:
        resultados.append((False, 'Pillow', 'NO INSTALADO'))

    try:
        import imagehash
        resultados.append((True, 'ImageHash', _version_de(imagehash, 'ImageHash')))
    except ImportError:
        resultados.append((False, 'ImageHash', 'NO INSTALADO'))

    try:
        import numpy
        resultados.append((True, 'numpy', _version_de(numpy, 'numpy')))
    except ImportError:
        resultados.append((False, 'numpy', 'NO INSTALADO'))

    if TIENE_RICH:
        try:
            import rich as _rich_mod
            v_rich = _version_de(_rich_mod, 'rich')
        except ImportError:
            v_rich = 'disponible'
        opcionales.append((True, 'rich', v_rich or 'disponible'))
    else:
        opcionales.append((False, 'rich', 'no instalado (se usará ANSI puro)'))

    try:
        import psycopg2
        opcionales.append((True, 'psycopg2', _version_de(psycopg2, 'psycopg2')))
    except ImportError as exc:
        motivo = str(exc).strip()
        if 'libpq' in motivo.lower() or 'cannot open shared object' in motivo.lower():
            detalle = 'falta libpq (pkg install -y libpq postgresql)'
        elif 'no module named' in motivo.lower():
            detalle = 'no instalado (solo SQLite local)'
        else:
            detalle = f'no disponible ({motivo[:60]})'
        opcionales.append((False, 'psycopg2', detalle))

    titulo = "[spring_green2]VIDUP — Verificando dependencias[/]"
    lineas: List[str] = []
    for ok, nombre, detalle in resultados:
        lineas.append(linea_dep(ok, nombre, detalle))
    for ok, nombre, detalle in opcionales:
        lineas.append(linea_opcional(ok, nombre, detalle))

    faltantes = [n for ok, n, _ in resultados if not ok]

    from .telegram import configurado as telegram_configurado
    telegram_estado = '[green]activo[/]' if telegram_configurado() else '[yellow]desactivado[/]'
    lineas.append('')
    lineas.append(f'[plum1]Telegram:[/] {telegram_estado}')
    try:
        from .database import describir_conexion
        lineas.append(f'[plum1]Conexión:[/] [cyan]{describir_conexion()}[/]')
    except Exception:
        lineas.append('[plum1]Conexión:[/] pendiente de inicializar')

    if faltantes:
        lineas.append('')
        lineas.append(f'[red]Faltan {len(faltantes)} dependencias obligatorias. VIDUP no puede continuar.[/]')
        lineas.append(_instrucciones_instalacion(faltantes))
        lineas.append('')
        lineas.append(f'Plataforma detectada: [cyan]{so_legible()}[/] ({arquitectura_maquina()})')

        if CONSOLA is not None and Panel is not None:
            CONSOLA.print(Panel(
                '\n'.join(lineas),
                title=f'[bold red]{titulo}[/]',
                border_style='red',
            ))
        else:
            print('\n'.join(lineas))
        return False

    if CONSOLA is not None and Panel is not None:
        CONSOLA.print(Panel(
            '\n'.join(lineas),
            title=titulo,
            border_style='bright_cyan',
        ))
    else:
        print('\n'.join(lineas))
    return True
