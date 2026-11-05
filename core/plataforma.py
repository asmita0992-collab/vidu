#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Detección de plataforma, sistema operativo y entorno."""

from __future__ import annotations

from typing import List
from core.presentacion import imprimir

import os
import sys
import platform
from pathlib import Path


def es_android() -> bool:
    """Detecta si se ejecuta en Android/Termux."""
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


def set_high_priority() -> None:
    """Intenta mejorar la prioridad sin hacerla un requisito del programa.

    La operación puede no estar disponible en Termux, iOS, contenedores o
    cuentas sin privilegios; en esos casos VIDUP debe continuar normalmente.
    """
    try:
        sistema = platform.system().lower()

        if sistema == 'windows':
            try:
                import psutil
                # El valor de ABOVE_NORMAL_PRIORITY_CLASS es 0x80; algunas versiones
                # o stubs de psutil no exponen la constante en el módulo.
                prioridad_alta = getattr(psutil, 'ABOVE_NORMAL_PRIORITY_CLASS', 0x80)
                psutil.Process(os.getpid()).nice(prioridad_alta)
                imprimir('[spring_green2]✓ Prioridad del proceso elevada a ABOVE_NORMAL_PRIORITY_CLASS.[/]')
            except (ImportError, OSError, PermissionError, AttributeError):
                pass
        elif sistema in {'linux', 'android', 'darwin', 'freebsd', 'openbsd', 'netbsd'}:
            # os.nice existe en Unix, pero puede estar ausente o bloqueado.
            nice = getattr(os, 'nice', None)
            if nice is None:
                return
            try:
                nice(-5)
                imprimir('[spring_green2]✓ nice() ajustado a -5.[/]')
            except (OSError, PermissionError):
                pass
    except Exception:
        # La prioridad es solo una optimización; nunca debe impedir el inicio.
        pass

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
