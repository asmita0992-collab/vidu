#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""SHA256 de carpetas MP4, integrado en el menú de VIDUP.

Permite generar hashes fuera de línea, compararlos contra bases SQLite/Neon/JSON
o importar huellas mínimas sin necesidad de recalcular los SHA256.

Escritura incremental:
  - Cada operación guarda un .jsonl de progreso en reportes/sha256/jsonl/.
  - Si se interrumpe con Ctrl+C, el .jsonl se conserva.
  - Al re-ejecutar la misma operación, se salta automáticamente lo ya procesado.
  - Al terminar correctamente, se consolida en .json/.txt/.csv y se borra el .jsonl.

Los reportes JSON, TXT y CSV se guardan en:
  reportes/sha256/json/, reportes/sha256/txt/, reportes/sha256/csv/
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple, Any


ZONA_MEXICO = timezone(timedelta(hours=-6))

def _ahora_utc6_str() -> str:
    return datetime.now(ZONA_MEXICO).strftime('%Y-%m-%d %H:%M:%S')

def _id_utc6() -> str:
    return datetime.now(ZONA_MEXICO).strftime('%Y%m%d_%H%M%S')

# Permite ejecutar como python -m core.sha256
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import database
from core.fingerprint import VideoFingerprint
from core.metadata import extraer_metadatos

# Importa el detector de carpetas Android
try:
    # Primero intenta desde vidup (para compatibilidad)
    from core.plataforma import _carpetas_android_candidatas
except ImportError:
    def _carpetas_android_candidatas():
        return []

TIENE_VIDUP = True

# Mantiene las extensiones de vídeo compatibles con el escaneo de VIDUP.
FORMATOS_VIDEO = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm',
                  '.m4v', '.mpg', '.mpeg', '.ts', '.3gp', '.ogv', '.rm',
                  '.rmvb', '.vob', '.divx', '.m2ts'}

# ============================================================================
# Rich (opcional)
# ============================================================================
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.rule import Rule
    from rich.markup import escape as rich_escape
    from rich import box as rich_box
    from rich.progress import (
        Progress, SpinnerColumn, BarColumn, TextColumn,
        TimeElapsedColumn, TimeRemainingColumn,
    )
    TIENE_RICH = True
except ImportError:
    Console = None      # type: ignore
    Panel = None        # type: ignore
    Table = None        # type: ignore
    Rule = None         # type: ignore
    rich_box = None     # type: ignore
    rich_escape = lambda texto: str(texto)
    TIENE_RICH = False

CONSOLA = Console(highlight=False, soft_wrap=True) if Console is not None else None

# ============================================================================
# Colores ANSI (fallback)
# ============================================================================
NC = '\033[0m'
NEGRITA = '\033[1m'
AZUL = '\033[94m'
AMARILLO = '\033[93m'
VERDE = '\033[92m'
ROJO = '\033[91m'
GRIS = '\033[90m'
CIAN = '\033[96m'
MAGENTA = '\033[95m'

# ============================================================================
# Constantes
# ============================================================================
_PALABRAS_ESCAPE = {'0', '00', 'inicio', 'salir', 'cancelar', 'menu', 'menú', 'volver'}
_MAX_INTENTOS = 5

# Edad máxima del .jsonl parcial para reanudar (24 horas)
_EDAD_MAXIMA_JSONL_SEG = 24 * 3600


class RegresarAlMenu(Exception):
    """Excepción para abortar la operación actual y volver al menú."""
    pass


# ============================================================================
# Impresión (rich o ANSI)
# ============================================================================
def _imprimir(mensaje: str = '', estilo: str = '') -> None:
    if CONSOLA is not None:
        CONSOLA.print(mensaje, style='#a5d6ff' if estilo == 'white' else (estilo or '#e4e4e4'))
    else:
        prefijo = ''
        sufijo = ''
        mapa = {
            'red': (ROJO, NC), 'yellow': (AMARILLO, NC), 'green': (VERDE, NC),
            'cyan': (CIAN, NC), 'magenta': (MAGENTA, NC), 'grey37': (GRIS, NC),
            'bold': (NEGRITA, NC),
        }
        if estilo in mapa:
            prefijo, sufijo = mapa[estilo]
        print(f'{prefijo}{mensaje}{sufijo}')


def _panel(cuerpo: str, titulo: str = '', estilo: str = 'bright_cyan') -> None:
    if CONSOLA is not None and Panel is not None:
        CONSOLA.print(Panel.fit(cuerpo, title=titulo or None, border_style=estilo))
    else:
        print(f'\n{"=" * 72}')
        if titulo:
            print(titulo)
        print(cuerpo)
        print(f'{"=" * 72}')


# ============================================================================
# Telegram
# ============================================================================
def _notificar_inicio(titulo: str, detalle: str = '') -> None:
    try:
        from .telegram import notificar_inicio as _ni
        _ni(titulo, detalle)
    except Exception:
        pass


def _notificar_fin(titulo: str, ok: bool, detalle: str = '') -> None:
    try:
        from .telegram import notificar_fin as _nf
        _nf(titulo, ok, detalle)
    except Exception:
        pass


# ============================================================================
# Utilidades
# ============================================================================
def _fmt_seg(segundos: float) -> str:
    segundos = max(0.0, segundos)
    horas = int(segundos // 3600)
    minutos = int((segundos % 3600) // 60)
    seg = int(segundos % 60)
    return f'{horas:02d}:{minutos:02d}:{seg:02d}'


def _fmt_bytes(cantidad: int) -> str:
    if cantidad <= 0:
        return '0 B'
    if cantidad < 1024:
        return f'{cantidad} B'
    if cantidad < 1024 ** 2:
        return f'{cantidad / 1024:.1f} KB'
    if cantidad < 1024 ** 3:
        return f'{cantidad / (1024 ** 2):.1f} MB'
    return f'{cantidad / (1024 ** 3):.2f} GB'


def _carpeta_reportes_sha256(raiz: Path) -> Path:
    return raiz / 'reportes' / 'sha256'


def _asegurar_subcarpetas(raiz: Path) -> Dict[str, Path]:
    base = _carpeta_reportes_sha256(raiz)
    sub = {}
    for nombre in ('json', 'csv', 'txt', 'jsonl'):
        p = base / nombre
        p.mkdir(parents=True, exist_ok=True)
        sub[nombre] = p
    return sub


def _pedir_texto(
    mensaje: str,
    defecto: str = '',
    obligatorio: bool = False,
    permitir_regreso: bool = True,
) -> str:
    intentos = 0
    while True:
        sufijo = f' [{defecto}]' if defecto else ''
        try:
            prompt = (f'[bold #87d7ff]{rich_escape(mensaje)}[/][#d7afff]{rich_escape(sufijo)}[/] [gold1]➜[/] '
                      if CONSOLA else f'{mensaje}{sufijo}: ')
            valor = (CONSOLA.input(prompt) if CONSOLA else input(prompt)).strip().strip('"').strip("'")
        except (KeyboardInterrupt, EOFError):
            raise RegresarAlMenu()

        if permitir_regreso and valor.lower() in _PALABRAS_ESCAPE:
            raise RegresarAlMenu()

        valor_final = valor or defecto
        if valor_final or not obligatorio:
            return valor_final

        intentos += 1
        if intentos >= _MAX_INTENTOS:
            _imprimir(f'⚠️ Demasiados intentos sin respuesta válida ({_MAX_INTENTOS}). Regresando...', 'yellow')
            raise RegresarAlMenu()
        _imprimir('Escribe un valor válido o "inicio" para regresar.', 'yellow')


def _pedir_numero(
    mensaje: str,
    defecto: float,
    minimo: float = 0,
    maximo: Optional[float] = None,
    entero: bool = False,
    permitir_regreso: bool = True,
) -> float:
    intentos = 0
    while True:
        try:
            raw = _pedir_texto(mensaje, str(defecto), permitir_regreso=permitir_regreso)
            valor = int(raw) if entero else float(raw.replace(',', '.'))
            if not math.isfinite(valor) or valor < minimo or (maximo is not None and valor > maximo):
                raise ValueError
            return valor
        except RegresarAlMenu:
            raise
        except ValueError:
            intentos += 1
            if intentos >= _MAX_INTENTOS:
                _imprimir(f'⚠️ Demasiados intentos fallidos ({_MAX_INTENTOS}). Regresando...', 'yellow')
                raise RegresarAlMenu()
            rango = f'mínimo {minimo}' + (f', máximo {maximo}' if maximo is not None else '')
            _imprimir(f'Escribe un número válido ({rango}).', 'yellow')


def _pedir_si_no(mensaje: str, defecto: bool = False, permitir_regreso: bool = True) -> bool:
    intentos = 0
    while True:
        try:
            valor = _pedir_texto(
                mensaje + ' (s/n)', 's' if defecto else 'n',
                permitir_regreso=permitir_regreso).lower()
            if valor in ('s', 'si', 'sí', 'y', 'yes'):
                return True
            if valor in ('n', 'no'):
                return False
            intentos += 1
            if intentos >= _MAX_INTENTOS:
                _imprimir(f'⚠️ Demasiados intentos fallidos ({_MAX_INTENTOS}). Regresando...', 'yellow')
                raise RegresarAlMenu()
            _imprimir('Responde s o n.', 'yellow')
        except RegresarAlMenu:
            raise


def _pedir_ruta(
    mensaje: str,
    defecto: str = '',
    existente: bool = False,
    directorio: bool = False,
    permitir_regreso: bool = True,
) -> str:
    intentos = 0
    while True:
        try:
            texto = _pedir_texto(mensaje, defecto, obligatorio=True, permitir_regreso=permitir_regreso)
            ruta = Path(texto).expanduser().resolve()
            if existente and not (ruta.is_dir() if directorio else ruta.is_file()):
                _imprimir(f'No se encontró {"el directorio" if directorio else "el archivo"}: {ruta}', 'yellow')
                intentos += 1
                if intentos >= _MAX_INTENTOS:
                    _imprimir(f'⚠️ Demasiados intentos ({_MAX_INTENTOS}). Regresando...', 'yellow')
                    raise RegresarAlMenu()
                continue
            return str(ruta)
        except RegresarAlMenu:
            raise


def _archivos_recientes(raiz: Path, patrones: List[str], subcarpetas: List[str]) -> List[Path]:
    base = raiz / 'reportes'
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


def _elegir_archivo_reciente(
    raiz: Path,
    descripcion: str,
    patrones: List[str],
    subcarpetas: List[str],
    defecto: str = '',
) -> str:
    recientes = _archivos_recientes(raiz, patrones, subcarpetas)
    if recientes:
        reciente = recientes[0]
        _imprimir(f'\n{descripcion}', 'cyan')
        _imprimir(f'  Más reciente: {reciente}', 'bold')
        if len(recientes) > 1:
            _imprimir(f'  ({len(recientes) - 1} archivo(s) más antiguo(s))', 'grey37')
        try:
            respuesta = _pedir_texto('¿Usar el más reciente? (s/n)', 's').lower()
        except RegresarAlMenu:
            raise
        if respuesta in ('s', 'si', 'sí', 'y', 'yes', ''):
            return str(reciente)
    return _pedir_ruta(descripcion or 'Ruta del archivo', defecto, existente=True)


def calcular_sha256(ruta: Path, chunk: int = 4 * 1024 * 1024) -> str:
    if chunk <= 0:
        raise ValueError('El tamaño del bloque debe ser positivo.')
    ruta = Path(ruta)
    antes = ruta.stat()
    sha = hashlib.sha256()
    with ruta.open('rb') as archivo:
        for bloque in iter(lambda: archivo.read(chunk), b''):
            sha.update(bloque)
    despues = ruta.stat()
    if (antes.st_dev, antes.st_ino, antes.st_size, antes.st_mtime_ns) != (
        despues.st_dev, despues.st_ino, despues.st_size, despues.st_mtime_ns
    ):
        raise OSError('El archivo cambió durante el cálculo de SHA256; vuelve a intentarlo.')
    return sha.hexdigest()


def listar_mp4(directorio: Path, recursivo: bool) -> List[Path]:
    """Enumera vídeos, respetando las exclusiones configuradas por dispositivo."""
    from core import inventario
    from core.database import info_dispositivo_actual
    raiz = Path(__file__).resolve().parents[1]
    excluidas = inventario.cargar_exclusiones(raiz, info_dispositivo_actual()['dispositivo_id'])
    if inventario.excluido(str(directorio), excluidas):
        _imprimir(f'Carpeta excluida: {directorio}', 'yellow')
        return []
    archivos = []
    if recursivo:
        def aviso(exc):
            _imprimir(f'No se pudo leer {exc}', 'yellow')
        for carpeta, subdirs, nombres in os.walk(directorio, followlinks=False, onerror=aviso):
            subdirs[:] = [sub for sub in subdirs
                           if not inventario.excluido(os.path.join(carpeta, sub), excluidas)]
            for nombre in nombres:
                if Path(nombre).suffix.lower() in FORMATOS_VIDEO:
                    archivo = Path(carpeta) / nombre
                    if not inventario.excluido(str(archivo), excluidas):
                        archivos.append(archivo)
    else:
        archivos = [p for p in directorio.iterdir()
                    if p.is_file() and p.suffix.lower() in FORMATOS_VIDEO
                    and not inventario.excluido(str(p), excluidas)]
    return sorted(archivos)


def listar_mp4_varias_carpetas(carpetas: Sequence[Path], recursivo: bool) -> List[Path]:
    """Lista MP4 de todas las raíces, sin repetir rutas solapadas.

    Respeta las exclusiones de VIDUP por dispositivo, mediante listar_mp4().
    Cada vídeo conserva su ruta absoluta completa y su dispositivo de origen.
    """
    encontrados: Dict[str, Path] = {}
    for carpeta in carpetas:
        for archivo in listar_mp4(Path(carpeta), recursivo):
            ruta_real = archivo.resolve()
            clave = os.path.normcase(str(ruta_real))
            encontrados.setdefault(clave, ruta_real)
    return sorted(encontrados.values(), key=lambda p: os.path.normcase(str(p)))


def _origen_del_archivo(archivo: Path, carpetas: Sequence[Path]) -> str:
    """Devuelve la raíz seleccionada más específica que contiene el archivo."""
    archivo = archivo.resolve()
    candidatas = []
    for carpeta in carpetas:
        try:
            archivo.relative_to(carpeta.resolve())
            candidatas.append(carpeta)
        except ValueError:
            continue
    return str(max(candidatas, key=lambda p: len(p.parts))) if candidatas else ''


class ProgresoJSONL:
    def __init__(
        self,
        carpeta_jsonl: Path,
        prefijo: str,
        reanudar: bool = True,
        edad_maxima_seg: Optional[int] = _EDAD_MAXIMA_JSONL_SEG,
    ):
        self._carpeta = carpeta_jsonl
        self._prefijo = prefijo
        self._reanudar = reanudar
        # None permite retomar operaciones SHA256 extensas incluso tras varios días.
        self._edad_maxima_seg = edad_maxima_seg
        self._ruta_jsonl: Path
        self._handle = None
        self._procesados: Dict[str, dict] = {}
        self._archivos_leidos: List[dict] = []
        self._es_nuevo = True

    def abrir(self) -> 'ProgresoJSONL':
        self._carpeta.mkdir(parents=True, exist_ok=True)

        if self._reanudar:
            reciente = self._buscar_jsonl_reciente()
            if reciente is not None:
                self._ruta_jsonl = reciente
                self._cargar_jsonl(reciente)
                self._es_nuevo = False
                self._handle = reciente.open('a', encoding='utf-8')
                return self

        identificador = _id_utc6()
        self._ruta_jsonl = self._carpeta / f'{self._prefijo}_{identificador}.jsonl'
        self._handle = self._ruta_jsonl.open('w', encoding='utf-8')
        self._es_nuevo = True
        return self

    def _buscar_jsonl_reciente(self) -> Optional[Path]:
        patron = f'{self._prefijo}_*.jsonl'
        candidatos: List[Tuple[int, Path]] = []
        for ruta in self._carpeta.glob(patron):
            try:
                mtime = ruta.stat().st_mtime
            except OSError:
                continue
            if self._edad_maxima_seg is not None and (time.time() - mtime) > self._edad_maxima_seg:
                continue
            candidatos.append((ruta.stat().st_mtime_ns, ruta))
        candidatos.sort(reverse=True)
        return candidatos[0][1] if candidatos else None

    def _cargar_jsonl(self, ruta: Path) -> None:
        try:
            with ruta.open('r', encoding='utf-8') as archivo:
                for linea in archivo:
                    linea = linea.strip()
                    if not linea:
                        continue
                    try:
                        registro = json.loads(linea)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(registro, dict):
                        continue
                    r = registro.get('ruta')
                    if r:
                        self._procesados[r] = registro
        except OSError:
            pass

    @property
    def es_nuevo(self) -> bool:
        return self._es_nuevo

    @property
    def ruta_jsonl(self) -> Path:
        return self._ruta_jsonl

    @property
    def cantidad_procesados(self) -> int:
        return len(self._procesados)

    def ya_procesado(self, ruta: str) -> bool:
        return ruta in self._procesados

    def registros_previos(self) -> List[dict]:
        return list(self._procesados.values())

    def escribir(self, registro: dict) -> None:
        if self._handle is None:
            return
        try:
            linea = json.dumps(registro, ensure_ascii=False)
            self._handle.write(linea + '\n')
            self._handle.flush()
            try:
                os.fsync(self._handle.fileno())
            except OSError:
                pass
        except OSError as exc:
            _imprimir(f'[AVISO] No se pudo escribir en {self._ruta_jsonl}: {exc}', 'yellow')

        r = registro.get('ruta')
        if r:
            self._procesados[r] = registro

    def cerrar(self) -> None:
        if self._handle is not None:
            try:
                self._handle.flush()
                try:
                    os.fsync(self._handle.fileno())
                except OSError:
                    pass
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    def limpiar(self) -> None:
        self.cerrar()
        try:
            if self._ruta_jsonl.is_file():
                self._ruta_jsonl.unlink()
        except OSError:
            pass


def modo_generar_offline(
    raiz: Path,
    carpeta: Path | Sequence[Path],
    recursivo: bool,
    archivos: List[Path],
) -> Dict:
    """Genera un único catálogo SHA256 para una o varias raíces del mismo equipo.

    Conserva la firma de la versión anterior: también acepta una única Path.
    El progreso se separa según el ID del equipo y la selección de raíces.
    """
    from core.database import info_dispositivo_actual
    info_disp = info_dispositivo_actual()

    if isinstance(carpeta, (str, os.PathLike)):
        carpetas = [Path(carpeta).expanduser().resolve()]
    else:
        carpetas = [Path(c).expanduser().resolve() for c in carpeta]
    # La identidad de las raíces es independiente del orden en que se añadan.
    carpetas = list(dict.fromkeys(carpetas))
    if not carpetas:
        raise ValueError('Selecciona al menos una carpeta para generar los SHA256.')

    raices_txt = [str(c) for c in carpetas]
    identidad = {
        'dispositivo_id': str(info_disp['dispositivo_id']),
        'carpetas': sorted(os.path.normcase(r) for r in raices_txt),
        'recursivo': bool(recursivo),
    }
    firma = hashlib.sha256(json.dumps(identidad, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:16]
    sub = _asegurar_subcarpetas(raiz)
    prog = ProgresoJSONL(
        sub['jsonl'], f'sha256_offline_{firma}', reanudar=True,
        edad_maxima_seg=None,
    ).abrir()

    if not prog.es_nuevo and prog.cantidad_procesados:
        _imprimir(f'\nSe encontró progreso de esta selección: {prog.ruta_jsonl.name}', 'cyan')
        _imprimir(f'Registros anteriores: {prog.cantidad_procesados}. Solo se omitirán los válidos y sin cambios.\n', 'bold')

    _notificar_inicio('SHA256 fuera de línea', f'{len(archivos)} vídeos en {len(carpetas)} ruta(s)')

    inicio = time.monotonic()
    total = len(archivos)
    saltados = 0
    interrumpido = False

    def manejar_ctrl_c(signum, frame):
        nonlocal interrumpido
        if not interrumpido:
            interrumpido = True
            raise KeyboardInterrupt

    manejador_anterior = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, manejar_ctrl_c)
    try:
        for indice, archivo in enumerate(archivos, 1):
            if interrumpido:
                break
            try:
                ruta_resuelta = str(archivo.resolve())
                datos_antes = archivo.stat()
                tamanio = datos_antes.st_size
                anterior = prog._procesados.get(ruta_resuelta, {})
                if (anterior.get('estado') == 'procesado'
                        and isinstance(anterior.get('sha256'), str)
                        and len(anterior['sha256']) == 64
                        and anterior.get('tamanio_bytes') == tamanio
                        and anterior.get('mtime_ns') == datos_antes.st_mtime_ns):
                    saltados += 1
                    continue

                _imprimir(f'[{indice}/{total}] {archivo.name} ({_fmt_bytes(tamanio)}) ...', 'white')
                sha = calcular_sha256(archivo)
                datos_despues = archivo.stat()
                if (datos_antes.st_dev, datos_antes.st_ino, datos_antes.st_size, datos_antes.st_mtime_ns) != (
                    datos_despues.st_dev, datos_despues.st_ino, datos_despues.st_size, datos_despues.st_mtime_ns
                ):
                    raise OSError('El archivo cambió durante el cálculo; intenta de nuevo.')
                meta_video = extraer_metadatos(archivo)

                registro = {
                    **meta_video,
                    'archivo': archivo.name,
                    'ruta': ruta_resuelta,
                    'carpeta_origen': _origen_del_archivo(archivo, carpetas),
                    'tamanio_bytes': tamanio,
                    'mtime_ns': datos_despues.st_mtime_ns,
                    'sha256': sha,
                    'estado': 'procesado',
                    'dispositivo': info_disp['dispositivo'],
                    'dispositivo_id': info_disp['dispositivo_id'],
                    'fecha_proceso': _ahora_utc6_str(),
                }
                prog.escribir(registro)

            except KeyboardInterrupt:
                interrumpido = True
                break
            except Exception as exc:
                _imprimir(f'  ERROR: {exc}', 'red')
                registro = {
                    'archivo': archivo.name,
                    'ruta': str(archivo.resolve()),
                    'carpeta_origen': _origen_del_archivo(archivo, carpetas),
                    'estado': 'error',
                    'causa': str(exc),
                    'dispositivo': info_disp['dispositivo'],
                    'dispositivo_id': info_disp['dispositivo_id'],
                    'fecha_proceso': _ahora_utc6_str(),
                }
                prog.escribir(registro)

    finally:
        signal.signal(signal.SIGINT, manejador_anterior)
        prog.cerrar()

    duracion = time.monotonic() - inicio
    registros_por_ruta = {it['ruta']: it for it in prog.registros_previos() if it.get('ruta')}
    # Se incluyen únicamente archivos presentes en la selección actual.
    todos = [registros_por_ruta[str(a.resolve())] for a in archivos
             if str(a.resolve()) in registros_por_ruta]
    completos = [r for r in todos if r.get('estado') == 'procesado']
    errores = [r for r in todos if r.get('estado') != 'procesado']

    resultado = {
        'modo': 'offline',
        'tipo_informe': 'sha256_offline',
        'carpeta': raices_txt[0] if len(raices_txt) == 1 else ' | '.join(raices_txt),
        'carpetas': raices_txt,
        'recursivo': recursivo,
        'ruta_db': 'N/A (fuera de línea)',
        'total_archivos': total,
        'videos': completos, 'detalles': todos, 'nuevos': completos,
        'total_procesados': len(completos), 'total_errores': len(errores),
        'duracion_seg': duracion, 'duracion': _fmt_seg(duracion),
        'dispositivo': info_disp, 'interrumpido': interrumpido,
        'saltados_previos': saltados,
        'ruta_jsonl_progreso': str(prog.ruta_jsonl),
    }

    if interrumpido:
        _imprimir(f'\nSHA256 interrumpido. Progreso conservado: {prog.ruta_jsonl}', 'yellow')
        _imprimir(f'  {len(completos)} archivos correctos, {len(errores)} errores.', 'yellow')
        resultado['ruta_jsonl_parcial'] = str(prog.ruta_jsonl)
        _notificar_fin('SHA256 fuera de línea', False, f'Interrumpido con {len(completos)} correctos')
    elif errores:
        _imprimir(f'\n{len(errores)} archivo(s) con error. Se generará informe, pero el JSONL se conserva para reintentar.', 'yellow')
        resultado['incompleto'] = True
        _notificar_fin('SHA256 fuera de línea', False, f'{len(completos)} correctos, {len(errores)} errores')
    else:
        _notificar_fin('SHA256 fuera de línea', True, f'{len(completos)} archivos en {_fmt_seg(duracion)}')
    # El JSONL se elimina SOLO después de escribir los tres informes con éxito.
    return resultado


def modo_importar(raiz: Path, carpeta: Path, recursivo: bool,
                  ruta_db: str, archivos: List[Path]) -> Dict:
    if not TIENE_VIDUP:
        raise RuntimeError('Este modo requiere core/ junto a vidup.py.')

    from core.database import info_dispositivo_actual
    info_disp = info_dispositivo_actual()

    _notificar_inicio('Importar carpeta como huellas', f'{len(archivos)} archivos en {carpeta}')

    conn = database.conectar(ruta_db)
    try:
        importados = 0
        omitidos = 0
        errores = 0
        detalles = []
        inicio = time.monotonic()

        for indice, archivo in enumerate(archivos, 1):
            try:
                ruta_resuelta = str(archivo.resolve())
                tamanio = archivo.stat().st_size
                _imprimir(f'[{indice}/{len(archivos)}] {archivo.name} ({_fmt_bytes(tamanio)}) ...', 'white')

                if database.existe_ruta(conn, ruta_resuelta):
                    _imprimir('  ya estaba en la base (por ruta).', 'grey37')
                    omitidos += 1
                    detalles.append({
                        'archivo': archivo.name, 'ruta': ruta_resuelta,
                        'estado': 'omitido', 'causa': 'La ruta ya existía en la base.',
                    })
                    continue

                sha = calcular_sha256(archivo)
                meta_video = extraer_metadatos(archivo)
                fp = VideoFingerprint(
                    ruta=ruta_resuelta, nombre=archivo.name, duracion=meta_video['duracion'],
                    resolucion=(meta_video['ancho'], meta_video['alto']), fps=meta_video['fps'],
                    codec_video=meta_video['codec_video'], codec_audio=meta_video['codec_audio'],
                    hash_archivo=sha, hash_rapido='sha256_solo',
                    frame_hashes=[], frame_intervalo=0.0, audio_huella=None,
                    tamanio_bytes=tamanio, metodo_acel='sha256',
                    error=None, bitrate_video_kbps=meta_video['bitrate_video_kbps'],
                )
                video_id = database.guardar_fingerprint(conn, fp)

                if video_id is None:
                    _imprimir('  ERROR al insertar.', 'red')
                    errores += 1
                    detalles.append({
                        'archivo': archivo.name, 'ruta': ruta_resuelta,
                        'estado': 'error', 'causa': 'guardar_fingerprint devolvió None.',
                    })
                else:
                    _imprimir(f'  importado (SHA256 {sha[:12]}...).', 'green')
                    importados += 1
                    detalles.append({
                        'archivo': archivo.name, 'ruta': ruta_resuelta,
                        'estado': 'importado', 'sha256': sha, 'tamanio_bytes': tamanio,
                        **meta_video,
                    })

            except Exception as exc:
                _imprimir(f'  ERROR: {exc}', 'red')
                errores += 1
                detalles.append({
                    'archivo': archivo.name, 'ruta': str(archivo.resolve()),
                    'estado': 'error', 'causa': str(exc),
                })

        duracion = time.monotonic() - inicio
        _notificar_fin('Importar carpeta como huellas', errores == 0,
                       f'{importados} importados, {errores} errores')

        return {
            'modo': 'importar', 'tipo_informe': 'sha256_importacion_directa',
            'carpeta': str(carpeta), 'recursivo': recursivo, 'ruta_db': ruta_db,
            'total_archivos': len(archivos),
            'importados': importados, 'omitidos': omitidos, 'errores': errores,
            'duracion_seg': duracion, 'duracion': _fmt_seg(duracion),
            'detalles': detalles, 'dispositivo': info_disp,
        }
    finally:
        database.cerrar(conn)


def modo_importar_offline_json(raiz: Path, ruta_json: str, ruta_db: str) -> Dict:
    """Importa solo SHA256 no registrados en el mismo dispositivo de origen.

    No reemplaza huellas visuales existentes, aunque el JSON use otra ruta.
    Distingue copias de dispositivos diferentes usando su ID persistente.
    """
    if not TIENE_VIDUP:
        raise RuntimeError('Este modo requiere core/ junto a vidup.py.')

    from core.database import info_dispositivo_actual, obtener_indices_existentes
    info_disp = info_dispositivo_actual()

    ruta_p = Path(ruta_json).expanduser().resolve()
    if not ruta_p.is_file():
        raise FileNotFoundError(f'No se encontró el archivo JSON: {ruta_json}')
    if Path(ruta_db).suffix.lower() not in ('.db', '.sqlite', '.sqlite3'):
        raise ValueError('La importación SHA256 requiere una base de datos SQLite (.db).')

    with ruta_p.open(encoding='utf-8-sig') as archivo:
        datos_json = json.load(archivo)
    if not isinstance(datos_json, dict):
        raise ValueError('El archivo JSON debe ser un objeto con una lista de vídeos.')

    items = datos_json.get('videos') or datos_json.get('nuevos') or datos_json.get('detalles') or []
    if not isinstance(items, list):
        raise ValueError('El archivo JSON no contiene una lista válida de vídeos o registros.')

    disp_json = datos_json.get('dispositivo') or {}
    if not isinstance(disp_json, dict):
        disp_json = {}
    origen_nombre = str(disp_json.get('dispositivo') or datos_json.get('dispositivo_origen') or '')
    origen_id = str(disp_json.get('dispositivo_id') or datos_json.get('dispositivo_id_origen') or '').strip()

    _notificar_inicio('Importar SHA256 desde JSON', f'{len(items)} registros')

    # Esta subopción es exclusiva de SQLite. Usar conectar() podría enviar los
    # datos a Neon si hay conexión incluso cuando el usuario eligió un .db.
    conn = database.conectar_sqlite(ruta_db)
    try:
        hashes_registrados, rutas_registradas = obtener_indices_existentes(conn)
        importados = 0
        omitidos = 0
        omitidos_por_sha = 0
        omitidos_por_ruta = 0
        errores = 0
        detalles = []
        inicio = time.monotonic()

        for indice, item in enumerate(items, 1):
            try:
                if not isinstance(item, dict):
                    errores += 1
                    detalles.append({'estado': 'error', 'causa': 'Registro JSON inválido.'})
                    continue
                ruta_v = str(item.get('ruta') or '').strip()
                nombre = str(item.get('archivo') or item.get('nombre') or Path(ruta_v).name)
                sha = str(item.get('sha256') or item.get('hash_archivo') or '').strip().lower()
                tamanio = int(item.get('tamanio_bytes') or 0)
                id_origen = str(item.get('dispositivo_id') or item.get('dispositivo_id_origen') or origen_id).strip()
                nombre_origen = str(item.get('dispositivo') or item.get('dispositivo_origen') or origen_nombre)

                if not id_origen:
                    raise ValueError('Sin ID de dispositivo en el JSON; no se importa para evitar atribuirlo a otro equipo.')
                if not ruta_v:
                    raise ValueError('Ruta de vídeo ausente.')
                if not re.fullmatch(r'[0-9a-f]{64}', sha):
                    raise ValueError('SHA256 ausente o inválido.')
                if tamanio < 0:
                    raise ValueError('Tamaño negativo.')

                # Comprueba el PAR (dispositivo, SHA256) antes que la ruta. Esto
                # cubre distintos métodos de escaneo y rutas distintas del mismo
                # archivo. También detecta duplicados dentro del propio JSON.
                if (id_origen, sha) in hashes_registrados:
                    omitidos += 1
                    omitidos_por_sha += 1
                    continue  # Sin ficha individual en TXT, CSV ni JSON.

                # Nunca sobrescribir una huella existente para la misma ruta:
                # guardar_fingerprint hace UPSERT y podría borrar pHash/audio.
                if (id_origen, ruta_v) in rutas_registradas:
                    omitidos += 1
                    omitidos_por_ruta += 1
                    continue

                _imprimir(f'[{indice}/{len(items)}] {nombre} ({_fmt_bytes(tamanio)}) ...', 'white')
                fp = VideoFingerprint(
                    ruta=ruta_v, nombre=nombre, duracion=float(item.get('duracion') or 0),
                    resolucion=(int(item.get('ancho') or 0), int(item.get('alto') or 0)),
                    fps=float(item.get('fps') or 0),
                    codec_video=item.get('codec_video') or '', codec_audio=item.get('codec_audio') or '',
                    hash_archivo=sha, hash_rapido='sha256_solo',
                    frame_hashes=[], frame_intervalo=0.0, audio_huella=None,
                    tamanio_bytes=tamanio, metodo_acel='sha256',
                    error=None, bitrate_video_kbps=float(item.get('bitrate_video_kbps') or 0),
                    dispositivo=nombre_origen, dispositivo_id=id_origen,
                )

                video_id = database.guardar_fingerprint(
                    conn, fp, dispositivo=nombre_origen, dispositivo_id=id_origen,
                    dispositivo_origen=item.get('dispositivo_origen') or origen_nombre,
                    dispositivo_origen_id=item.get('dispositivo_origen_id') or origen_id,
                )
                if video_id is None:
                    raise RuntimeError('La base de datos no pudo guardar el registro.')

                # Incluir los registros insertados en los índices para omitir
                # SHA256 repetidos dentro del mismo archivo de importación.
                hashes_registrados.add((id_origen, sha))
                rutas_registradas.add((id_origen, ruta_v))
                importados += 1
                detalles.append({
                    'archivo': nombre, 'ruta': ruta_v,
                    'estado': 'importado', 'sha256': sha, 'tamanio_bytes': tamanio,
                    'dispositivo': nombre_origen, 'dispositivo_id': id_origen,
                })
                _imprimir(f'  importado (SHA256 {sha[:12]}...).', 'green')
            except Exception as exc:
                _imprimir(f'  ERROR en registro {indice}: {exc}', 'red')
                errores += 1
                detalles.append({
                    'archivo': item.get('archivo', '?') if isinstance(item, dict) else '?',
                    'ruta': item.get('ruta', '') if isinstance(item, dict) else '',
                    'estado': 'error', 'causa': str(exc),
                })

        duracion = time.monotonic() - inicio
        _imprimir(f'Importación: {importados} nuevos; {omitidos_por_sha} ignorados por SHA256 '
                  f'del mismo dispositivo; {omitidos_por_ruta} ignorados por ruta; {errores} errores.', 'cyan')
        _notificar_fin('Importar SHA256 desde JSON', errores == 0,
                       f'{importados} importados, {omitidos} omitidos, {errores} errores')
        return {
            'modo': 'importar', 'tipo_informe': 'sha256_importacion_offline',
            'carpeta': datos_json.get('carpeta', 'Desde JSON offline'),
            'recursivo': datos_json.get('recursivo', False),
            'ruta_db': ruta_db, 'json_origen': str(ruta_p),
            'total_archivos': len(items),
            'importados': importados, 'omitidos': omitidos,
            'omitidos_por_sha_mismo_dispositivo': omitidos_por_sha,
            'omitidos_por_ruta': omitidos_por_ruta,
            'errores': errores,
            'duracion_seg': duracion, 'duracion': _fmt_seg(duracion),
            'detalles': detalles, 'dispositivo': info_disp,
            'dispositivo_origen': origen_nombre,
            'dispositivo_id_origen': origen_id,
        }
    finally:
        database.cerrar(conn)


def _cargar_hashes_de_base(ruta_db: str) -> Dict[str, List[Dict]]:
    ruta = Path(ruta_db).expanduser().resolve()
    if not ruta.is_file():
        raise FileNotFoundError(f'No se encontró la base de datos: {ruta}')
    if ruta.suffix.lower() == '.json':
        with ruta.open(encoding='utf-8-sig') as archivo:
            datos = json.load(archivo)
        if not isinstance(datos, dict) or not isinstance(datos.get('videos'), list):
            raise ValueError(f'{ruta}: se requiere un JSON de huellas, no un reporte de comparación.')
        filas = datos['videos']
        if any(not isinstance(f, dict) for f in filas):
            raise ValueError(f'{ruta}: el JSON de huellas contiene registros inválidos.')
    else:
        conn = sqlite3.connect(ruta.as_uri() + '?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            columnas = {f[1] for f in conn.execute('PRAGMA table_info(videos)').fetchall()}
            campos = ['ruta', 'nombre', 'hash_archivo', 'tamanio_bytes']
            for extra in ('dispositivo', 'dispositivo_id', 'clave_unica'):
                if extra in columnas:
                    campos.append(extra)
            sql = f'SELECT {", ".join(campos)} FROM videos WHERE error IS NULL AND hash_archivo <> ""'
            filas = [dict(f) for f in conn.execute(sql)]
        finally:
            conn.close()

    hashes: Dict[str, List[Dict]] = {}
    for fila in filas:
        if fila.get('error'):
            continue
        sha = str(fila.get('hash_archivo') or '').lower()
        if re.fullmatch(r'[0-9a-f]{64}', sha):
            hashes.setdefault(sha, []).append(dict(
                ruta=fila.get('ruta', ''),
                nombre=fila.get('nombre', ''),
                tamanio_bytes=fila.get('tamanio_bytes', 0),
                dispositivo=fila.get('dispositivo', '') or '',
                dispositivo_id=fila.get('dispositivo_id', '') or '',
                clave_unica=fila.get('clave_unica', '') or '',
                base=str(ruta),
            ))
    return hashes


def _clasificar_coincidencia(
    referencias: List[Dict],
    dispositivo_id_actual: str,
) -> Tuple[str, List[Dict], List[Dict]]:
    # Nunca interpretar un ID vacío como coincidencia de dispositivo.
    # Sin un ID verificable no se sabe si los registros son del mismo equipo.
    if not dispositivo_id_actual:
        return 'coincidencia', list(referencias), []
    otros = [r for r in referencias if r.get('dispositivo_id', '') != dispositivo_id_actual]
    aqui = [r for r in referencias if r.get('dispositivo_id', '') == dispositivo_id_actual]
    if otros:
        return 'coincidencia', otros, aqui
    return 'ya_registrado_aqui', otros, aqui


def modo_comparar(
    raiz: Path,
    carpeta: Path,
    recursivo: bool,
    ruta_db: str,
    archivos: List[Path],
    bases_extra: Optional[List[str]] = None,
) -> Dict:
    if not TIENE_VIDUP:
        raise RuntimeError('Este modo requiere core/ junto a vidup.py.')

    from core.database import info_dispositivo_actual, describir_conexion
    info_disp = info_dispositivo_actual()
    disp_id_actual = info_disp['dispositivo_id']

    sub = _asegurar_subcarpetas(raiz)
    prog = ProgresoJSONL(sub['jsonl'], 'sha256_comparar', reanudar=True).abrir()

    if not prog.es_nuevo and prog.cantidad_procesados:
        _imprimir(f'\nDetectado progreso anterior: {prog.ruta_jsonl.name}', 'cyan')
        _imprimir(f'Archivos ya procesados: {prog.cantidad_procesados}. Se saltarán automáticamente.\n', 'bold')

    _notificar_inicio('SHA256 comparar carpeta', f'{len(archivos)} archivos')

    _imprimir('\nCargando SHA256 existentes en la(s) base(s) de VIDUP...', 'cyan')
    hashes_base: Dict[str, List[Dict]] = {}
    bases = list(dict.fromkeys([ruta_db] + list(bases_extra or [])))
    for base in bases:
        for sha, referencias in _cargar_hashes_de_base(base).items():
            hashes_base.setdefault(sha, []).extend(referencias)
    _imprimir(f'  {len(hashes_base)} hashes únicos en la base.\n', 'white')

    coincidencias: List[dict] = []
    ya_registrados_aqui: List[dict] = []
    nuevos: List[dict] = []
    errores: List[dict] = []
    resumen_por_disp: Dict[str, Dict] = {}
    saltados = 0
    interrumpido = False

    def manejar_ctrl_c(signum, frame):
        nonlocal interrumpido
        if not interrumpido:
            interrumpido = True
            raise KeyboardInterrupt

    manejador_anterior = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, manejar_ctrl_c)

    inicio = time.monotonic()
    total = len(archivos)

    try:
        for indice, archivo in enumerate(archivos, 1):
            if interrumpido:
                break
            try:
                ruta_resuelta = str(archivo.resolve())
                tamanio = archivo.stat().st_size

                if prog.ya_procesado(ruta_resuelta):
                    saltados += 1
                    for prev in prog.registros_previos():
                        if prev.get('ruta') == ruta_resuelta:
                            estado_prev = prev.get('_clasificacion')
                            if estado_prev == 'coincidencia':
                                coincidencias.append(prev)
                            elif estado_prev == 'ya_registrado_aqui':
                                ya_registrados_aqui.append(prev)
                            elif estado_prev == 'nuevo':
                                nuevos.append(prev)
                            elif estado_prev == 'error':
                                errores.append(prev)
                            break
                    continue

                _imprimir(f'[{indice}/{total}] {archivo.name} ({_fmt_bytes(tamanio)}) ...', 'white')

                sha = calcular_sha256(archivo)

                if sha in hashes_base:
                    referencias = hashes_base[sha]
                    _, refs_otros, refs_aqui = _clasificar_coincidencia(referencias, disp_id_actual)
                    # Un SHA256 ya registrado en este dispositivo NO debe
                    # figurar como coincidencia pendiente, aunque también exista
                    # en otros dispositivos y aunque haya sido escaneado con pHash.
                    estado = 'ya_registrado_aqui' if refs_aqui else 'coincidencia'
                    ref_principal = (refs_aqui or refs_otros)[0]

                    entrada = {
                        'archivo': archivo.name, 'ruta': ruta_resuelta,
                        'tamanio_bytes': tamanio, 'sha256': sha,
                        'coincide_con': ref_principal['nombre'],
                        'ruta_en_base': ref_principal['ruta'],
                        'dispositivo_coincidente': ref_principal.get('dispositivo', ''),
                        'dispositivo_id_coincidente': ref_principal.get('dispositivo_id', ''),
                        'coincidencias_en_otros_dispositivos': refs_otros,
                        'coincidencias_en_este_dispositivo': refs_aqui,
                        '_clasificacion': estado,
                    }
                    if estado == 'coincidencia':
                        coincidencias.append(entrada)
                        for ref in refs_otros:
                            did = ref.get('dispositivo_id', '') or 'desconocido'
                            if did not in resumen_por_disp:
                                resumen_por_disp[did] = {
                                    'dispositivo': ref.get('dispositivo', '') or 'desconocido',
                                    'total': 0,
                                }
                            resumen_por_disp[did]['total'] += 1
                    else:
                        ya_registrados_aqui.append(entrada)
                    prog.escribir(entrada)

                else:
                    entrada = {
                        'archivo': archivo.name, 'ruta': ruta_resuelta,
                        'tamanio_bytes': tamanio, 'sha256': sha,
                        '_clasificacion': 'nuevo',
                    }
                    nuevos.append(entrada)
                    prog.escribir(entrada)

            except KeyboardInterrupt:
                interrumpido = True
                break
            except Exception as exc:
                _imprimir(f'  ERROR: {exc}', 'red')
                entrada = {
                    'archivo': archivo.name, 'ruta': str(archivo.resolve()),
                    'causa': str(exc), '_clasificacion': 'error',
                }
                errores.append(entrada)
                prog.escribir(entrada)

    finally:
        signal.signal(signal.SIGINT, manejador_anterior)
        prog.cerrar()

    duracion = time.monotonic() - inicio

    resultado = {
        'modo': 'comparar', 'tipo_informe': 'sha256_carpeta',
        'carpeta': str(carpeta), 'recursivo': recursivo, 'ruta_db': ruta_db,
        'dispositivo_actual': info_disp,
        'total_archivos': total,
        'coincidencias': coincidencias,
        'ya_registrados_aqui': ya_registrados_aqui,
        'nuevos': nuevos, 'errores': errores,
        'bases_de_datos': bases,
        'resumen_coincidencias_por_dispositivo': resumen_por_disp,
        'total_coincidencias': len(coincidencias),
        'total_ya_registrados_aqui': len(ya_registrados_aqui),
        'total_nuevos': len(nuevos), 'total_errores': len(errores),
        'duracion_seg': duracion, 'duracion': _fmt_seg(duracion),
        'dispositivo': info_disp, 'conexion': describir_conexion(),
    }

    if interrumpido:
        resultado['interrumpido'] = True
        resultado['ruta_jsonl_parcial'] = str(prog.ruta_jsonl)
        resultado['saltados_previos'] = saltados
        _imprimir(f'\nComparación interrumpida. Progreso guardado en:', 'yellow')
        _imprimir(f'  {prog.ruta_jsonl}', 'yellow')
        _notificar_fin('SHA256 comparar carpeta', False, f'Interrumpido')
        return resultado

    prog.limpiar()
    _notificar_fin('SHA256 comparar carpeta', True, f'{len(coincidencias)} coincidencias, {len(nuevos)} nuevos')
    return resultado


def modo_comparar_offline_json(
    raiz: Path,
    ruta_json: str,
    ruta_db: str,
    bases_extra: Optional[List[str]] = None,
) -> Dict:
    if not TIENE_VIDUP:
        raise RuntimeError('Este modo requiere core/ junto a vidup.py.')

    from core.database import info_dispositivo_actual, describir_conexion
    info_disp = info_dispositivo_actual()

    ruta_p = Path(ruta_json).expanduser().resolve()
    if not ruta_p.is_file():
        raise FileNotFoundError(f'No se encontró el archivo JSON: {ruta_json}')

    with ruta_p.open(encoding='utf-8-sig') as f:
        datos_json = json.load(f)

    items = datos_json.get('videos') or datos_json.get('nuevos') or datos_json.get('detalles') or []
    if not isinstance(items, list):
        raise ValueError('El archivo JSON no contiene una lista válida de vídeos o registros.')

    # El JSON puede haberse generado en Android y estar comparándose desde Windows.
    # El ID relevante es el del archivo de origen, NUNCA el del equipo que ejecuta
    # ahora VIDUP. Los datos de cada vídeo tienen precedencia en JSON mixtos.
    origen_json = datos_json.get('dispositivo') or datos_json.get('dispositivo_origen') or {}
    id_origen_general = (origen_json.get('dispositivo_id') or '').strip() \
        if isinstance(origen_json, dict) else ''
    if not id_origen_general and not any(
        isinstance(it, dict) and it.get('dispositivo_id') for it in items
    ):
        _imprimir('AVISO: el JSON no identifica su dispositivo de origen. '
                  'No se pueden omitir registros con seguridad basándose en el dispositivo.', 'yellow')

    sub = _asegurar_subcarpetas(raiz)
    prog = ProgresoJSONL(sub['jsonl'], 'sha256_comparar_offline', reanudar=True).abrir()

    if not prog.es_nuevo and prog.cantidad_procesados:
        _imprimir(f'\nDetectado progreso anterior: {prog.ruta_jsonl.name}', 'cyan')
        _imprimir(f'Registros ya procesados: {prog.cantidad_procesados}. Se saltarán automáticamente.\n', 'bold')

    _notificar_inicio('SHA256 comparar JSON offline', f'{len(items)} registros')

    _imprimir('\nCargando SHA256 existentes en la(s) base(s) de datos...', 'cyan')
    hashes_base: Dict[str, List[Dict]] = {}
    bases = list(dict.fromkeys([ruta_db] + list(bases_extra or [])))
    for base in bases:
        for sha, referencias in _cargar_hashes_de_base(base).items():
            hashes_base.setdefault(sha, []).extend(referencias)
    _imprimir(f'  {len(hashes_base)} hashes únicos en la base.\n', 'white')

    coincidencias: List[dict] = []
    ya_registrados_aqui: List[dict] = []
    nuevos: List[dict] = []
    errores: List[dict] = []
    resumen_por_disp: Dict[str, Dict] = {}
    saltados = 0
    interrumpido = False

    def manejar_ctrl_c(signum, frame):
        nonlocal interrumpido
        if not interrumpido:
            interrumpido = True
            raise KeyboardInterrupt

    manejador_anterior = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, manejar_ctrl_c)

    inicio = time.monotonic()
    total = len(items)

    try:
        for indice, item in enumerate(items, 1):
            if interrumpido:
                break
            try:
                if not isinstance(item, dict):
                    continue
                nombre = item.get('archivo') or item.get('nombre') or Path(item.get('ruta', '')).name
                ruta_v = item.get('ruta', '')
                sha = str(item.get('sha256') or '').lower()
                tamanio = item.get('tamanio_bytes', 0)
                disp_id_origen = str(item.get('dispositivo_id') or id_origen_general).strip()
                disp_nombre_origen = str(item.get('dispositivo') or (
                    origen_json.get('dispositivo', '') if isinstance(origen_json, dict) else ''
                ))

                # Incluye el dispositivo: dos JSON distintos pueden tener rutas y
                # hashes iguales, pero las clasificaciones pueden ser diferentes.
                clave_reanud = f'{disp_id_origen}|{ruta_v}|{sha}'
                if prog.ya_procesado(clave_reanud):
                    saltados += 1
                    for prev in prog.registros_previos():
                        if prev.get('ruta') == clave_reanud:
                            # En el JSONL la clave de reanudación ocupa 'ruta';
                            # recuperar la ruta real del archivo para el informe.
                            prev = dict(prev)
                            prev['ruta'] = ruta_v
                            estado_prev = prev.get('_clasificacion')
                            if estado_prev == 'coincidencia':
                                coincidencias.append(prev)
                            elif estado_prev == 'ya_registrado_aqui':
                                ya_registrados_aqui.append(prev)
                            elif estado_prev == 'nuevo':
                                nuevos.append(prev)
                            elif estado_prev == 'error':
                                errores.append(prev)
                            break
                    continue

                if not sha or len(sha) != 64:
                    entrada = {
                        'archivo': nombre, 'ruta': ruta_v,
                        'causa': 'Hash SHA256 ausente o inválido en el JSON.',
                        '_clasificacion': 'error', 'clave_reanud': clave_reanud,
                    }
                    errores.append(entrada)
                    entrada_prog = dict(entrada)
                    entrada_prog['ruta'] = clave_reanud
                    prog.escribir(entrada_prog)
                    continue

                _imprimir(f'[{indice}/{total}] {nombre} ({_fmt_bytes(tamanio)}) ...', 'white')

                if sha in hashes_base:
                    referencias = hashes_base[sha]
                    _, refs_otros, refs_aqui = _clasificar_coincidencia(
                        referencias, disp_id_origen)
                    # Regla de omisión: el hash YA figura en ese dispositivo, con
                    # independencia de si fue incorporado mediante escaneo visual,
                    # mínimo SHA256 o importación, y aunque exista en otros equipos.
                    estado = 'ya_registrado_aqui' if refs_aqui else 'coincidencia'
                    ref_principal = (refs_aqui or refs_otros)[0]

                    entrada = {
                        'archivo': nombre, 'ruta': ruta_v,
                        'dispositivo_origen': disp_nombre_origen,
                        'dispositivo_id_origen': disp_id_origen,
                        'tamanio_bytes': tamanio, 'sha256': sha,
                        'coincide_con': ref_principal['nombre'],
                        'ruta_en_base': ref_principal['ruta'],
                        'dispositivo_coincidente': ref_principal.get('dispositivo', ''),
                        'dispositivo_id_coincidente': ref_principal.get('dispositivo_id', ''),
                        'coincidencias_en_otros_dispositivos': refs_otros,
                        'coincidencias_en_este_dispositivo': refs_aqui,
                        '_clasificacion': estado, 'clave_reanud': clave_reanud,
                    }
                    if estado == 'coincidencia':
                        coincidencias.append(entrada)
                        for ref in refs_otros:
                            did = ref.get('dispositivo_id', '') or 'desconocido'
                            if did not in resumen_por_disp:
                                resumen_por_disp[did] = {
                                    'dispositivo': ref.get('dispositivo', '') or 'desconocido',
                                    'total': 0,
                                }
                            resumen_por_disp[did]['total'] += 1
                    else:
                        entrada['causa'] = ('Ignorado: el SHA256 ya fue escaneado y '
                                            'registrado para el dispositivo de origen.')
                        ya_registrados_aqui.append(entrada)
                        _imprimir(f'  ignorado: registrado previamente en {disp_nombre_origen or disp_id_origen}', 'grey37')
                    entrada_prog = dict(entrada)
                    entrada_prog['ruta'] = clave_reanud
                    prog.escribir(entrada_prog)

                else:
                    entrada = {
                        'archivo': nombre, 'ruta': ruta_v,
                        'tamanio_bytes': tamanio, 'sha256': sha,
                        '_clasificacion': 'nuevo', 'clave_reanud': clave_reanud,
                    }
                    nuevos.append(entrada)
                    entrada_prog = dict(entrada)
                    entrada_prog['ruta'] = clave_reanud
                    prog.escribir(entrada_prog)

            except KeyboardInterrupt:
                interrumpido = True
                break
            except Exception as exc:
                _imprimir(f'  ERROR: {exc}', 'red')
                errores.append({
                    'archivo': item.get('archivo', '?') if isinstance(item, dict) else '?',
                    'ruta': item.get('ruta', '') if isinstance(item, dict) else '',
                    'causa': str(exc), '_clasificacion': 'error',
                })

    finally:
        signal.signal(signal.SIGINT, manejador_anterior)
        prog.cerrar()

    duracion = time.monotonic() - inicio

    resultado = {
        'modo': 'comparar', 'tipo_informe': 'sha256_comparacion_offline',
        'carpeta': datos_json.get('carpeta', 'Desde JSON offline'),
        'recursivo': datos_json.get('recursivo', False),
        'ruta_db': ruta_db, 'json_origen': str(ruta_p),
        'dispositivo_actual': info_disp,
        'dispositivo_id_origen_general': id_origen_general,
        'total_archivos': total,
        'coincidencias': coincidencias, 'ya_registrados_aqui': ya_registrados_aqui,
        'omitidos_mismo_dispositivo': ya_registrados_aqui,
        'nuevos': nuevos, 'errores': errores,
        'bases_de_datos': bases,
        'resumen_coincidencias_por_dispositivo': resumen_por_disp,
        'total_coincidencias': len(coincidencias),
        'total_ya_registrados_aqui': len(ya_registrados_aqui),
        'total_omitidos_mismo_dispositivo': len(ya_registrados_aqui),
        'total_nuevos': len(nuevos), 'total_errores': len(errores),
        'duracion_seg': duracion, 'duracion': _fmt_seg(duracion),
        'dispositivo': info_disp, 'conexion': describir_conexion(),
        'dispositivo_origen_json': datos_json.get('dispositivo'),
    }

    if interrumpido:
        resultado['interrumpido'] = True
        resultado['ruta_jsonl_parcial'] = str(prog.ruta_jsonl)
        resultado['saltados_previos'] = saltados
        _notificar_fin('SHA256 comparar JSON offline', False, f'Interrumpido')
        return resultado

    prog.limpiar()
    _imprimir(f'Comparación finalizada: {len(ya_registrados_aqui)} ignorados por registro previo '
              f'en el dispositivo de origen; {len(coincidencias)} coincidencias en otros equipos; '
              f'{len(nuevos)} nuevos; {len(errores)} errores.', 'cyan')
    _notificar_fin('SHA256 comparar JSON offline', True,
                   f'{len(coincidencias)} coincidencias, {len(ya_registrados_aqui)} omitidos por registro previo, {len(nuevos)} nuevos')
    return resultado


# ============================================================================
# Generación de reportes (JSON, TXT, CSV) - Lógica COMPLETA restaurada
# ============================================================================
def escribir_reporte_json(resultado: Dict, ruta_salida: str) -> None:
    destino = Path(ruta_salida)
    destino.parent.mkdir(parents=True, exist_ok=True)
    datos = {
        'version': 3,
        'tipo_informe': resultado.get('tipo_informe', 'sha256_carpeta'),
        'fecha': _ahora_utc6_str(),
        **resultado,
    }
    # Los omitidos NO se enumeran en ninguno de los reportes finales.
    # Mantener solo los totales para auditoría y para el resumen.
    if datos.get('modo') == 'comparar':
        datos['total_ya_registrados_aqui'] = resultado.get(
            'total_ya_registrados_aqui', len(resultado.get('ya_registrados_aqui', [])))
        datos['total_omitidos_mismo_dispositivo'] = datos['total_ya_registrados_aqui']
        datos.pop('ya_registrados_aqui', None)
        datos.pop('omitidos_mismo_dispositivo', None)
    if datos.get('modo') == 'importar':
        datos['detalles'] = [d for d in resultado.get('detalles', [])
                             if d.get('estado') != 'omitido']
    if 'dispositivo' not in datos:
        from core.database import info_dispositivo_actual
        datos['dispositivo'] = info_dispositivo_actual()
    if 'conexion' not in datos:
        from core.database import describir_conexion
        datos['conexion'] = describir_conexion()
    with destino.open('w', encoding='utf-8') as archivo:
        json.dump(datos, archivo, ensure_ascii=False, indent=4)
        archivo.write('\n')


def escribir_reporte_txt(resultado: Dict, ruta_salida: str) -> None:
    destino = Path(ruta_salida)
    destino.parent.mkdir(parents=True, exist_ok=True)

    ancho = 78
    sep = '=' * ancho
    sub_sep = '-' * ancho

    info_disp = resultado.get('dispositivo') or {}
    lineas = [
        sep, 'SHA256 DE CARPETAS — REPORTE', sep,
        f'Fecha           : {_ahora_utc6_str()}',
        f'Dispositivo     : {info_disp.get("dispositivo", "N/D")}',
        f'ID dispositivo  : {info_disp.get("dispositivo_id", "N/D")}',
        f'Plataforma      : {info_disp.get("plataforma", "N/D")} ({info_disp.get("maquina", "N/D")})',
        f'¿Android?       : {"sí" if info_disp.get("es_android") else "no"}',
        f'Modo de conexión: {resultado.get("conexion", "N/D")}',
        f'Modo            : {resultado.get("modo", "")}',
        f'Carpeta         : {len(resultado["carpetas"])} raíz(es)' if resultado.get('carpetas') else f'Carpeta         : {resultado.get("carpeta", "")}',
        f'Recursivo       : {"sí" if resultado.get("recursivo") else "no"}',
        f'Base de datos   : {resultado.get("ruta_db", "")}',
        f'Total archivos  : {resultado.get("total_archivos", 0)}',
        f'Tiempo total    : {resultado.get("duracion", _fmt_seg(resultado.get("duracion_seg", 0.0)))}',
        '',
    ]

    if resultado.get('carpetas'):
        lineas.append('RAÍCES INCLUIDAS EN ESTE REPORTE:')
        lineas.extend(f'  {i}. {ruta}' for i, ruta in enumerate(resultado['carpetas'], 1))
        lineas.append('')

    if resultado.get('dispositivo_origen_json'):
        o = resultado['dispositivo_origen_json']
        if isinstance(o, dict):
            lineas.extend([
                'DISPOSITIVO DE ORIGEN (JSON):',
                f'  Dispositivo   : {o.get("dispositivo", "N/D")}',
                f'  ID            : {o.get("dispositivo_id", "N/D")}',
                '',
            ])

    modo = resultado.get('modo')

    if modo == 'offline':
        detalles = resultado.get('detalles', [])
        lineas.extend([sep, 'RESUMEN (GENERACIÓN FUERA DE LÍNEA)', sep,
                       f'  Procesados : {resultado.get("total_procesados", len(detalles))}',
                       f'  Errores    : {resultado.get("total_errores", 0)}', '',
                       sep, 'DETALLE DE ARCHIVOS PROCESADOS', sep, ''])
        for i, d in enumerate(detalles, 1):
            lineas.extend([sub_sep, f'{i}. {d.get("archivo", "")}', sub_sep,
                           f'  Ruta   : {d.get("ruta", "")}',
                           f'  Raíz   : {d.get("carpeta_origen", "")}',
                           f'  Estado : {d.get("estado", "")}',
                           f'  Error  : {d.get("causa", "")}' if d.get('causa') else '  Error  : ninguno',
                           f'  Tamaño : {_fmt_bytes(d.get("tamanio_bytes", 0))}',
                           f'  SHA256 : {d.get("sha256", "")}',
                           f'  Resolución: {d.get("ancho") or 0}x{d.get("alto") or 0}' if d.get('ancho') else '  Resolución: N/D',
                           f'  Duración: {_fmt_seg(d.get("duracion", 0)) if d.get("duracion") else "N/D"}',
                           f'  Códec vídeo: {d.get("codec_video") or "N/D"}',
                           f'  Códec audio: {d.get("codec_audio") or "N/D"}',
                           f'  Bitrate vídeo: {d.get("bitrate_video_kbps", 0):.0f} kbps' if d.get('bitrate_video_kbps') else '  Bitrate vídeo: N/D', ''])
    
    
    elif modo == 'importar':
        lineas.extend([sep, 'RESUMEN', sep,
                   f'  Importados : {resultado.get("importados", 0)}',
                   f'  Omitidos   : {resultado.get("omitidos", 0)}',
                   f'    - SHA256 ya registrado en mismo dispositivo : {resultado.get("omitidos_por_sha_mismo_dispositivo", 0)}',
                   f'    - Ruta ya registrada                        : {resultado.get("omitidos_por_ruta", 0)}',
                   f'  Errores    : {resultado.get("errores", 0)}', ''])
    detalles = [d for d in resultado.get('detalles', [])
                if d.get('estado') != 'omitido']
    if detalles:
        lineas.extend([sep, 'DETALLE POR ARCHIVO', sep, ''])
        for i, detalle in enumerate(detalles, 1):
            lineas.extend([sub_sep, f'{i}. {detalle.get("archivo", "")}', sub_sep,
                           f'  Ruta   : {detalle.get("ruta", "")}',
                           f'  Estado : {detalle.get("estado", "")}'])
            if detalle.get('sha256'):
                lineas.append(f'  SHA256 : {detalle["sha256"]}')
            if detalle.get('tamanio_bytes'):
                lineas.append(f'  Tamaño : {_fmt_bytes(detalle["tamanio_bytes"])}')
            
            # --- NUEVOS CAMPOS (igual que en offline) ---
            lineas.append(f'  Resolución: {detalle.get("ancho") or 0}x{detalle.get("alto") or 0}' if detalle.get('ancho') else '  Resolución: N/D')
            lineas.append(f'  Duración: {_fmt_seg(detalle.get("duracion", 0)) if detalle.get("duracion") else "N/D"}')
            lineas.append(f'  Códec vídeo: {detalle.get("codec_video") or "N/D"}')
            lineas.append(f'  Códec audio: {detalle.get("codec_audio") or "N/D"}')
            lineas.append(f'  Bitrate vídeo: {detalle.get("bitrate_video_kbps", 0):.0f} kbps' if detalle.get('bitrate_video_kbps') else '  Bitrate vídeo: N/D')
            # --------------------------------------------

            if detalle.get('causa'):
                lineas.append(f'  Causa  : {detalle["causa"]}')
            lineas.append('')
    

    elif modo == 'comparar':
        coincidencias = resultado.get('coincidencias', [])
        numero_ignorados = resultado.get('total_ya_registrados_aqui',
                                           len(resultado.get('ya_registrados_aqui', [])))
        nuevos = resultado.get('nuevos', [])
        errores = resultado.get('errores', [])
        resumen_disp = resultado.get('resumen_coincidencias_por_dispositivo', {})

        es_json_sha = resultado.get('tipo_informe') == 'sha256_comparacion_offline'
        titulo_coincidencias = ('Coincidencias (sin registro previo en origen)' if es_json_sha
                               else 'Coincidencias (copiados en otros dispositivos)')
        lineas.extend([sep, 'RESUMEN', sep,
                       f'  {titulo_coincidencias} : {len(coincidencias)}'])
        for did, info in resumen_disp.items():
            lineas.append(f'    • {info.get("dispositivo", did):<45} : {info.get("total", 0)}')
        lineas.extend([
            (f'  Ignorados (ya registrados en dispositivo origen): {numero_ignorados}'
             if es_json_sha else
             f'  Ya registrados aquí (mismo dispositivo)         : {numero_ignorados}'),
            f'  Nuevos (nunca vistos)                           : {len(nuevos)}',
            f'  Errores                                         : {len(errores)}',
            '',
        ])

        if coincidencias:
            descripcion = ('Coincidencias SHA256: no se encontraron previamente registradas '
                           'en el dispositivo de origen identificado en el JSON.' if es_json_sha else
                           'Estos vídeos ya existen en la base, en otro dispositivo distinto al actual.')
            lineas.extend([sep, f'COINCIDENCIAS — OTROS REGISTROS ({len(coincidencias)})',
                           descripcion, sep, ''])
            for i, c in enumerate(coincidencias, 1):
                lineas.extend([sub_sep, f'{i}. {c.get("archivo", "")}', sub_sep,
                               f'  Ruta en carpeta      : {c.get("ruta", "")}',
                               f'  Ya existe como       : {c.get("coincide_con", "")}',
                               f'  Ruta en base         : {c.get("ruta_en_base", "")}',
                               f'  Dispositivo          : {c.get("dispositivo_coincidente", "")}',
                               f'  ID dispositivo       : {c.get("dispositivo_id_coincidente", "")}',
                               f'  Tamaño               : {_fmt_bytes(c.get("tamanio_bytes", 0))}',
                               f'  SHA256               : {c.get("sha256", "")}', ''])
                otros = c.get('coincidencias_en_otros_dispositivos', [])
                if len(otros) > 1:
                    lineas.append('  También en:')
                    for ref in otros[1:]:
                        lineas.append(f'    - {ref.get("dispositivo", "?")} | {ref.get("ruta", "")}')

        if numero_ignorados:
            lineas.extend([
                '  Nota: los vídeos ya registrados en el mismo dispositivo',
                '  se contabilizan, pero NO se enumeran ni se importan de nuevo.',
                '',
            ])

        if nuevos:
            lineas.extend([sep, f'NUEVOS ({len(nuevos)})',
                           'El SHA256 de estos archivos no aparece en las bases seleccionadas.',
                           'Usa este JSON en la opción de escanear nuevos desde SHA256.',
                           sep, ''])
            for i, n in enumerate(nuevos, 1):
                lineas.extend([sub_sep, f'{i}. {n.get("archivo", "")}', sub_sep,
                               f'  Ruta   : {n.get("ruta", "")}',
                               f'  Tamaño : {_fmt_bytes(n.get("tamanio_bytes", 0))}',
                               f'  SHA256 : {n.get("sha256", "")}', ''])

        if errores:
            lineas.extend([sep, f'ERRORES ({len(errores)})', sep, ''])
            for i, e in enumerate(errores, 1):
                lineas.extend([sub_sep, f'{i}. {e.get("archivo", "")}', sub_sep,
                               f'  Ruta  : {e.get("ruta", "")}',
                               f'  Causa : {e.get("causa", "")}', ''])

    lineas.extend([sep, 'FIN DEL REPORTE', sep, ''])

    with destino.open('w', encoding='utf-8') as archivo:
        archivo.write('\n'.join(lineas) + '\n')


def escribir_reporte_csv(resultado: Dict, ruta_salida: str) -> None:
    destino = Path(ruta_salida)
    destino.parent.mkdir(parents=True, exist_ok=True)
    columnas = [
        'Estado', 'Archivo', 'Ruta', 'SHA256', 'Tamaño (bytes)',
        'Coincide con', 'Ruta en base', 'Dispositivo coincidente', 'ID dispositivo coincidente',
        'Coincidencias en otros dispositivos', 'Coincidencias en este dispositivo',
        'Causa', 'Carpeta de origen', 'Carpeta analizada', 'Incluye subcarpetas', 'Bases de datos',
        'Tiempo (segundos)', 'Duración (HH:MM:SS)',
        'Dispositivo actual', 'ID dispositivo actual', 'Modo de conexión',
    ]
    filas = []
    info_disp = resultado.get('dispositivo') or {}
    conexion_txt = resultado.get('conexion', '')
    modo = resultado.get('modo')

    if modo == 'offline':
        grupos = [('Procesado', resultado.get('detalles', []))]
    elif modo == 'comparar':
        grupos = [
            ('Coincidencia (otro dispositivo)', resultado.get('coincidencias', [])),
            ('Nuevo', resultado.get('nuevos', [])),
            ('Error', resultado.get('errores', [])),
        ]
    else:
        grupos = [('Importación', [d for d in resultado.get('detalles', [])
                                  if d.get('estado') != 'omitido'])]

    for estado, detalles in grupos:
        for item in detalles:
            filas.append({
                'Estado': 'Error' if item.get('estado') == 'error' else estado,
                'Archivo': item.get('archivo', ''),
                'Ruta': item.get('ruta', ''),
                'SHA256': item.get('sha256', ''),
                'Tamaño (bytes)': item.get('tamanio_bytes', ''),
                'Coincide con': item.get('coincide_con', ''),
                'Ruta en base': item.get('ruta_en_base', ''),
                'Dispositivo coincidente': item.get('dispositivo_coincidente', ''),
                'ID dispositivo coincidente': item.get('dispositivo_id_coincidente', ''),
                'Coincidencias en otros dispositivos': json.dumps(
                    item.get('coincidencias_en_otros_dispositivos', []), ensure_ascii=False),
                'Coincidencias en este dispositivo': json.dumps(
                    item.get('coincidencias_en_este_dispositivo', []), ensure_ascii=False),
                'Causa': item.get('causa', ''),
                'Carpeta de origen': item.get('carpeta_origen', ''),
                'Dispositivo actual': info_disp.get('dispositivo', ''),
                'ID dispositivo actual': info_disp.get('dispositivo_id', ''),
                'Modo de conexión': conexion_txt,
            })

    filas.insert(0, {
        'Estado': 'Resumen',
        'Archivo': f'{resultado.get("total_archivos", 0)} archivos',
        'Carpeta analizada': resultado.get('carpeta', ''),
        'Incluye subcarpetas': 'sí' if resultado.get('recursivo') else 'no',
        'Bases de datos': ' | '.join(resultado.get('bases_de_datos', [resultado.get('ruta_db', '')])),
        'Tiempo (segundos)': resultado.get('duracion_seg', 0.0),
        'Duración (HH:MM:SS)': resultado.get('duracion', _fmt_seg(resultado.get('duracion_seg', 0.0))),
        'Dispositivo actual': info_disp.get('dispositivo', ''),
        'ID dispositivo actual': info_disp.get('dispositivo_id', ''),
        'Modo de conexión': conexion_txt,
        'Causa': (
            f'Ignorados por registro previo: {resultado.get("total_ya_registrados_aqui", len(resultado.get("ya_registrados_aqui", [])))}'
            if modo == 'comparar' else
            f'Importados: {resultado.get("importados", 0)}; omitidos: {resultado.get("omitidos", 0)}; errores: {resultado.get("errores", 0)}'
            if modo == 'importar' else ''
        ),
    })

    with destino.open('w', encoding='utf-8-sig', newline='') as archivo:
        writer = csv.DictWriter(archivo, fieldnames=columnas)
        writer.writeheader()
        writer.writerows(filas)


def cargar_pendientes(ruta_json: str, incluir_ya_registrados: bool = False):
    with Path(ruta_json).expanduser().open(encoding='utf-8-sig') as archivo:
        datos = json.load(archivo)

    if not isinstance(datos, dict):
        raise ValueError('El JSON no es válido.')

    if datos.get('modo') not in ('comparar', 'offline'):
        raise ValueError('Selecciona un JSON de comparación o fuera de línea SHA256 válido.')

    items = datos.get('nuevos')
    if items is None:
        items = datos.get('videos') or datos.get('detalles') or []
    if not isinstance(items, list):
        raise ValueError('El JSON no contiene una lista válida de vídeos o registros.')

    # Nunca reintroducir omitidos: versiones anteriores incluían esta lista
    # detallada, pero no representa vídeos pendientes de escaneo.

    candidatos, esperados, avisos = [], {}, []

    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('ruta'), str) or not item['ruta']:
            raise ValueError('El JSON contiene una ruta de vídeo nueva inválida.')
        sha = str(item.get('sha256') or '').lower()
        if not re.fullmatch(r'[0-9a-f]{64}', sha):
            raise ValueError(f'SHA256 inválido para {item["ruta"]}.')
        tamanio = item.get('tamanio_bytes')
        if not isinstance(tamanio, int) or isinstance(tamanio, bool) or tamanio < 0:
            raise ValueError(f'Tamaño inválido para {item["ruta"]}.')
        ruta = Path(item['ruta']).expanduser().resolve()
        if str(ruta) in esperados:
            if esperados[str(ruta)] != {'sha256': sha, 'tamanio_bytes': tamanio}:
                raise ValueError(f'El JSON contiene datos contradictorios para {ruta}.')
            continue
        if not ruta.is_file():
            avisos.append(f'No se encontró el vídeo: {ruta}')
            continue
        if ruta.stat().st_size != tamanio:
            avisos.append(f'El tamaño cambió desde el reporte SHA256: {ruta}; vuelve a comparar.')
            continue
        candidatos.append(ruta)
        esperados[str(ruta)] = dict(sha256=sha, tamanio_bytes=tamanio)

    return datos, candidatos, esperados, avisos


def ultimo_reporte_comparacion(raiz):
    carpeta = Path(raiz) / 'reportes' / 'sha256' / 'json'
    if not carpeta.is_dir():
        return ''
    candidatos = []
    for ruta in carpeta.glob('sha256_*.json'):
        try:
            candidatos.append((ruta.stat().st_mtime_ns, ruta.name, ruta))
        except OSError:
            continue
    for _, _, ruta in sorted(candidatos, reverse=True):
        try:
            with ruta.open(encoding='utf-8-sig') as archivo:
                datos = json.load(archivo)
            if isinstance(datos, dict) and datos.get('modo') in ('comparar', 'offline') \
                    and isinstance(datos.get('nuevos'), list):
                return str(ruta)
        except (OSError, ValueError):
            continue
    return ''


def _cabecera_menu(raiz: Path) -> None:
    from core import __version__
    from core.database import info_dispositivo_actual, describir_conexion
    info = info_dispositivo_actual()
    if CONSOLA is not None and Rule is not None:
        CONSOLA.print(Rule(f'[bold bright_magenta]SHA256 DE CARPETAS Y ARCHIVOS MP4 — VIDUP v{__version__}[/]',
                           style='bright_cyan'))
        CONSOLA.print(
            f'[cyan]Dispositivo:[/] {rich_escape(str(info["dispositivo"]))}\n'
            f'[cyan]ID         :[/] {info["dispositivo_id"]}\n'
            f'[cyan]Plataforma :[/] {rich_escape(str(info["plataforma"]))} ({rich_escape(str(info["maquina"]))})\n'
            f'[cyan]Conexión   :[/] {describir_conexion()}'
        )
    else:
        print(f'\n{"=" * 72}')
        print(f'          SHA256 DE CARPETAS Y ARCHIVOS MP4 — VIDUP v{__version__}')
        print(f'{"=" * 72}')
        print(f'  Dispositivo actual : {info["dispositivo"]}')
        print(f'  ID dispositivo     : {info["dispositivo_id"]}')
        print(f'  Plataforma         : {info["plataforma"]} ({info["maquina"]})')
        print(f'  Modo de conexión   : {describir_conexion()}')
        print(f'{"=" * 72}')


def _elegir_carpeta_videos(raiz: Path) -> Path:
    """Pregunta qué carpeta analizar, ofreciendo atajos en Android."""
    candidatas = _carpetas_android_candidatas()

    if candidatas:
        _imprimir('\nCarpetas de vídeo detectadas en este dispositivo:', 'cyan')
        for i, c in enumerate(candidatas, 1):
            _imprimir(f'  {i}. {c}', 'white')
        _imprimir(f'  {len(candidatas) + 1}. Usar la carpeta de vidup.py ({raiz})', 'white')
        _imprimir(f'  {len(candidatas) + 2}. Otra carpeta (ruta manual)', 'white')

        opcion = int(_pedir_numero(
            'Elige una carpeta',
            1, 1, len(candidatas) + 2, entero=True,
        ))
        if opcion == len(candidatas) + 1:
            return raiz
        if opcion == len(candidatas) + 2:
            return Path(_pedir_ruta('Otra carpeta de vídeos MP4',
                                    existente=True, directorio=True))
        return candidatas[opcion - 1]

    # Comportamiento original cuando no es Android o no hay carpetas detectadas.
    if _pedir_si_no(f'¿Analizar la carpeta donde está vidup.py? ({raiz})', True):
        return raiz
    return Path(_pedir_ruta('Otra carpeta de vídeos MP4',
                            existente=True, directorio=True))


def _elegir_carpetas_videos(raiz: Path) -> List[Path]:
    """Permite seleccionar varias carpetas/unidades para UN solo informe.

    Conserva _elegir_carpeta_videos para el resto de opciones del menú.
    """
    carpetas = [_elegir_carpeta_videos(raiz).expanduser().resolve()]
    while True:
        _imprimir('\nRaíces seleccionadas para un único catálogo SHA256:', 'cyan')
        for n, carpeta in enumerate(carpetas, 1):
            _imprimir(f'  {n}. {carpeta}', 'white')
        if not _pedir_si_no('¿Agregar otra carpeta o unidad a este mismo reporte?', False):
            break
        nueva = Path(_pedir_ruta('Ruta adicional (por ejemplo D:\\Videos o E:\\)',
                                existente=True, directorio=True)).resolve()
        if nueva in carpetas:
            _imprimir('Esta ruta ya está seleccionada; no se añadirá otra vez.', 'yellow')
            continue
        carpetas.append(nueva)
    return carpetas


def menu(raiz=None, db_defecto=None):
    raiz = Path(raiz or Path(__file__).resolve().parents[1]).resolve()
    _asegurar_subcarpetas(raiz)

    try:
        while True:
            _cabecera_menu(raiz)
            opciones = (
                ('1', 'Comparar carpeta de vídeos directamente contra base(s) de datos'),
                ('2', 'Generar SHA256 de UNA O VARIAS carpetas/discos (JSON/TXT/CSV)'),
                ('3', 'Comparar JSON de SHA256 fuera de línea contra base(s)'),
                ('4', 'Cargar / Importar JSON de SHA256 fuera de línea a la BD'),
                ('5', 'Importar carpeta de vídeos como huellas mínimas'),
                ('00', 'Volver al menú principal'),
            )
            from core.interfaz import menu_rich
            menu_rich('Herramientas SHA256', opciones,
                      subtitulo='Catálogos de archivos y comparación fuera de línea.')
            _imprimir('💡 Las bases de datos pueden ser archivos .db (SQLite) o .json (huellas exportadas).', 'grey37')

            opcion = _pedir_texto('\n¿Qué quieres hacer? (1-5 / 00)', '1').zfill(1)
            if opcion in ('00', '0'):
                return None
            if opcion in ('1', '2', '3', '4', '5'):
                break
            _imprimir('Elige una opción válida: 1, 2, 3, 4, 5 o 00.', 'yellow')

        if opcion == '1':
            carpeta = _elegir_carpeta_videos(raiz)
            recursivo = _pedir_si_no('¿Incluir subcarpetas?', False)
            archivos = listar_mp4(carpeta, recursivo)

            sugerido = db_defecto or str(raiz / 'vidup.db')

            ruta_db = _elegir_archivo_reciente(
                raiz, 'Base de datos de VIDUP (SQLite .db o JSON de huellas)',
                ['*.db', '*.json'],
                ['respaldos/db', 'exportaciones/json'],
                defecto=sugerido,
            )
            extras = []
            while _pedir_si_no('¿Agregar otra base SQLite o JSON?', False):
                extra = _pedir_ruta('Base adicional (.db o .json)', existente=True)
                if extra not in [ruta_db] + extras:
                    extras.append(extra)
                else:
                    _imprimir('Esa base ya está seleccionada.', 'yellow')
            _imprimir(f'\nMP4 encontrados: {len(archivos)}\nCarpeta: {carpeta}\nBase de datos: {ruta_db}', 'cyan')
            resultado = modo_comparar(raiz, carpeta, recursivo, ruta_db, archivos, extras)

        elif opcion == '2':
            carpetas = _elegir_carpetas_videos(raiz)
            recursivo = _pedir_si_no('¿Incluir subcarpetas en todas las rutas?', True)
            archivos = listar_mp4_varias_carpetas(carpetas, recursivo)
            _imprimir(f'\nMP4 únicos encontrados: {len(archivos)}\nRaíces: {len(carpetas)}', 'cyan')
            for carpeta in carpetas:
                _imprimir(f'  - {carpeta}', 'white')
            resultado = modo_generar_offline(raiz, carpetas, recursivo, archivos)

        elif opcion == '3':
            sug_json = _archivos_recientes(raiz, ['sha256_offline_*.json'], ['sha256/json'])
            sug_def = str(sug_json[0]) if sug_json else ''
            ruta_json = _elegir_archivo_reciente(
                raiz, 'Archivo JSON de SHA256 fuera de línea',
                ['sha256_offline_*.json'],
                ['sha256/json'],
                defecto=sug_def,
            )
            sugerido = db_defecto or str(raiz / 'vidup.db')
            ruta_db = _elegir_archivo_reciente(
                raiz, 'Base de datos de VIDUP',
                ['*.db', '*.json'],
                ['respaldos/db', 'exportaciones/json'],
                defecto=sugerido,
            )
            extras = []
            while _pedir_si_no('¿Agregar otra base SQLite o JSON?', False):
                extra = _pedir_ruta('Base adicional (.db o .json)', existente=True)
                if extra not in [ruta_db] + extras:
                    extras.append(extra)
                else:
                    _imprimir('Esa base ya está seleccionada.', 'yellow')
            resultado = modo_comparar_offline_json(raiz, ruta_json, ruta_db, extras)

        elif opcion == '4':
            sug_off = _archivos_recientes(raiz, ['sha256_offline_*.json'], ['sha256/json'])
            sug_off_def = str(sug_off[0]) if sug_off else ''
            ruta_json = _elegir_archivo_reciente(
                raiz, 'Archivo JSON de SHA256 fuera de línea',
                ['sha256_offline_*.json'],
                ['sha256/json'],
                defecto=sug_off_def,
            )
            sugerido = db_defecto or str(raiz / 'vidup.db')
            ruta_db = _pedir_ruta('Base de datos SQLite donde guardar las huellas (.db)', sugerido)
            if Path(ruta_db).suffix.lower() == '.json':
                raise ValueError('Para importar huellas mínimas, selecciona una base SQLite (.db).')
            resultado = modo_importar_offline_json(raiz, ruta_json, ruta_db)

        elif opcion == '5':
            carpeta = _elegir_carpeta_videos(raiz)
            recursivo = _pedir_si_no('¿Incluir subcarpetas?', False)
            archivos = listar_mp4(carpeta, recursivo)
            sugerido = db_defecto or str(raiz / 'vidup.db')
            ruta_db = _pedir_ruta('Base de datos SQLite donde guardar las huellas (.db)', sugerido)
            if Path(ruta_db).suffix.lower() == '.json':
                raise ValueError('Para importar huellas mínimas, selecciona una base SQLite (.db).')
            _imprimir(f'\nMP4 encontrados: {len(archivos)}\nCarpeta: {carpeta}\nBase de datos: {ruta_db}', 'cyan')
            resultado = modo_importar(raiz, carpeta, recursivo, ruta_db, archivos)

        modo_actual = resultado.get('modo')
        tipo_informe = resultado.get('tipo_informe', '')
        if modo_actual == 'comparar':
            prefijo = 'sha256_comparar_offline' if tipo_informe == 'sha256_comparacion_offline' else 'sha256_comparar'
        elif modo_actual == 'offline':
            prefijo = 'sha256_offline'
        elif modo_actual == 'importar':
            prefijo = 'sha256_importar'
        else:
            prefijo = 'sha256_otro'

        sub = _asegurar_subcarpetas(raiz)
        identificador = _id_utc6()
        base = sub['json'] / f'{prefijo}_{identificador}.json'

        if resultado.get('interrumpido'):
            _imprimir(f'\nNo se generaron reportes finales porque la operación fue interrumpida.', 'yellow')
            return dict(resultado=resultado, ruta_json='', ruta_db=resultado.get('ruta_db', db_defecto))

        escribir_reporte_json(resultado, str(base))
        escribir_reporte_txt(resultado, str(sub['txt'] / f'{prefijo}_{identificador}.txt'))
        escribir_reporte_csv(resultado, str(sub['csv'] / f'{prefijo}_{identificador}.csv'))

        if modo_actual == 'offline' and not resultado.get('incompleto'):
            progreso = resultado.get('ruta_jsonl_progreso')
            if progreso:
                try:
                    Path(progreso).unlink(missing_ok=True)
                except OSError as exc:
                    _imprimir(f'No se pudo eliminar el progreso terminado: {exc}', 'yellow')

        _imprimir(f'Reportes JSON, TXT y CSV guardados en: {sub["json"].parent}', 'green')
        return dict(resultado=resultado, ruta_json=str(base), ruta_db=resultado.get('ruta_db', db_defecto))

    except RegresarAlMenu:
        _imprimir('\n↩ Operación cancelada. Regresando al menú principal...', 'yellow')
        return None


if __name__ == '__main__':
    try:
        menu()
    except (KeyboardInterrupt, EOFError, RegresarAlMenu):
        _imprimir('\nFinalizado.', 'cyan')