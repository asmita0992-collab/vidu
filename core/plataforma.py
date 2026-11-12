#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Detección de plataforma, sistema operativo y entorno."""

from __future__ import annotations

from typing import List, Optional, Sequence
from core.presentacion import imprimir

import os
import sys
import platform
import re
import shutil
import subprocess
from pathlib import Path


def es_android() -> bool:
    """Detecta si se ejecuta en Android/Termux."""
    if os.name == 'nt':
        return False
    if os.environ.get('ANDROID_ROOT') or os.environ.get('ANDROID_DATA'):
        return True
    if 'com.termux' in os.environ.get('PREFIX', ''):
        return True
    if 'com.termux' in os.environ.get('PATH', ''):
        return True
    try:
        return Path('/data/data/com.termux').exists()
    except Exception:
        return False


def detectar_so() -> str:
    """Retorna el SO detectado: 'windows', 'macos', 'android' o 'linux'."""
    if os.name == 'nt':
        return 'windows'
    if sys.platform.startswith('darwin'):
        return 'macos'
    if es_android():
        return 'android'
    return 'linux'


def so_legible() -> str:
    """Retorna el nombre legible del SO."""
    return {
        'windows': 'Windows',
        'macos': 'macOS',
        'android': 'Android/Termux',
        'linux': 'Linux',
    }.get(detectar_so(), sys.platform)


def distro_linux() -> str:
    """Retorna la distribución de Linux detectada desde /etc/os-release."""
    try:
        with open('/etc/os-release', 'r', encoding='utf-8') as f:
            for linea in f:
                if linea.startswith('ID='):
                    return linea.split('=', 1)[1].strip().strip('"').lower()
    except OSError:
        pass
    return 'desconocida'


def es_windows() -> bool:
    """Retorna True si se ejecuta en Windows."""
    return detectar_so() == 'windows'


def es_macos() -> bool:
    """Retorna True si se ejecuta en macOS."""
    return detectar_so() == 'macos'


def es_linux() -> bool:
    """Retorna True si se ejecuta en Linux."""
    return detectar_so() == 'linux'


def arquitectura_maquina() -> str:
    """Retorna la arquitectura de la máquina (arquitectura del procesador)."""
    return platform.machine()


OPCIONES_PRIORIDAD = {
    '1': {
        'nombre': 'HIGH_PRIORITY_CLASS',
        'etiqueta': 'Alta (HIGH_PRIORITY_CLASS / nice -10)',
        'win': 'HIGH_PRIORITY_CLASS',
        'win_val': 0x8000,
        'unix_nice': -10,
    },
    '2': {
        'nombre': 'ABOVE_NORMAL_PRIORITY_CLASS',
        'etiqueta': 'Por encima de lo normal (ABOVE_NORMAL_PRIORITY_CLASS / nice -5)',
        'win': 'ABOVE_NORMAL_PRIORITY_CLASS',
        'win_val': 0x80,
        'unix_nice': -5,
    },
    '3': {
        'nombre': 'NORMAL_PRIORITY_CLASS',
        'etiqueta': 'Normal (NORMAL_PRIORITY_CLASS / nice 0)',
        'win': 'NORMAL_PRIORITY_CLASS',
        'win_val': 0x20,
        'unix_nice': 0,
    },
    '4': {
        'nombre': 'PROCESS_MODE_BACKGROUND_BEGIN',
        'etiqueta': 'Segundo plano (PROCESS_MODE_BACKGROUND_BEGIN / IDLE / nice 10)',
        'win': 'IDLE_PRIORITY_CLASS',
        'win_val': 0x40,
        'unix_nice': 10,
    },
}


def obtener_prioridad_configurada() -> str:
    """Retorna la clave o nombre de prioridad desde el entorno o .env."""
    return os.environ.get('VIDUP_PRIORIDAD', 'ABOVE_NORMAL_PRIORITY_CLASS').strip()


def aplicar_prioridad(nivel: Optional[str] = None) -> str:
    """Aplica la prioridad indicada o la configurada en .env según el SO.

    Soporta Windows, Linux, Ubuntu/Xubuntu, Android y macOS.
    """
    if not nivel:
        nivel = obtener_prioridad_configurada()

    cfg = None
    for item in OPCIONES_PRIORIDAD.values():
        if nivel == item['nombre'] or nivel == item['win'] or nivel == str(item['unix_nice']):
            cfg = item
            break
    if not cfg:
        cfg = OPCIONES_PRIORIDAD['2']

    try:
        sistema = platform.system().lower()

        if sistema == 'windows':
            try:
                import psutil
                val = getattr(psutil, cfg['win'], cfg['win_val'])
                psutil.Process(os.getpid()).nice(val)
                imprimir(f'[spring_green2]✓ Prioridad del proceso ajustada a {cfg["win"]} en Windows.[/]')
            except (ImportError, OSError, PermissionError, AttributeError):
                pass
        elif sistema in {'linux', 'android', 'darwin', 'freebsd', 'openbsd', 'netbsd'}:
            nice = getattr(os, 'nice', None)
            if nice is not None:
                try:
                    target_nice = cfg['unix_nice']
                    nice(target_nice)
                    imprimir(f'[spring_green2]✓ Prioridad del proceso ajustada a nice({target_nice}) en {so_legible()}.[/]')
                except (OSError, PermissionError):
                    pass
    except Exception:
        pass

    return cfg['nombre']


def set_high_priority() -> None:
    """Alias para aplicar la prioridad configurada al iniciar VIDUP."""
    aplicar_prioridad()

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
# Raíces externas y acceso elevado opcional para búsquedas de solo lectura
# ============================================================================
def raices_android_almacenamiento(avisos: Optional[List[str]] = None,
                                 sugeridas: Sequence[str] = ()) -> List[str]:
    """Localiza raíces de memoria interna/SD sin depender de listar /storage.

    Comprueba las rutas explícitas primero; luego usa variables de Android,
    enlaces de Termux, puntos de montaje y, por último, /storage. No abre
    archivos ni necesita root. Una raíz denegada se conserva como candidata
    para que la interfaz informe del permiso y ofrezca una vía alternativa.
    """
    if not es_android():
        return []
    candidatas: List[str] = []
    vistos = set()

    def agregar(cadena: str) -> None:
        if not cadena:
            return
        ruta = str(cadena).strip().rstrip('/')
        if not ruta or ruta in ('/storage', '/mnt', '/mnt/media_rw'):
            return
        if ruta not in vistos:
            vistos.add(ruta)
            candidatas.append(ruta)

    for ruta in sugeridas:
        agregar(str(ruta))
    agregar('/storage/emulated/0')
    agregar(os.environ.get('EXTERNAL_STORAGE', ''))
    for ruta in os.environ.get('SECONDARY_STORAGE', '').split(':'):
        agregar(ruta)

    # Termux instala enlaces que pueden funcionar aunque /storage no se liste.
    termux_storage = Path.home() / 'storage'
    for nombre in ('shared', 'external-0', 'external-1', 'external-2'):
        enlace = termux_storage / nombre
        if enlace.exists() or enlace.is_symlink():
            agregar(str(enlace))

    # Android suele publicar el UUID en mountinfo incluso con /storage inaccesible.
    try:
        with open('/proc/self/mountinfo', encoding='utf-8') as montajes:
            for linea in montajes:
                partes = linea.split()
                if len(partes) < 5:
                    continue
                punto = (partes[4].replace(r'\040', ' ')
                                  .replace(r'\011', '\t')
                                  .replace(r'\134', '\\'))
                if re.fullmatch(r'/storage/[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}', punto):
                    agregar(punto)
                elif re.fullmatch(r'/mnt/media_rw/[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}', punto):
                    agregar('/storage/' + punto.rsplit('/', 1)[-1])
                    agregar(punto)
    except OSError as exc:
        if avisos is not None:
            avisos.append(f'No se pudo inspeccionar mountinfo: {exc}')

    try:
        for carpeta in ('/storage', '/mnt/media_rw'):
            try:
                for hijo in Path(carpeta).iterdir():
                    if re.fullmatch(r'[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}', hijo.name):
                        agregar(str(hijo))
            except (OSError, PermissionError) as exc:
                if avisos is not None:
                    avisos.append(f'No se pudo listar {carpeta}: {exc}')
    except Exception:
        pass
    return candidatas


def comando_lectura_elevada() -> Optional[List[str]]:
    """Indica la forma de solicitar lectura elevada, SIN pedir autorización.

    No invoca su/sudo: la interfaz debe explicar el alcance y preguntar
    expresamente al usuario antes de llamar a una comprobación de acceso.
    """
    if os.name == 'nt':
        return None
    if os.geteuid() == 0:
        return []
    if es_android():
        programa = shutil.which('su')
        return [programa, '-c'] if programa else None
    programa = shutil.which('sudo')
    return [programa, '-n', 'sh', '-c'] if programa else None


def validar_lectura_elevada(comando: Sequence[str], timeout: int = 12) -> bool:
    """Comprueba acceso root tras una autorización explícita del usuario.

    En Android `su` puede mostrar el cuadro de autorización de Magisk; en
    Linux se usa sudo -n sin pedir ni almacenar contraseñas del usuario.
    """
    if comando == []:
        return True
    if not comando:
        return False
    try:
        p = subprocess.run([*comando, 'id -u'], capture_output=True,
                           text=True, timeout=timeout, check=False)
        return p.returncode == 0 and p.stdout.strip() == '0'
    except (OSError, subprocess.TimeoutExpired):
        return False
