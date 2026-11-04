#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""VIDUP — detector de vídeos duplicados y fragmentos.

Ejecuta python vidup.py y elige una operación en el menú interactivo.
Los reportes se guardan junto al programa, dentro de reportes/.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ============================================================================
# Sincronización de __main__ con sys.modules['vidup'] para módulos externos
# ============================================================================
sys.modules['vidup'] = sys.modules[__name__]

# ============================================================================
# Zona horaria fija UTC-6 Ciudad de México
# ============================================================================
ZONA_MEXICO = timezone(timedelta(hours=-6))

def _ahora_utc6() -> datetime:
    return datetime.now(ZONA_MEXICO)

def _fmt_ahora_utc6() -> str:
    return _ahora_utc6().strftime("%Y-%m-%d %H:%M:%S")

def _id_utc6() -> str:
    return _ahora_utc6().strftime("%Y%m%d_%H%M%S")

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
# Rich (opcional) con fallback ANSI
# ============================================================================
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
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
    Table = None    # type: ignore
    Text = None     # type: ignore
    Rule = None     # type: ignore
    rich_box = None # type: ignore
    rich_escape = lambda texto: str(texto)
    TIENE_RICH = False

CONSOLA: Optional["Console"] = (
    Console(highlight=False, soft_wrap=True) if TIENE_RICH else None
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
# Constantes generales
# ============================================================================
EXTENSIONES_VIDEO = {
    '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v',
    '.mpg', '.mpeg', '.ts', '.3gp', '.ogv', '.rm', '.rmvb', '.vob',
    '.divx', '.m2ts',
}

_PALABRAS_ESCAPE = {'0', '00', 'inicio', 'salir', 'cancelar', 'menu', 'menú', 'volver'}
_PALABRAS_SALIR = {'0', '00', 'salir', 'exit', 'quit', 'q'}
_MAX_INTENTOS = 5

# ============================================================================
# Telegram
# ============================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_TIMEOUT = 10
TELEGRAM_HABILITADO = True
TELEGRAM_TOKEN_CONST = ""
TELEGRAM_CHAT_ID_CONST = ""

if not TELEGRAM_TOKEN:
    TELEGRAM_TOKEN = TELEGRAM_TOKEN_CONST
if not TELEGRAM_CHAT_ID:
    TELEGRAM_CHAT_ID = TELEGRAM_CHAT_ID_CONST


class RegresarAlMenu(Exception):
    """Se lanza cuando el usuario pide volver al menú o agota intentos."""
    pass


# ============================================================================
# Helpers de impresión enriquecidos con Rich
# ============================================================================
def imprimir(mensaje: str = "", *, estilo: str = "", markup: bool = True) -> None:
    if CONSOLA is not None:
        CONSOLA.print(mensaje, style=estilo or None, markup=markup)
    else:
        print(mensaje)


def regla(titulo: str = "", estilo: str = "bright_cyan") -> None:
    if CONSOLA is not None:
        CONSOLA.rule(titulo, style=estilo)
    else:
        print()
        if titulo:
            print(f'{CIAN}{NEGRITA}{titulo}{NC}')
        print('─' * 72)


def panel(cuerpo: str, titulo: str = "", estilo: str = "bright_cyan") -> None:
    if CONSOLA is not None:
        CONSOLA.print(Panel.fit(cuerpo, title=titulo or None, border_style=estilo))
    else:
        print()
        print(f'{CIAN}{"═" * 72}{NC}')
        if titulo:
            print(f'{NEGRITA}{titulo}{NC}')
        print(cuerpo)
        print(f'{CIAN}{"═" * 72}{NC}')


# ============================================================================
# Detección de plataforma
# ============================================================================
def es_android() -> bool:
    if os.name == 'nt':
        return False
    if 'com.termux' in os.environ.get('PREFIX', ''):
        return True
    if 'com.termux' in os.environ.get('PATH', ''):
        return True
    try:
        return Path('/data/data/com.termux').exists()
    except Exception:
        return False


def detectar_so() -> str:
    if os.name == 'nt':
        return 'windows'
    if sys.platform.startswith('darwin'):
        return 'macos'
    if es_android():
        return 'android'
    return 'linux'


def so_legible() -> str:
    return {
        'windows': 'Windows',
        'macos': 'macOS',
        'android': 'Android/Termux',
        'linux': 'Linux',
    }.get(detectar_so(), sys.platform)


def _distro_linux() -> str:
    try:
        with open('/etc/os-release', 'r', encoding='utf-8') as f:
            for linea in f:
                if linea.startswith('ID='):
                    return linea.split('=', 1)[1].strip().strip('"').lower()
    except OSError:
        pass
    return 'desconocida'


def _linea_dep(ok: bool, nombre: str, detalle: str = '') -> str:
    simbolo = '✓' if ok else '✗'
    color_ok = 'green' if ok else 'red'
    if CONSOLA is not None:
        return f'[{color_ok}]{simbolo}[/] [deep_sky_blue1]{nombre:<16}[/] {detalle}'
    return f'{VERDE if ok else ROJO}{simbolo}{NC} {nombre:<16}{detalle}'


def _linea_opcional(ok: bool, nombre: str, detalle: str = '') -> str:
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


def _instrucciones_instalacion(faltantes: List[str]) -> str:
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
        distro = _distro_linux()
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


def _pip_name(nombre: str) -> str:
    mapa = {
        'dotenv': 'python-dotenv',
        'psutil': 'psutil',
        'Pillow': 'Pillow',
        'ImageHash': 'ImageHash',
        'numpy': 'numpy',
    }
    return mapa.get(nombre, nombre)


def verificar_dependencias_obligatorias() -> bool:
    if os.name == 'nt':
        os.system('')

    resultados: List[Tuple[bool, str, str]] = []
    opcionales: List[Tuple[bool, str, str]] = []

    def _version_de(modulo, nombre_pypi: str) -> str:
        v = getattr(modulo, '__version__', '')
        if v:
            return str(v)
        try:
            from importlib.metadata import version as _pkg_version
            return _pkg_version(nombre_pypi)
        except Exception:
            return ''

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
        lineas.append(_linea_dep(ok, nombre, detalle))
    for ok, nombre, detalle in opcionales:
        lineas.append(_linea_opcional(ok, nombre, detalle))

    faltantes = [n for ok, n, _ in resultados if not ok]

    telegram_estado = '[green]activo[/]' if telegram_configurado() else '[yellow]desactivado[/]'
    lineas.append('')
    lineas.append(f'[plum1]Telegram:[/] {telegram_estado}')
    try:
        from core.database import describir_conexion
        lineas.append(f'[plum1]Conexión:[/] [cyan]{describir_conexion()}[/]')
    except Exception:
        lineas.append('[plum1]Conexión:[/] pendiente de inicializar')

    if faltantes:
        lineas.append('')
        lineas.append(f'[red]Faltan {len(faltantes)} dependencias obligatorias. VIDUP no puede continuar.[/]')
        lineas.append(_instrucciones_instalacion(faltantes))
        lineas.append('')
        lineas.append(f'Plataforma detectada: [cyan]{so_legible()}[/] ({platform.machine()})')

        if CONSOLA is not None:
            CONSOLA.print(Panel(
                '\n'.join(lineas),
                title=f'[bold red]{titulo}[/]',
                border_style='red',
            ))
        else:
            print('\n'.join(lineas))
        return False

    if CONSOLA is not None:
        CONSOLA.print(Panel(
            '\n'.join(lineas),
            title=titulo,
            border_style='bright_cyan',
        ))
    else:
        print('\n'.join(lineas))
    return True




def _carpetas_android_candidatas() -> List[Path]:
    """
    Detecta carpetas de vídeo de la memoria interna y de tarjetas SD
    en Android, utilizando /proc/self/mountinfo.

    No requiere enumerar /storage ni conocer previamente el UUID
    de la tarjeta SD.
    """
    if not es_android():
        return []

    candidatas: List[Path] = []
    nombres_video = ("Videos", "Vids")

    def agregar(ruta: Path) -> None:
        """Agrega una carpeta accesible, evitando duplicados."""
        try:
            if not ruta.is_dir():
                return

            clave = ruta.resolve(strict=True)

            for existente in candidatas:
                try:
                    if existente.resolve(strict=True) == clave:
                        return
                except OSError:
                    continue

            candidatas.append(ruta)

        except (OSError, PermissionError, RuntimeError):
            pass

    def decodificar_montaje(texto: str) -> str:
        """Decodifica los escapes octales de mountinfo."""
        return (
            texto.replace(r"\040", " ")
                 .replace(r"\011", "\t")
                 .replace(r"\012", "\n")
                 .replace(r"\134", "\\")
        )

    # 1. Memoria interna: mantener el orden de preferencia original.
    interna = Path("/storage/emulated/0")

    for nombre in nombres_video:
        agregar(interna / nombre)

    # 2. Obtener los puntos de montaje sin enumerar /storage.
    puntos_montaje = set()

    try:
        with open(
            "/proc/self/mountinfo",
            "r",
            encoding="utf-8",
        ) as archivo:
            for linea in archivo:
                campos = linea.split()

                # El punto de montaje está en el campo 5 (índice 4).
                if len(campos) < 6:
                    continue

                punto = decodificar_montaje(campos[4])

                # Nos interesan únicamente volúmenes bajo /storage/.
                if not punto.startswith("/storage/"):
                    continue

                # Excluir la memoria interna y sus alias.
                partes = Path(punto).parts

                if len(partes) < 3:
                    continue

                if partes[2].lower() in ("emulated", "self"):
                    continue

                puntos_montaje.add(punto)

    except (OSError, PermissionError):
        pass

    # 3. Comprobar las carpetas de vídeo en cada punto de montaje.
    # En tu dispositivo se incluirá /storage/9F36-61B5.
    for punto in sorted(puntos_montaje):
        raiz = Path(punto)

        for nombre in nombres_video:
            agregar(raiz / nombre)

    return candidatas


# ============================================================================
# Telegram
# ============================================================================
def _verificar_red_telegram() -> bool:
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=0.8)
        return True
    except OSError:
        try:
            socket.create_connection(("api.telegram.org", 443), timeout=0.8)
            return True
        except OSError:
            return False


def telegram_configurado() -> bool:
    if not (TELEGRAM_HABILITADO and bool(str(TELEGRAM_TOKEN).strip()) and bool(str(TELEGRAM_CHAT_ID).strip())):
        return False
    return _verificar_red_telegram()


def telegram_escapar(texto: Any) -> str:
    s = '' if texto is None else str(texto)
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def telegram_enviar(mensaje: str) -> bool:
    if not telegram_configurado():
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
        imprimir(f'   [yellow]⚠️  Telegram: no se pudo enviar la notificación ({exc}).[/]')
        return False


def notificar_inicio(titulo: str, detalle: str = '') -> None:
    if not telegram_configurado():
        return
    msg = (
        f'▶️ <b>{telegram_escapar(titulo)}</b> iniciado\n'
        f'🖥️ {telegram_escapar(so_legible())}\n'
    )
    if detalle:
        msg += f'📄 {telegram_escapar(detalle)}\n'
    msg += f'🕒 {_fmt_ahora_utc6()}'
    telegram_enviar(msg)


def notificar_fin(titulo: str, ok: bool, detalle: str = '') -> None:
    if not telegram_configurado():
        return
    icono = '✅' if ok else '❌'
    msg = (
        f'{icono} <b>{telegram_escapar(titulo)}</b> {"finalizado" if ok else "terminó con errores"}\n'
        f'🖥️ {telegram_escapar(so_legible())}\n'
    )
    if detalle:
        msg += f'📄 {telegram_escapar(detalle)}\n'
    msg += f'🕒 {_fmt_ahora_utc6()}'
    telegram_enviar(msg)


def notificar_error(titulo: str, detalle: str) -> None:
    if not telegram_configurado():
        return
    msg = (
        f'🛑 <b>Error en {telegram_escapar(titulo)}</b>\n'
        f'📄 {telegram_escapar(detalle)}\n'
        f'🕒 {_fmt_ahora_utc6()}'
    )
    telegram_enviar(msg)


# Aliases para compatibilidad con módulos externos (ej. sha256.py)
_notificar_inicio = notificar_inicio
_notificar_fin = notificar_fin
_notificar_error = notificar_error


def probar_telegram() -> None:
    if not telegram_configurado():
        imprimir('[yellow]Telegram no está configurado o no hay conexión a internet. Define TELEGRAM_TOKEN y TELEGRAM_CHAT_ID en el archivo .env junto a vidup.py y verifica tu red.[/]')
        return
    msg = (
        '🧪 <b>Prueba de VIDUP</b>\n'
        f'🖥️ {telegram_escapar(so_legible())}\n'
        f'📂 <code>{telegram_escapar(str(DIR_SCRIPT))}</code>\n'
        f'🕒 {_fmt_ahora_utc6()}'
    )
    if telegram_enviar(msg):
        imprimir('[spring_green2]✅ Notificación de prueba enviada.[/]')
    else:
        imprimir('[red]❌ No se pudo enviar la notificación de prueba.[/]')


def set_high_priority() -> None:
    try:
        if os.name == 'nt':
            os.system('')
            try:
                import psutil
                p = psutil.Process(os.getpid())
                p.nice(psutil.HIGH_PRIORITY_CLASS)
                imprimir('[spring_green2]✓ Prioridad del proceso elevada a HIGH_PRIORITY_CLASS.[/]')
            except Exception as e:
                imprimir(f'[yellow]⚠️ No se pudo ajustar la prioridad: {e}[/]')
        else:
            try:
                os.nice(-5)
                imprimir('[spring_green2]✓ nice() ajustado a -5.[/]')
            except (PermissionError, AttributeError):
                pass
    except Exception:
        pass


def pedir_con_cuenta_regresiva(mensaje: str, timeout: int, por_defecto: str) -> str:
    if not sys.stdin.isatty():
        return por_defecto

    if os.name == 'nt':
        try:
            import msvcrt
        except ImportError:
            return por_defecto

        inicio = time.time()
        entrada = ''
        while True:
            restante = timeout - int(time.time() - inicio)
            if restante <= 0:
                sys.stdout.write(f'\r\033[2K{mensaje} [0s] -> {por_defecto}\n')
                sys.stdout.flush()
                return por_defecto

            sys.stdout.write(f'\r\033[2K{mensaje} [{restante}s]: {entrada}')
            sys.stdout.flush()

            t_espera = time.time()
            while time.time() - t_espera < 0.2:
                if msvcrt.kbhit():
                    c = msvcrt.getwche()
                    if c in ('\r', '\n'):
                        sys.stdout.write('\n')
                        resultado = entrada.strip()
                        if resultado.lower() in _PALABRAS_ESCAPE:
                            raise KeyboardInterrupt
                        return resultado if resultado else por_defecto
                    elif c == '\b':
                        entrada = entrada[:-1]
                        sys.stdout.write(' \b')
                    else:
                        entrada += c
                time.sleep(0.05)
    else:
        try:
            import select
            import tty
            import termios
        except ImportError:
            return por_defecto

        fd = sys.stdin.fileno()
        try:
            old_settings = termios.tcgetattr(fd)
        except termios.error:
            return por_defecto

        try:
            tty.setcbreak(fd)
            inicio = time.time()
            entrada = ''
            while True:
                restante = timeout - int(time.time() - inicio)
                if restante <= 0:
                    sys.stdout.write(f'\r\033[2K{mensaje} [0s] -> {por_defecto}\n')
                    sys.stdout.flush()
                    return por_defecto

                sys.stdout.write(f'\r\033[2K{mensaje} [{restante}s]: {entrada}')
                sys.stdout.flush()

                r, _, _ = select.select([sys.stdin], [], [], 0.2)
                if r:
                    c = sys.stdin.read(1)
                    if c in ('\n', '\r'):
                        sys.stdout.write('\n')
                        resultado = entrada.strip()
                        if resultado.lower() in _PALABRAS_ESCAPE:
                            raise KeyboardInterrupt
                        return resultado if resultado else por_defecto
                    elif c in ('\x08', '\x7f'):
                        entrada = entrada[:-1]
                    else:
                        entrada += c
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _directorio_script() -> Path:
    return DIR_SCRIPT



def _carpeta_reportes() -> Path:
    return DIR_SCRIPT / 'reportes'


def _ruta_errores_jsonl() -> Path:
    return _carpeta_reportes() / 'errores' / 'jsonl' / 'errores_videos.jsonl'


def _ruta_errores_txt() -> Path:
    return _carpeta_reportes() / 'errores' / 'txt' / 'errores_videos.txt'


def _asegurar_estructura_reportes() -> None:
    base = _carpeta_reportes()
    for sub in (
        'comparativas/json', 'comparativas/csv', 'comparativas/txt',
        'recursos/txt',
        'errores/jsonl', 'errores/txt',
        'exportaciones/json', 'exportaciones/txt',
        'movimientos/json', 'movimientos/txt', 'movimientos/jsonl',
        'sha256/json', 'sha256/csv', 'sha256/txt', 'sha256/jsonl',
        'respaldos/db',
        'escaneos/json',
        'estadisticas/json', 'estadisticas/txt', 'estadisticas/csv',
        'verificaciones/json', 'verificaciones/txt', 'verificaciones/csv',
        'depuraciones/json', 'depuraciones/txt', 'depuraciones/csv',
        'otros',
        'launcher',
    ):
        (base / sub).mkdir(parents=True, exist_ok=True)


def _base_reciente_por_defecto() -> str:
    carpeta = _carpeta_reportes() / 'respaldos' / 'db'
    candidatas: List[Tuple[int, Path]] = []
    if carpeta.is_dir():
        for ruta in carpeta.glob('*.db'):
            if not ruta.is_file():
                continue
            try:
                candidatas.append((ruta.stat().st_mtime_ns, ruta))
            except OSError:
                continue
    if candidatas:
        candidatas.sort(reverse=True)
        return str(candidatas[0][1])
    return str(DIR_SCRIPT / 'vidup.db')


def _rutas_con_error_registrado() -> set:
    rutas: set = set()
    for log in (_ruta_errores_jsonl(), _carpeta_reportes() / 'errores_videos.jsonl'):
        if not log.is_file():
            continue
        try:
            with log.open('r', encoding='utf-8') as archivo:
                for linea in archivo:
                    try:
                        registro = json.loads(linea)
                        ruta = registro.get('ruta')
                        if ruta:
                            rutas.add(str(Path(ruta).resolve()))
                    except (json.JSONDecodeError, AttributeError):
                        continue
        except OSError:
            pass
    return rutas


def _registrar_error_video(nombre: str, ruta: str, causa: str) -> None:
    log_jsonl = _ruta_errores_jsonl()
    log_txt = _ruta_errores_txt()
    try:
        from core.database import info_dispositivo_actual
        info_disp = info_dispositivo_actual()
    except Exception:
        info_disp = {}

    registro = {
        'fecha': _fmt_ahora_utc6(),
        'archivo': nombre,
        'ruta': str(Path(ruta).resolve()),
        'causa': causa,
        'dispositivo': info_disp.get('dispositivo', ''),
        'dispositivo_id': info_disp.get('dispositivo_id', ''),
    }

    try:
        log_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with log_jsonl.open('a', encoding='utf-8') as archivo:
            archivo.write(json.dumps(registro, ensure_ascii=False) + '\n')
        log_txt.parent.mkdir(parents=True, exist_ok=True)
        with log_txt.open('a', encoding='utf-8') as archivo:
            archivo.write(
                f'Fecha: {registro["fecha"]}\n'
                f'Dispositivo: {registro["dispositivo"]} (ID {registro["dispositivo_id"]})\n'
                f'Archivo: {nombre}\nRuta: {registro["ruta"]}\nCausa: {causa}\n\n'
            )
    except OSError as exc:
        imprimir(f'[yellow][AVISO] No se pudo guardar el log de errores: {exc}[/]')


def _es_video(ruta: Path) -> bool:
    return ruta.suffix.lower() in EXTENSIONES_VIDEO


def _listar_videos(directorio: str, recursivo: bool = True) -> List[Path]:
    """Lista vídeos sin entrar en carpetas excluidas para este dispositivo."""
    from core import inventario
    from core.database import info_dispositivo_actual
    base = Path(directorio)
    if not base.is_dir():
        imprimir(f'[red][ERROR] No existe el directorio: {directorio}[/]')
        return []
    excluidas = inventario.cargar_exclusiones(DIR_SCRIPT, info_dispositivo_actual()['dispositivo_id'])
    if inventario.excluido(str(base), excluidas):
        imprimir(f'[yellow]Carpeta excluida del escaneo: {base}[/]')
        return []
    videos = []
    if recursivo:
        def avisar_error(exc):
            imprimir(f'[yellow]No se pudo abrir {rich_escape(str(exc))}[/]')
        for carpeta, subdirectorios, nombres in os.walk(base, followlinks=False, onerror=avisar_error):
            subdirectorios[:] = [n for n in subdirectorios
                                 if not inventario.excluido(os.path.join(carpeta, n), excluidas)]
            for nombre in nombres:
                ruta = Path(carpeta) / nombre
                if _es_video(ruta) and not inventario.excluido(str(ruta), excluidas):
                    videos.append(ruta)
    else:
        videos = [ruta for ruta in base.iterdir() if ruta.is_file() and _es_video(ruta)
                  and not inventario.excluido(str(ruta), excluidas)]
    return sorted(videos)


def _fmt_seg(segundos: float) -> str:
    segundos = max(0.0, segundos)
    horas = int(segundos // 3600)
    minutos = int((segundos % 3600) // 60)
    seg = int(segundos % 60)
    return f'{horas:02d}:{minutos:02d}:{seg:02d}'


def _archivos_recientes(patrones: List[str], subcarpetas: List[str]) -> List[Path]:
    base = _carpeta_reportes()
    encontrados: List[Tuple[int, Path]] = []
    for sub in subcarpetas:
        carpeta = base / sub
        if not carpeta.is_dir():
            continue
        for patron in patrones:
            for ruta in carpeta.glob(patron):
                if ruta.is_file():
                    try:
                        encontrados.append((ruta.stat().st_mtime_ns, ruta))
                    except OSError:
                        continue
    encontrados.sort(reverse=True)
    return [r for _, r in encontrados]


def elegir_archivo_reciente(
    descripcion: str,
    patrones: List[str],
    subcarpetas: List[str],
    defecto: str = '',
    obligatorio: bool = True,
) -> str:
    recientes = _archivos_recientes(patrones, subcarpetas)

    if recientes:
        reciente = recientes[0]
        imprimir(f'\n[deep_sky_blue1]{descripcion}[/]')
        imprimir(f'  [gold1]Más reciente:[/] [bright_white]{reciente}[/]')
        if len(recientes) > 1:
            imprimir(f'  [grey37]({len(recientes) - 1} archivo(s) más antiguo(s) disponibles)[/]')

        try:
            respuesta = pedir_texto('¿Usar el más reciente? (s/n)', 's').lower()
        except RegresarAlMenu:
            raise

        if respuesta in ('s', 'si', 'sí', 'y', 'yes', ''):
            return str(reciente)

    return pedir_ruta(descripcion or 'Ruta del archivo', defecto, existente=False)


def pedir_texto(mensaje: str, defecto: str = '', obligatorio: bool = False,
                permitir_escape: bool = True) -> str:
    intentos = 0
    while True:
        sufijo = f' [{defecto}]' if defecto else ''
        try:
            prompt = (f'[bold #87d7ff]{rich_escape(mensaje)}[/][#d7afff]{rich_escape(sufijo)}[/] [gold1]➜[/] '
                      if CONSOLA else f'{mensaje}{sufijo}: ')
            valor = (CONSOLA.input(prompt) if CONSOLA else input(prompt)).strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            raise RegresarAlMenu()

        if permitir_escape and valor.lower() in _PALABRAS_ESCAPE:
            raise RegresarAlMenu()

        valor = valor or str(defecto)
        if valor or not obligatorio:
            return valor

        intentos += 1
        if intentos >= _MAX_INTENTOS:
            imprimir(f'[yellow]⚠️ Demasiados intentos sin respuesta válida ({_MAX_INTENTOS}). Regresando...[/]')
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
                imprimir(f'[yellow]⚠️ Demasiados intentos fallidos ({_MAX_INTENTOS}). Regresando...[/]')
                raise RegresarAlMenu()
            rango = f'mínimo {minimo}' + (f', máximo {maximo}' if maximo is not None else '')
            imprimir(f'[yellow]Escribe un número válido ({rango}).[/]')


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
            imprimir(f'[yellow]⚠️ Demasiados intentos fallidos ({_MAX_INTENTOS}). Regresando...[/]')
            raise RegresarAlMenu()
        imprimir('[yellow]Responde s o n.[/]')


def pedir_ruta(mensaje: str, defecto: str = '', existente: bool = False,
               directorio: bool = False) -> str:
    intentos = 0
    while True:
        try:
            ruta = Path(pedir_texto(mensaje, defecto, obligatorio=True)).expanduser().resolve()
        except RegresarAlMenu:
            raise
        if existente and not (ruta.is_dir() if directorio else ruta.is_file()):
            imprimir(f'[yellow]No se encontró {"el directorio" if directorio else "el archivo"}: {ruta}[/]')
            intentos += 1
            if intentos >= _MAX_INTENTOS:
                imprimir(f'[yellow]⚠️ Demasiados intentos ({_MAX_INTENTOS}). Regresando...[/]')
                raise RegresarAlMenu()
            continue
        return str(ruta)


class Dashboard:
    def __init__(self, num_workers: int, tty: bool = True):
        self._lock = threading.Lock()
        self._num_workers = max(1, num_workers)
        self._tty = tty and CONSOLA is None
        self._workers: Dict[int, str] = {}
        self._thread_slots: Dict[int, int] = {}
        self._completados = 0
        self._errores = 0
        self._total = 0
        self._lineas_dibujadas = 0

    def iniciar(self, total: int) -> None:
        with self._lock:
            self._total = total
            self._completados = 0
            self._errores = 0
            self._workers = {i: 'Esperando asignación...' for i in range(self._num_workers)}
            self._thread_slots = {}
            self._lineas_dibujadas = 0
            if self._tty:
                sys.stdout.write('\n' * (self._num_workers + 1))
                sys.stdout.flush()
                self._lineas_dibujadas = self._num_workers + 1
            self._dibujar()

    def detener(self) -> None:
        with self._lock:
            if self._tty and self._lineas_dibujadas > 0:
                sys.stdout.write(f'\033[{self._lineas_dibujadas}F')
                sys.stdout.write('\033[J')
                sys.stdout.flush()
            self._workers = {}
            self._thread_slots = {}
            self._lineas_dibujadas = 0

    def _get_slot(self, tid: int) -> int:
        if tid not in self._thread_slots:
            slot = len(self._thread_slots)
            if slot < self._num_workers:
                self._thread_slots[tid] = slot
            else:
                return 0
        return self._thread_slots[tid]

    def actualizar_worker(self, tid: int, texto: str) -> None:
        with self._lock:
            if not self._workers:
                return
            slot = self._get_slot(tid)
            self._workers[slot] = texto
            self._dibujar()

    def marcar_completado(self) -> None:
        with self._lock:
            self._completados += 1
            self._dibujar()

    def marcar_error(self) -> None:
        with self._lock:
            self._errores += 1
            self._dibujar()

    def imprimir_mensaje(self, mensaje: str) -> None:
        with self._lock:
            if self._tty and self._lineas_dibujadas > 0:
                sys.stdout.write(f'\033[{self._lineas_dibujadas}F')
                sys.stdout.write('\033[J')
            print(mensaje)
            if self._workers:
                if self._tty:
                    sys.stdout.write('\n' * (self._num_workers + 1))
                    self._lineas_dibujadas = self._num_workers + 1
                self._dibujar()

    def _dibujar(self) -> None:
        if not self._tty or self._lineas_dibujadas == 0:
            return
        sys.stdout.write(f'\033[{self._lineas_dibujadas}F')
        lineas = [
            f'\033[2K{AZUL}▶ Completados: {NEGRITA}{self._completados}{NC}{AZUL} / {self._total}{NC}'
            f'{AZUL} | Errores: {ROJO}{self._errores}{NC}'
        ]
        for i in range(self._num_workers):
            estado = self._workers.get(i, 'Esperando asignación...')
            lineas.append(f'\033[2K{AMARILLO}Worker {i + 1}:{NC} {estado}')
        sys.stdout.write('\n'.join(lineas) + '\n')
        sys.stdout.flush()
        self._lineas_dibujadas = len(lineas)


class ProgresoFingerprintWorker:
    def __init__(self, dashboard: Dashboard, worker_id: int, nombre_video: str):
        self._dashboard = dashboard
        self._worker_id = worker_id
        self._nombre = nombre_video
        self._ultimo_texto = ''
        self._ultimo_momento = 0.0

    def __call__(self, fase: str, fraccion: float, eta: float) -> None:
        fraccion = max(0.0, min(1.0, fraccion))
        ahora = time.monotonic()
        if ahora - self._ultimo_momento < 0.15 and fraccion < 1.0:
            return
        self._ultimo_momento = ahora
        eta_texto = _fmt_seg(eta) if eta >= 0 else 'calculando'
        ancho = shutil.get_terminal_size(fallback=(100, 24)).columns
        max_nombre = max(15, ancho - 80)
        nombre = self._nombre[: max_nombre - 1] + '…' if len(self._nombre) > max_nombre else self._nombre
        texto = f'{nombre} | {fase:<15} | {fraccion:5.1%} | ETA: {eta_texto}'
        if texto != self._ultimo_texto:
            self._ultimo_texto = texto
            self._dashboard.actualizar_worker(self._worker_id, texto)

    def cerrar(self) -> None:
        pass


def _filtrar_videos_pendientes(videos: List[Path], db: str, forzar: bool) -> Tuple[List[Path], int]:
    if forzar:
        return videos, 0

    from core.database import _usa_neon, cerrar, conectar

    pendientes: List[Path] = []
    omitidos = 0
    rutas_con_error = _rutas_con_error_registrado()
    conn = conectar(db)

    try:
        if _usa_neon():
            import psycopg2.extras
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                for ruta in videos:
                    rr = str(ruta.resolve())
                    if rr in rutas_con_error:
                        omitidos += 1
                    else:
                        cur.execute(
                            'SELECT hash_rapido, EXISTS(SELECT 1 FROM frame_hashes WHERE video_id=videos.id) AS huella_visual FROM videos WHERE ruta=%s',
                            (rr,),
                        )
                        fila = cur.fetchone()
                        if fila and fila['hash_rapido'] != 'sha256_solo' and fila['huella_visual']:
                            omitidos += 1
                        else:
                            pendientes.append(ruta)
        else:
            for ruta in videos:
                rr = str(ruta.resolve())
                if rr in rutas_con_error:
                    omitidos += 1
                else:
                    fila = conn.execute(
                        'SELECT hash_rapido, EXISTS(SELECT 1 FROM frame_hashes WHERE video_id=videos.id) AS huella_visual FROM videos WHERE ruta=?',
                        (rr,),
                    ).fetchone()
                    if fila and fila['hash_rapido'] != 'sha256_solo' and fila['huella_visual']:
                        omitidos += 1
                    else:
                        pendientes.append(ruta)
    finally:
        cerrar(conn)

    return pendientes, omitidos


def _trabajo_fingerprint(ruta: Path, intervalo: float, dashboard: Dashboard,
                         evento_cancelacion: threading.Event, hilos_ffmpeg: int,
                         aceleracion: str, medidor_recursos=None,
                         hash_esperado: Optional[str] = None,
                         tamanio_esperado: Optional[int] = None) -> Tuple[Path, dict]:
    from core.fingerprint import generar_fingerprint

    tid = threading.get_ident()
    ruta_str = str(ruta.resolve())
    nombre = ruta.name
    inicio = time.monotonic()
    medicion = None

    if evento_cancelacion.is_set():
        return ruta, {'estado': 'cancelado', 'tiempo': 0.0, 'medicion': None}

    if medidor_recursos is not None:
        try:
            medicion = medidor_recursos.iniciar_medicion(ruta_str)
        except Exception:
            medicion = None

    barra = ProgresoFingerprintWorker(dashboard, tid, nombre)
    dashboard.actualizar_worker(tid, f'{nombre[:35]} preparando...')

    try:
        fp = generar_fingerprint(
            ruta_str, intervalo=intervalo, progreso=barra,
            cancelar=evento_cancelacion, hilos_ffmpeg=hilos_ffmpeg,
            aceleracion=aceleracion,
        )
    except KeyboardInterrupt:
        evento_cancelacion.set()
        if medicion is not None:
            medicion.error = 'Interrumpido por el usuario'
            medidor_recursos.finalizar_medicion(medicion, completado=False)
        return ruta, {'estado': 'cancelado', 'tiempo': time.monotonic() - inicio, 'medicion': medicion}
    except Exception as exc:
        if medicion is not None:
            medicion.error = f'Error no esperado: {exc}'
            medidor_recursos.finalizar_medicion(medicion, completado=False)
        dashboard.actualizar_worker(tid, f'{nombre[:35]} ERROR: {exc}')
        return ruta, {'estado': 'error', 'error': str(exc), 'tiempo': time.monotonic() - inicio, 'medicion': medicion}

    if evento_cancelacion.is_set():
        if medicion is not None:
            medicion.error = 'Interrumpido por el usuario'
            medidor_recursos.finalizar_medicion(medicion, completado=False)
        return ruta, {'estado': 'cancelado', 'tiempo': time.monotonic() - inicio, 'medicion': medicion}

    if not fp.error and hash_esperado:
        if fp.hash_archivo != hash_esperado or (tamanio_esperado is not None and fp.tamanio_bytes != tamanio_esperado):
            fp.error = 'El vídeo cambió desde el reporte SHA256; vuelve a comparar antes de escanearlo.'

    if fp.error:
        if medicion is not None:
            medicion.rellenar_info_video(fp)
            medicion.error = fp.error
            medidor_recursos.finalizar_medicion(medicion, completado=False)
        dashboard.actualizar_worker(tid, f'{nombre[:35]} ERROR: {fp.error}')
        return ruta, {'estado': 'error', 'error': fp.error, 'tiempo': time.monotonic() - inicio, 'medicion': medicion}

    tiempo = time.monotonic() - inicio
    if medicion is not None:
        medicion.rellenar_info_video(fp)
        medidor_recursos.finalizar_medicion(medicion, completado=True)

    acel_lbl = f'[{fp.metodo_acel}]'
    dashboard.actualizar_worker(tid, f'{nombre[:35]} OK {acel_lbl}: {len(fp.frame_hashes)} frames; {_fmt_seg(tiempo)}')

    return ruta, {
        'estado': 'ok', 'fingerprint': fp,
        'frames': len(fp.frame_hashes), 'tiempo': tiempo, 'medicion': medicion,
    }


def cmd_escanear(args: argparse.Namespace) -> int:
    from core.database import _usa_neon, cerrar, conectar, guardar_fingerprint, describir_conexion, info_dispositivo_actual
    from core.reporter import imprimir_resumen_escaneo
    from core.recursos import MedidorRecursos

    if not math.isfinite(args.intervalo) or args.intervalo <= 0:
        imprimir('[red][ERROR] El intervalo debe ser positivo y finito.[/]')
        return 2
    if args.hilos_ffmpeg <= 0:
        imprimir('[red][ERROR] Los hilos de FFmpeg deben ser un entero mayor a cero.[/]')
        return 2

    notificar_inicio('Escanear vídeos', f'Directorio: {args.directorio or "(por defecto)"}')

    print()
    try:
        if args.carpetas_sub is None:
            ans_sub = pedir_con_cuenta_regresiva("¿Escanear también subcarpetas? (y/n)", 30, "n")
            recursivo = ans_sub.lower() in ('y', 'si', 'sí', 'yes')
        else:
            recursivo = args.carpetas_sub
    except KeyboardInterrupt:
        imprimir('[yellow]Operación cancelada.[/]')
        return 0

    esperados_sha: Dict[str, dict] = {}
    manifiesto_sha = getattr(args, 'manifiesto_sha', None)
    if manifiesto_sha:
        from core.sha256 import cargar_pendientes
        datos_sha, videos_encontrados, esperados_sha, avisos = cargar_pendientes(manifiesto_sha)
        directorio = datos_sha.get('carpeta') or '(rutas del JSON SHA256)'
        imprimir(f'[deep_sky_blue1]Escaneo limitado a los vídeos nuevos del reporte: {manifiesto_sha}[/]')
        for aviso in avisos:
            imprimir(f'[yellow][AVISO] {aviso}[/]')
        if not videos_encontrados:
            imprimir('[yellow]No hay vídeos nuevos disponibles para escanear en ese JSON.[/]')
            return 0 if not avisos else 1
    else:
        if args.directorio:
            directorio = str(Path(args.directorio).expanduser().resolve())
            videos_encontrados = _listar_videos(directorio, recursivo=recursivo)
        else:
            directorio = str(DIR_SCRIPT)
            imprimir(f'[deep_sky_blue1]Buscando vídeos en: {directorio}[/]')
            videos_encontrados = _listar_videos(directorio, recursivo=recursivo)
            if not videos_encontrados:
                imprimir('[yellow]No se encontraron vídeos en la carpeta del script.[/]')
                directorio = pedir_ruta('Directorio a escanear', existente=True, directorio=True)
                if not directorio:
                    return 1
                videos_encontrados = _listar_videos(directorio, recursivo=recursivo)
        if not videos_encontrados:
            imprimir(f'[red]No se encontraron archivos de vídeo en: {directorio}[/]')
            return 1

    # Las rutas de un manifiesto JSON también deben respetar exclusiones.
    from core import inventario as _inventario_escaneo
    _exclusiones = _inventario_escaneo.cargar_exclusiones(
        DIR_SCRIPT, info_dispositivo_actual()['dispositivo_id'])
    antes_excluir = len(videos_encontrados)
    videos_encontrados = [p for p in videos_encontrados
                          if not _inventario_escaneo.excluido(str(p), _exclusiones)]
    if antes_excluir != len(videos_encontrados):
        imprimir(f'[yellow]Omitidos por exclusión: {antes_excluir - len(videos_encontrados)}[/]')
    if not videos_encontrados:
        imprimir('[yellow]No hay vídeos elegibles después de aplicar las exclusiones.[/]')
        return 0

    rutas_con_error = _rutas_con_error_registrado()
    videos_pendientes, omitidos_previos = _filtrar_videos_pendientes(videos_encontrados, args.db, args.forzar)

    if not videos_pendientes:
        errores_registrados = sum(1 for ruta in videos_encontrados if str(ruta.resolve()) in rutas_con_error)
        if errores_registrados:
            imprimir(f'[yellow]{errores_registrados} vídeo(s) omitido(s) por tener error registrado.[/]')
        else:
            imprimir('[spring_green2]Todos los vídeos encontrados ya tienen huella registrada.[/]')
        if getattr(args, 'resumen_json', None):
            _guardar_resumen_json(args.resumen_json, 0, omitidos_previos, [], 0.0)
        notificar_fin('Escanear vídeos', True, 'Nada por procesar')
        return 0

    pendientes_disponibles = len(videos_pendientes)

    if args.max_videos is None:
        ans_max = pedir_con_cuenta_regresiva(
            f"¿Cuántos vídeos analizar de los {pendientes_disponibles} pendientes? (0 para todos)",
            30, "5")
        try:
            max_v = int(ans_max)
            if max_v == 0:
                max_v = pendientes_disponibles
            elif max_v < 0 or max_v > pendientes_disponibles:
                max_v = min(5, pendientes_disponibles)
        except ValueError:
            max_v = min(5, pendientes_disponibles)
    else:
        max_v = args.max_videos
        if max_v <= 0:
            imprimir('[red][ERROR] El límite de vídeos debe ser mayor a cero.[/]')
            return 2

    videos = videos_pendientes[:max_v]

    recomendado = _cpu_threads_recomendados()
    if args.trabajadores is None:
        ans_wk = pedir_con_cuenta_regresiva(
            f"¿Cuántos trabajadores quieres utilizar? (Sugerido {recomendado})",
            30, str(recomendado))
        try:
            trabajadores = int(ans_wk)
            if trabajadores <= 0:
                trabajadores = recomendado
        except ValueError:
            trabajadores = recomendado
    else:
        trabajadores = args.trabajadores
        if trabajadores <= 0:
            imprimir('[red][ERROR] La cantidad de trabajadores mínima es 1.[/]')
            return 2

    if args.aceleracion == 'auto':
        acel_desc = 'auto (detectará aceleradores disponibles)'
    else:
        acel_desc = args.aceleracion

    info_disp = info_dispositivo_actual()

    if CONSOLA is not None:
        tabla = Table(box=rich_box.ROUNDED, border_style='cyan', show_header=False)
        tabla.add_column(style='cyan'); tabla.add_column(style='white')
        tabla.add_row('Directorio', str(directorio))
        tabla.add_row('Vídeos', f'{len(videos)} seleccionados')
        tabla.add_row('Base datos', 'Neon PostgreSQL (Nube)' if _usa_neon() else str(args.db))
        tabla.add_row('Modo conexión', describir_conexion())
        tabla.add_row('Intervalo', f'{args.intervalo} s entre cuadros')
        tabla.add_row('Trabajadores', str(trabajadores))
        tabla.add_row('Hilos FFmpeg/trabajador', str(args.hilos_ffmpeg))
        tabla.add_row('Aceleración', acel_desc)
        tabla.add_row('Dispositivo', info_disp['dispositivo'])
        tabla.add_row('ID dispositivo', info_disp['dispositivo_id'])
        CONSOLA.print(tabla)
    else:
        print(f' Directorio        : {directorio}')
        print(f' Vídeos            : {len(videos)} seleccionados')
        print(f' Base datos        : {"Neon PostgreSQL (Nube)" if _usa_neon() else args.db}')
        print(f' Modo de conexión  : {describir_conexion()}')
        print(f' Intervalo         : {args.intervalo} s entre cuadros')
        print(f' Trabajadores      : {trabajadores}')
        print(f' Hilos FFmpeg/trabajador: {args.hilos_ffmpeg}')
        print(f' Aceleración video : {acel_desc}')
        print(f' Dispositivo       : {info_disp["dispositivo"]}')
        print(f' ID dispositivo    : {info_disp["dispositivo_id"]}')

    medidor_recursos = MedidorRecursos(intervalo_muestreo=2.0)
    mediciones = []
    fallos_detalle: List[Tuple[str, str, str]] = []

    dashboard = Dashboard(num_workers=trabajadores, tty=sys.stdout.isatty())
    evento_cancelacion = threading.Event()
    interrumpido = False
    inicio_total = time.monotonic()

    procesados = 0
    errores = 0
    omitidos = omitidos_previos

    manejador_anterior = signal.getsignal(signal.SIGINT)

    def manejar_ctrl_c(signum, frame):
        nonlocal interrumpido
        if not evento_cancelacion.is_set():
            interrumpido = True
            evento_cancelacion.set()
            return
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, manejar_ctrl_c)

    try:
        dashboard.iniciar(len(videos))

        with ThreadPoolExecutor(max_workers=trabajadores, thread_name_prefix='vidup') as executor:
            futuros: Dict[Future, Path] = {}
            for ruta in videos:
                if evento_cancelacion.is_set():
                    break
                futuro = executor.submit(
                    _trabajo_fingerprint,
                    ruta, args.intervalo, dashboard,
                    evento_cancelacion, args.hilos_ffmpeg, args.aceleracion,
                    medidor_recursos,
                    esperados_sha.get(str(ruta.resolve()), {}).get('sha256'),
                    esperados_sha.get(str(ruta.resolve()), {}).get('tamanio_bytes'),
                )
                futuros[futuro] = ruta

            for futuro in as_completed(futuros):
                if evento_cancelacion.is_set():
                    interrumpido = True
                    for pendiente in futuros:
                        pendiente.cancel()
                    break

                ruta = futuros[futuro]

                try:
                    _, resultado = futuro.result()
                except KeyboardInterrupt:
                    interrumpido = True
                    evento_cancelacion.set()
                    for pendiente in futuros:
                        pendiente.cancel()
                    break
                except Exception as exc:
                    errores += 1
                    err_msg = f'Excepción en hilo: {exc}'
                    rr = str(ruta.resolve())
                    fallos_detalle.append((ruta.name, rr, err_msg))
                    _registrar_error_video(ruta.name, rr, err_msg)
                    dashboard.imprimir_mensaje(f'❌ {ruta.name[:40]} | ERROR: {exc}')
                    dashboard.marcar_error()
                    continue

                estado = resultado.get('estado')
                medicion = resultado.get('medicion')
                if medicion is not None:
                    mediciones.append(medicion)

                if estado == 'cancelado':
                    interrumpido = True
                    evento_cancelacion.set()
                    break

                if estado == 'error':
                    errores += 1
                    err_msg = resultado.get('error', 'Error no especificado')
                    rr = str(ruta.resolve())
                    fallos_detalle.append((ruta.name, rr, err_msg))
                    _registrar_error_video(ruta.name, rr, err_msg)
                    dashboard.imprimir_mensaje(f'❌ {ruta.name[:40]} | ERROR: {err_msg}')
                    dashboard.marcar_error()
                    continue

                if estado != 'ok':
                    errores += 1
                    err_msg = 'Estado no reconocido'
                    rr = str(ruta.resolve())
                    fallos_detalle.append((ruta.name, rr, err_msg))
                    _registrar_error_video(ruta.name, rr, err_msg)
                    dashboard.imprimir_mensaje(f'❌ {ruta.name[:40]} | ERROR desconocido')
                    dashboard.marcar_error()
                    continue

                if evento_cancelacion.is_set():
                    interrumpido = True
                    break

                fp = resultado['fingerprint']
                conn = conectar(args.db)
                try:
                    video_id = guardar_fingerprint(conn, fp)
                finally:
                    cerrar(conn)

                if video_id is None:
                    errores += 1
                    err_msg = 'Fallo al insertar registros en la base de datos'
                    rr = str(ruta.resolve())
                    fallos_detalle.append((ruta.name, rr, err_msg))
                    _registrar_error_video(ruta.name, rr, err_msg)
                    dashboard.imprimir_mensaje(f'❌ {ruta.name[:40]} | ERROR al guardar en BD')
                    dashboard.marcar_error()
                else:
                    procesados += 1
                    dashboard.marcar_completado()

    except KeyboardInterrupt:
        interrumpido = True
        evento_cancelacion.set()
    finally:
        signal.signal(signal.SIGINT, manejador_anterior)
        dashboard.detener()

    if interrumpido:
        imprimir('[yellow]Escaneo interrumpido por el usuario.[/]')
        imprimir('[grey37]Los vídeos incompletos no se guardaron. Los ya procesados están en la base.[/]')

    tiempo_total = time.monotonic() - inicio_total
    imprimir_resumen_escaneo(procesados=procesados, errores=errores, omitidos=omitidos, tiempo_seg=tiempo_total)

    if fallos_detalle:
        if CONSOLA is not None:
            CONSOLA.print(Rule(f'[bold red]Vídeos con error ({len(fallos_detalle)})[/]', style='red'))
            for nom, rta, err in fallos_detalle:
                CONSOLA.print(f'[red]•[/] [bold]{nom}[/]')
                CONSOLA.print(f'  [grey37]{rta}[/]')
                CONSOLA.print(f'  [yellow]{err}[/]')
        else:
            print(f'\n{ROJO}{NEGRITA}=== VÍDEOS CON ERROR ({len(fallos_detalle)}) ==={NC}')
            for nom, rta, err in fallos_detalle:
                print(f'{ROJO}• {NEGRITA}{nom}{NC}')
                print(f'{GRIS}  {rta}{NC}')
                print(f'{AMARILLO}  {err}{NC}\n')

    if mediciones:
        try:
            from core.recursos import guardar_reporte_txt, imprimir_resumen_consola
            nombre = f'recursos_{_id_utc6()}.txt'
            ruta = str(_carpeta_reportes() / 'recursos' / 'txt' / nombre)
            guardar_reporte_txt(mediciones, ruta)
            imprimir_resumen_consola(mediciones)
        except Exception as exc:
            imprimir(f'[yellow][AVISO] No se pudo generar el reporte de recursos: {exc}[/]')

    if getattr(args, 'resumen_json', None):
        _guardar_resumen_json(
            args.resumen_json, procesados, omitidos,
            [{'archivo': n, 'ruta': r, 'causa': c} for n, r, c in fallos_detalle],
            tiempo_total)

    notificar_fin('Escanear vídeos', errores == 0 and not interrumpido,
                  f'{procesados} procesados, {errores} errores, {omitidos} omitidos')

    return 130 if interrumpido else 0


def _cpu_threads_recomendados() -> int:
    if es_android():
        return 1
    try:
        count = os.cpu_count() or 4
        return max(1, int(count * 0.75))
    except Exception:
        return 1


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


def _validar_umbrales(args: argparse.Namespace) -> bool:
    if not 0 <= args.umbral_phash <= 64:
        imprimir('[red][ERROR] La distancia pHash debe estar entre 0 y 64.[/]')
        return False
    porcentajes = {
        'El umbral visual': args.umbral_visual,
        'El umbral de fragmento': args.umbral_fragmento,
        'El umbral de posible fragmento': args.umbral_posible_fragmento,
        'El margen de duración': args.margen_duracion,
        'La proporción mínima': args.ratio_min,
    }
    for nombre, valor in porcentajes.items():
        if not math.isfinite(valor) or valor < 0 or valor > 100:
            imprimir(f'[red][ERROR] {nombre} debe estar entre 0 y 100.[/]')
            return False
    if args.umbral_posible_fragmento > args.umbral_fragmento:
        imprimir('[red][ERROR] El umbral de posible fragmento no puede superar el de fragmento confirmado.[/]')
        return False
    return True


def cmd_comparar(args: argparse.Namespace) -> int:
    from core.comparator import comparar_todos
    from core.database import _usa_neon, cerrar, conectar, importar_json, obtener_todos, info_dispositivo_actual, describir_conexion
    from core.reporter import BarraProgreso, construir_informe, exportar_csv, exportar_json, exportar_txt, imprimir_reporte

    if not _validar_umbrales(args):
        return 2

    notificar_inicio('Comparar bases de datos', f'Base principal: {args.db}')

    if not _usa_neon() and not Path(args.db).is_file():
        imprimir(f'[red][ERROR] No existe la base de datos: {args.db}[/]')
        return 1

    es_json_principal = not _usa_neon() and Path(args.db).suffix.lower() == '.json'
    conn = conectar(':memory:' if es_json_principal else args.db)
    try:
        if es_json_principal:
            conteo = importar_json(conn, args.db)
            if conteo['errores']:
                imprimir('[red][ERROR] La base JSON tiene registros inválidos.[/]')
                return 1
        videos = obtener_todos(conn)
    finally:
        cerrar(conn)

    if args.db_extra:
        for archivo_extra in args.db_extra:
            if not Path(archivo_extra).is_file():
                imprimir(f'[red][ERROR] No existe: {archivo_extra}[/]')
                return 1
            es_json = Path(archivo_extra).suffix.lower() == '.json'
            destino_extra = ':memory:' if es_json else archivo_extra
            conn_extra = conectar(destino_extra)
            try:
                if es_json:
                    conteo = importar_json(conn_extra, archivo_extra, omitir_existentes=True)
                    imprimir(f'[cyan]{archivo_extra}: {conteo["importados"]} importados; {conteo["errores"]} errores[/]')
                    if conteo['errores']:
                        imprimir('[red][ERROR] La base JSON contiene registros inválidos.[/]')
                        return 1
                adicionales = obtener_todos(conn_extra)
            finally:
                cerrar(conn_extra)
            videos.extend(adicionales)
            imprimir(f'[cyan]Cargados de {archivo_extra}: {len(adicionales)} vídeos[/]')

    unicos, claves = [], set()
    for fp in videos:
        # Una ruta idéntica de dos equipos diferentes representa dos vídeos.
        try:
            st = Path(fp.ruta).stat()
            clave = ('archivo', fp.dispositivo_id, st.st_dev, st.st_ino) if st.st_ino else ('ruta', fp.dispositivo_id, fp.ruta)
        except (OSError, ValueError):
            clave = ('ruta', fp.dispositivo_id, fp.ruta)
        if clave not in claves:
            claves.add(clave)
            unicos.append(fp)
    videos = unicos

    if len(videos) < 2:
        imprimir('[red]Se necesitan al menos dos vídeos con huella.[/]')
        return 1

    intervalos = {fp.frame_intervalo for fp in videos}
    if len(intervalos) > 1:
        imprimir(f'[yellow][AVISO] Huellas con diferentes intervalos: {sorted(intervalos)}. Solo comparables mediante SHA256 exacto.[/]')

    total_pares = len(videos) * (len(videos) - 1) // 2
    info_disp = info_dispositivo_actual()

    if CONSOLA is not None:
        tabla = Table(box=rich_box.ROUNDED, border_style='cyan', show_header=False)
        tabla.add_column(style='cyan'); tabla.add_column(style='white')
        tabla.add_row('Dispositivo', f'{info_disp["dispositivo"]} (ID {info_disp["dispositivo_id"]})')
        tabla.add_row('Modo conexión', describir_conexion())
        tabla.add_row('Vídeos a comparar', str(len(videos)))
        tabla.add_row('Pares a evaluar', str(total_pares))
        CONSOLA.print(tabla)
    else:
        print(f'\n Dispositivo actual: {info_disp["dispositivo"]} (ID {info_disp["dispositivo_id"]})')
        print(f' Modo de conexión  : {describir_conexion()}')
        print(f' Vídeos a comparar: {len(videos)}')
        print(f' Pares a evaluar : {total_pares}\n')

    barra = BarraProgreso(total=total_pares, total_videos=len(videos))
    resumen_comparativas = {}
    inicio = time.monotonic()

    try:
        resultados = comparar_todos(
            videos,
            callback_progreso=barra.actualizar,
            callback_videos=barra.actualizar_videos,
            resumen=resumen_comparativas,
            umbral_phash=args.umbral_phash,
            umbral_visual=args.umbral_visual / 100,
            umbral_fragmento=args.umbral_fragmento / 100,
            umbral_posible_fragmento=args.umbral_posible_fragmento / 100,
            margen_dur=args.margen_duracion / 100,
            ratio_min=args.ratio_min / 100,
        )
    finally:
        barra.limpiar()

    duracion = time.monotonic() - inicio
    carpeta = _carpeta_reportes() / 'comparativas'
    for sub in ('json', 'csv', 'txt'):
        (carpeta / sub).mkdir(parents=True, exist_ok=True)
    
    identificador = _id_utc6()
    base_nombre = f'comparacion_{identificador}'

    informe = construir_informe(resultados, len(videos), duracion, metadatos={
        'bases_de_datos': [args.db] + list(args.db_extra or []),
        'comparativas': resumen_comparativas,
        'umbrales': {k: getattr(args, k) for k in (
            'umbral_phash', 'umbral_visual', 'umbral_fragmento',
            'umbral_posible_fragmento', 'margen_duracion', 'ratio_min')},
        'dispositivo': info_disp,
        'conexion': describir_conexion(),
    })

    ruta_json = carpeta / 'json' / f'{base_nombre}.json'
    ruta_csv = carpeta / 'csv' / f'{base_nombre}.csv'
    ruta_txt = carpeta / 'txt' / f'{base_nombre}.txt'

    exportar_txt(informe, str(ruta_txt))
    exportar_json(resultados, str(ruta_json), informe=informe)
    exportar_csv(resultados, str(ruta_csv), informe=informe)
    args.ultimo_reporte = str(ruta_json)
    imprimir(f'[spring_green2]Reportes guardados en: {carpeta}[/]')

    notificar_fin('Comparar bases de datos', True,
                  f'{len(resultados)} hallazgos en {_fmt_seg(duracion)}')
    return 0


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
             'green' if conteo['errores'] == 0 else 'yellow')
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


def _fmt_tamanio_estadisticas(numero: int) -> str:
    """Tamaño en unidades binarias, utilizado solamente por el menú 7."""
    numero = max(0, int(numero or 0))
    if numero >= 1024 ** 4:
        return f'{numero / (1024 ** 4):,.2f} TB'
    if numero >= 1024 ** 3:
        return f'{numero / (1024 ** 3):,.2f} GB'
    if numero >= 1024 ** 2:
        return f'{numero / (1024 ** 2):,.1f} MB'
    if numero >= 1024:
        return f'{numero / 1024:,.1f} KB'
    return f'{numero} B'


def _mostrar_desglose_estadisticas(dispositivos: List[Dict[str, Any]]) -> None:
    """Muestra dispositivos y carpetas, tanto con Rich como sin él."""
    if not dispositivos:
        imprimir('[yellow]No hay vídeos registrados para desglosar.[/]')
        return

    if CONSOLA is not None:
        resumen = Table(title='Vídeos registrados por dispositivo',
                        box=rich_box.ROUNDED, border_style='bright_magenta')
        resumen.add_column('Dispositivo', style='plum1', overflow='fold')
        resumen.add_column('ID', style='deep_sky_blue1', overflow='fold')
        resumen.add_column('Vídeos', justify='right', style='bright_white')
        resumen.add_column('Tamaño', justify='right', style='cyan')
        resumen.add_column('SHA256', justify='right', style='green')
        resumen.add_column('Visual', justify='right', style='gold1')
        resumen.add_column('Errores', justify='right', style='red')
        for disp in dispositivos:
            resumen.add_row(
                rich_escape(disp['dispositivo']),
                rich_escape(disp['dispositivo_id']) if disp['id_verificado']
                    else '[yellow]SIN ID[/]',
                f'{disp["total_videos"]:,}',
                _fmt_tamanio_estadisticas(disp['tamanio_bytes']),
                f'{disp["con_sha256"]:,}',
                f'{disp["con_huella_visual"]:,}',
                f'{disp["con_error"]:,}',
            )
        CONSOLA.print(resumen)

        for disp in dispositivos:
            nombre = rich_escape(disp['dispositivo'])
            identificador = (rich_escape(disp['dispositivo_id']) if disp['id_verificado']
                             else '[yellow]SIN ID; identidad no verificable[/]')
            CONSOLA.print()
            CONSOLA.print(f'[bold bright_cyan]{nombre}[/]  |  ID: {identificador}')
            CONSOLA.print(
                f'  [bright_white]{disp["total_videos"]:,} vídeos[/]  ·  '
                f'[cyan]{_fmt_tamanio_estadisticas(disp["tamanio_bytes"])}[/]  ·  '
                f'[green]SHA256: {disp["con_sha256"]:,}[/]  ·  '
                f'[gold1]Visual: {disp["con_huella_visual"]:,}[/]  ·  '
                f'[magenta]Audio: {disp["con_huella_audio"]:,}[/]  ·  '
                f'[red]Errores: {disp["con_error"]:,}[/]'
            )
            if len(disp['nombres_registrados']) > 1:
                otros = ', '.join(rich_escape(n) for n in disp['nombres_registrados'])
                CONSOLA.print(f'  [yellow]Nombres asociados a este ID:[/] {otros}')
            tabla_rutas = Table(title='Desglose por carpeta (sin incluir subcarpetas)',
                                box=rich_box.SIMPLE, border_style='cyan',
                                show_lines=False)
            tabla_rutas.add_column('Carpeta registrada', style='bright_white', overflow='fold')
            tabla_rutas.add_column('Vídeos', justify='right', style='green')
            tabla_rutas.add_column('Tamaño', justify='right', style='cyan')
            tabla_rutas.add_column('SHA256', justify='right', style='green')
            tabla_rutas.add_column('Visual', justify='right', style='gold1')
            for carpeta in disp['carpetas']:
                tabla_rutas.add_row(
                    rich_escape(carpeta['ruta']), f'{carpeta["total_videos"]:,}',
                    _fmt_tamanio_estadisticas(carpeta['tamanio_bytes']),
                    f'{carpeta["con_sha256"]:,}', f'{carpeta["con_huella_visual"]:,}',
                )
            CONSOLA.print(tabla_rutas)
    else:
        print('\n VÍDEOS REGISTRADOS POR DISPOSITIVO')
        print(' ' + '=' * 76)
        for disp in dispositivos:
            id_disp = disp['dispositivo_id'] if disp['id_verificado'] else 'SIN ID (no verificable)'
            print(f'\n Dispositivo : {disp["dispositivo"]} (ID {id_disp})')
            print(f' Vídeos      : {disp["total_videos"]:,}')
            print(f' Tamaño      : {_fmt_tamanio_estadisticas(disp["tamanio_bytes"])}')
            print(f' SHA256      : {disp["con_sha256"]:,}')
            print(f' Visual      : {disp["con_huella_visual"]:,}')
            print(f' Audio       : {disp["con_huella_audio"]:,}')
            print(f' Con error   : {disp["con_error"]:,}')
            if len(disp['nombres_registrados']) > 1:
                print(f' Nombres de este ID: {", ".join(disp["nombres_registrados"])}')
            print('  Carpetas registradas (solo vídeos directamente contenidos):')
            for carpeta in disp['carpetas']:
                print(f'    {carpeta["ruta"]}  ->  {carpeta["total_videos"]:,} vídeos; '
                      f'{_fmt_tamanio_estadisticas(carpeta["tamanio_bytes"])}; '
                      f'SHA256 {carpeta["con_sha256"]:,}; visual {carpeta["con_huella_visual"]:,}')
        print()


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

    if CONSOLA is not None:
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
        if CONSOLA is not None:
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


def _seleccionar_base_lectura(db_defecto: str, titulo: str = 'Base de datos SQLite') -> str:
    from core.database import _usa_neon
    if _usa_neon():
        return 'neon_cloud'
    return elegir_archivo_reciente(titulo, ['*.db'], ['respaldos/db'], defecto=db_defecto)


def gestionar_exclusiones() -> None:
    """Configuración persistente de exclusiones por ID, sin modificar su identidad."""
    from core.database import info_dispositivo_actual
    from core import inventario
    ident = info_dispositivo_actual()
    device_id = ident['dispositivo_id']
    while True:
        actuales = inventario.cargar_exclusiones(DIR_SCRIPT, device_id)
        imprimir(f'\n[cyan]Carpetas excluidas en {rich_escape(ident["dispositivo"])} '
                 f'(ID {rich_escape(device_id)}):[/]')
        for i, ruta in enumerate(actuales, 1):
            imprimir(f'  [gold1]{i}.[/] {rich_escape(ruta)}')
        if not actuales:
            imprimir('  [grey70](ninguna)[/]')
        imprimir('  [green]A.[/] Añadir carpeta  [yellow]Q.[/] Quitar carpeta  [cyan]00.[/] Volver')
        opcion = pedir_texto('Elegir operación', '00').lower()
        if opcion in ('00', '0'):
            return
        if opcion == 'a':
            ruta = pedir_texto('Carpeta a excluir (ruta absoluta)', obligatorio=True)
            # No normalizar con Path.resolve(): puede tratarse de una ruta Windows.
            if not (Path(ruta).is_absolute() or inventario.es_windows(ruta)):
                imprimir('[red]Escribe una ruta absoluta, no relativa.[/]')
                continue
            if inventario.normalizar(ruta) in {inventario.normalizar(x) for x in actuales}:
                imprimir('[yellow]Esa carpeta ya está excluida.[/]')
                continue
            inventario.guardar_exclusiones(DIR_SCRIPT, device_id, [*actuales, ruta])
            imprimir('[green]Exclusión guardada (también se aplica a SHA256).[/]')
        elif opcion == 'q' and actuales:
            valor = int(pedir_numero('Número de exclusión a quitar', 1, 1, len(actuales), entero=True))
            if pedir_si_no(f'¿Quitar la exclusión {actuales[valor - 1]}?', False):
                inventario.guardar_exclusiones(DIR_SCRIPT, device_id,
                                             [r for i, r in enumerate(actuales) if i != valor - 1])
                imprimir('[green]Exclusión retirada.[/]')


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


def submenu_estadisticas(db_defecto: str) -> str:
    """Menú 7: consultar, cotejar rutas bidireccionalmente y exportar."""
    from core.database import cerrar, conectar, estadisticas, estadisticas_por_dispositivo
    from core.database import info_dispositivo_actual
    from core import inventario
    while True:
        _cabecera_submenu('Estadísticas e inventario')
        imprimir('  [gold1]1.[/] Mostrar estadísticas generales, por dispositivo y ruta')
        imprimir('  [gold1]2.[/] Comprobar disco ↔ base de datos (ambas direcciones)')
        imprimir('  [gold1]3.[/] Exportar estadísticas a TXT, JSON y CSV')
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
        elif opcion == '3':
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
        elif opcion == '2':
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
                continue
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


def _cabecera_submenu(titulo: str) -> None:
    from core.database import describir_conexion, info_dispositivo_actual
    info = info_dispositivo_actual()
    if CONSOLA is not None:
        CONSOLA.print(Rule(f'[bold plum1]{titulo}[/]', style='bright_cyan'))
        CONSOLA.print(
            f'[deep_sky_blue1]Dispositivo:[/] {info["dispositivo"]}  [grey37](ID {info["dispositivo_id"]})[/]\n'
            f'[deep_sky_blue1]Plataforma :[/] {info["plataforma"]} ({info["maquina"]})\n'
            f'[deep_sky_blue1]Conexión   :[/] {describir_conexion()}\n'
            f'[deep_sky_blue1]Telegram   :[/] {"[green]activo[/]" if telegram_configurado() else "[yellow]desactivado[/]"}'
        )
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


def submenu_mover(db_defecto: str) -> str:
    from core.mover import (mover_interactivo, mover_desde_sha256,
                            eliminar_desde_comparativa, eliminar_desde_sha256)
    from core.sha256 import ultimo_reporte_comparacion as _ult

    while True:
        _cabecera_submenu('Mover vídeos')
        imprimir('\n  [gold1]1.[/] Mover vídeos de una comparativa (visual)')
        imprimir('  [gold1]2.[/] Mover nuevos/ya registrados desde JSON SHA256')
        imprimir('  [bold red]3.[/] Eliminar duplicados EXACTOS locales desde comparativa')
        imprimir('  [bold red]4.[/] Eliminar coincidencias SHA256 locales')
        imprimir('  [gold1]00.[/] Volver al menú principal\n')
        opcion = _pedir_opcion('¿Qué quieres hacer? (1-4 / 00)', ('1', '2', '3', '4', '00', '0'), '1')
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
                mover_interactivo(ruta_reporte, destino, _carpeta_reportes() / 'movimientos')
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
                mover_desde_sha256(ruta_json, destino, _carpeta_reportes() / 'movimientos', grupos=grupos)
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
        imprimir('  [gold1]00.[/] Volver al menú principal\n')
        opcion = _pedir_opcion('¿Qué quieres hacer? (1-4 / 00)', ('1', '2', '3', '4', '00', '0'), '1')
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
        except RegresarAlMenu:
            imprimir('[yellow]Operación cancelada.[/]')
        except Exception as exc:
            imprimir(f'[red][ERROR] {exc}[/]')
    return db_defecto


def submenu_depurar(db_defecto: str) -> str:
    from core.database import _usa_neon, cerrar, conectar, estadisticas, info_dispositivo_actual

    while True:
        _cabecera_submenu('Depurar base de datos')
        imprimir('\n  [gold1]1.[/] Eliminar una huella por ruta')
        imprimir('  [gold1]2.[/] Eliminar huellas por ID de dispositivo')
        imprimir('  [gold1]3.[/] Reasignar huellas de un ID antiguo a este dispositivo')
        imprimir('  [gold1]4.[/] Eliminar registros por dispositivo y carpeta, con respaldo')
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

                if CONSOLA is not None:
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


def submenu_sha256(db_defecto: str) -> str:
    from core.sha256 import menu as menu_sha256
    salida = menu_sha256(DIR_SCRIPT, db_defecto)
    if salida:
        resultado = salida['resultado']
        ruta_db = salida.get('ruta_db')
        if ruta_db and not str(ruta_db).startswith('N/A') and Path(ruta_db).suffix.lower() != '.json':
            db_defecto = ruta_db
        if resultado.get('modo') == 'comparar' and resultado.get('total_nuevos') and pedir_si_no(
                '¿Escanear ahora únicamente los vídeos sin coincidencias?', False):
            db_defecto = escanear_interactivo(db_defecto, salida['ruta_json'])
    return db_defecto


def escanear_interactivo(db_defecto: str, reporte_sha: Optional[str] = None) -> str:
    from core.database import _usa_neon

    if not _verificar_dependencias_escaneo():
        return db_defecto

    if pedir_si_no('¿Configurar las carpetas excluidas antes de escanear?', False):
        gestionar_exclusiones()

    if reporte_sha:
        from core.sha256 import cargar_pendientes
        datos, candidatos, _, avisos = cargar_pendientes(reporte_sha)
        for aviso in avisos:
            imprimir(f'[yellow][AVISO] {aviso}[/]')
        if not candidatos:
            imprimir('[yellow]Ese reporte no contiene vídeos nuevos disponibles para escanear.[/]')
            return db_defecto
        referencia = datos.get('ruta_db', '')
        sugerida = referencia if referencia and Path(referencia).suffix.lower() != '.json' else db_defecto
        directorio = None
        recursivo = False
        imprimir(f'[cyan]Vídeos nuevos disponibles en el JSON: {len(candidatos)}[/]')
    else:
        candidatas = _carpetas_android_candidatas() if es_android() else []
        if candidatas:
            imprimir('\n[cyan]Carpetas de vídeo detectadas en este dispositivo:[/]')
            for i, c in enumerate(candidatas, 1):
                imprimir(f'  [gold1]{i}.[/] {rich_escape(str(c))}')
            imprimir(f'  [gold1]{len(candidatas) + 1}.[/] Otra carpeta (ruta manual)')
            opcion = pedir_numero(
                'Elige una carpeta',
                1, 1, len(candidatas) + 1, entero=True,
            )
            if opcion == len(candidatas) + 1:
                directorio = pedir_ruta(
                    'Directorio de vídeos', str(DIR_SCRIPT),
                    existente=True, directorio=True,
                )
            else:
                directorio = str(candidatas[int(opcion) - 1])
            recursivo = pedir_si_no('¿Incluir subcarpetas?', True)
        else:
            directorio = pedir_ruta(
                'Directorio de vídeos', str(DIR_SCRIPT),
                existente=True, directorio=True,
            )
            recursivo = pedir_si_no('¿Incluir subcarpetas?', False)
        sugerida = db_defecto  

    if _usa_neon():
        db = 'neon_cloud'
    else:
        if Path(sugerida).suffix.lower() == '.json':
            sugerida = _base_reciente_por_defecto()
        
        db_sug = sugerida

        db = elegir_archivo_reciente(
            'Base de datos SQLite donde guardar las huellas',
            ['*.db'],
            ['respaldos/db'],
            defecto=db_sug,
        )
        if Path(db).suffix.lower() == '.json':
            raise ValueError('El escaneo guarda huellas en SQLite; selecciona una base .db.')

    forzar = pedir_si_no('¿Volver a analizar vídeos registrados y vídeos con errores?', False)
    limite = pedir_numero('Máximo de vídeos en este lote (0 = todos)', 0, entero=True)
    trabajadores = pedir_numero('Vídeos simultáneos (trabajadores)', _cpu_threads_recomendados(), 1, entero=True)
    intervalo = 10.0
    hilos = 2
    aceleracion = 'auto'
    if pedir_si_no('¿Configurar intervalo, hilos y aceleración?', False):
        intervalo = pedir_numero('Segundos entre cuadros', 10, 0.001)
        hilos = pedir_numero('Hilos de FFmpeg por trabajador', 2, 1, entero=True)
        imprimir('[cyan]Aceleración: amf, qsv, d3d11va, dxva2, vaapi, cuda, mediacodec, videotoolbox, vulkan, software, auto[/]')
        permitidos = {'amf', 'qsv', 'd3d11va', 'dxva2', 'vaapi', 'cuda',
                      'mediacodec', 'videotoolbox', 'vulkan', 'software', 'auto'}
        while True:
            aceleracion = pedir_texto('Aceleración', 'auto').lower()
            if aceleracion in permitidos:
                break
            imprimir('[yellow]Elige uno de los modos indicados.[/]')

    resumen_json = _carpeta_reportes() / 'escaneos' / 'json' / f'resumen_escaneo_{_id_utc6()}.json'
    args = argparse.Namespace(
        db=db, directorio=directorio, carpetas_sub=recursivo, forzar=forzar,
        max_videos=limite or 2**63 - 1, trabajadores=trabajadores,
        intervalo=intervalo, hilos_ffmpeg=1, aceleracion='software',
        medir_recursos=True, manifiesto_sha=reporte_sha, resumen_json=str(resumen_json),
    )
    cmd_escanear(args)
    return db


def _verificar_dependencias_escaneo() -> bool:
    return True


def opciones_comparacion() -> dict:
    valores = dict(umbral_phash=10, umbral_visual=80.0, umbral_fragmento=75.0,
                   umbral_posible_fragmento=25.0, margen_duracion=5.0, ratio_min=5.0)
    if pedir_si_no('¿Personalizar los umbrales de comparación?', False):
        valores['umbral_phash'] = pedir_numero('Distancia máxima pHash', 10, 0, 64, entero=True)
        for clave, titulo in [
            ('umbral_visual', 'Similitud visual mínima (%)'),
            ('umbral_fragmento', 'Similitud de fragmento mínima (%)'),
            ('umbral_posible_fragmento', 'Similitud de posible fragmento mínima (%)'),
            ('margen_duracion', 'Margen de duración (%)'),
            ('ratio_min', 'Proporción mínima de duración del fragmento (%)'),
        ]:
            limite = valores['umbral_fragmento'] if clave == 'umbral_posible_fragmento' else 100
            valores[clave] = pedir_numero(titulo, min(valores[clave], limite), 0, limite)
    return valores


def comparar_interactivo(db_defecto: str) -> str:
    from core.database import _usa_neon
    if _usa_neon():
        db = 'neon_cloud'
    else:
        db = elegir_archivo_reciente(
            'Base principal SQLite o JSON de huellas',
            ['*.db', '*.json'],
            ['respaldos/db', 'exportaciones/json'],
            defecto=db_defecto,
        )
    extras: List[str] = []
    while pedir_si_no('¿Agregar otra base SQLite o JSON?', False):
        extra = pedir_ruta('Base adicional (.db o .json)', existente=True)
        if extra not in [db] + extras:
            extras.append(extra)
        else:
            imprimir('[yellow]Esa base ya está seleccionada.[/]')
    args = argparse.Namespace(db=db, db_extra=extras, **opciones_comparacion())
    cmd_comparar(args)
    return db


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
        if CONSOLA is not None:
            CONSOLA.print()
            CONSOLA.print(Panel.fit(
                f'[bold bright_white]VIDUP — Menú principal[/]\n'
                f'[deep_sky_blue1]Dispositivo:[/] {info_disp["dispositivo"]}\n'
                f'[deep_sky_blue1]ID         :[/] {info_disp["dispositivo_id"]}\n'
                f'[deep_sky_blue1]Plataforma :[/] {info_disp["plataforma"]} ({info_disp["maquina"]})\n'
                f'[deep_sky_blue1]Conexión   :[/] {describir_conexion()}\n'
                f'[deep_sky_blue1]Telegram   :[/] {"[green]activo[/]" if telegram_configurado() else "[yellow]desactivado[/]"}',
                title='[bold plum1]VIDUP[/]',
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
            tabla.add_row('6', 'Operaciones SHA256')
            tabla.add_row('7', 'Estadísticas, verificación bidireccional y reportes')
            tabla.add_row('8', 'Probar Telegram')
            tabla.add_row('0', 'Salir')
            CONSOLA.print(tabla)
        else:
            print(f'\n{MAGENTA}{NEGRITA}VIDUP — Menú principal{NC}')
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
            print('  6. Operaciones SHA256')
            print('  7. Estadísticas, verificación bidireccional y reportes')
            print('  8. Probar Telegram')
            print('  0. Salir')

        try:
            opcion = pedir_texto('¿Qué quieres hacer?', '0', permitir_escape=False).strip().lower()
        except RegresarAlMenu:
            continue

        if opcion in _PALABRAS_SALIR:
            return 0

        try:
            if opcion == '1':
                db_defecto = escanear_interactivo(db_defecto)
            elif opcion == '2':
                db_defecto = comparar_interactivo(db_defecto)
            elif opcion == '3':
                db_defecto = submenu_mover(db_defecto)
            elif opcion == '4':
                db_defecto = submenu_respaldos(db_defecto)
            elif opcion == '5':
                db_defecto = submenu_depurar(db_defecto)
            elif opcion == '6':
                db_defecto = submenu_sha256(db_defecto)
            elif opcion == '7':
                db_defecto = submenu_estadisticas(db_defecto)
            elif opcion == '8':
                probar_telegram()
            else:
                imprimir('[yellow]Elige una opción entre 0 y 8.[/]')
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
    return argparse.ArgumentParser(prog='vidup', description=__doc__)


def main(argv: Optional[Sequence[str]] = None) -> int:
    if os.name == 'nt':
        os.system('')
    construir_parser().parse_args(argv)

    try:
        from core.database import _detectar_modo_conexion_una_vez
        _detectar_modo_conexion_una_vez()
    except Exception:
        pass

    if not verificar_dependencias_obligatorias():
        return 1

    set_high_priority()

    try:
        return menu_interactivo()
    except (KeyboardInterrupt, EOFError):
        imprimir('\n[cyan]VIDUP finalizado.[/]')
        return 0


if __name__ == '__main__':
    sys.exit(main())
