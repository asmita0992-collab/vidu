"""
Movimiento verificable de vídeos, sin borrado automático de duplicados.

Dos flujos:
  1. mover_interactivo()    → desde un JSON de comparación visual.
  2. mover_desde_sha256()   → desde un JSON de comparación SHA256.

El modo estricto restaura las cuatro fechas de Windows con FILE_BASIC_INFO.
En POSIX no se puede establecer ctime ni, normalmente, la fecha de creación:
el modo estricto se niega a mover. El modo compatible requiere elección expresa.
Los bytes del vídeo no se editan: sus fechas internas se conservan sin ExifTool.

Estructura de reportes:
  reportes/movimientos/json/    → resúmenes movimientos_*.json
  reportes/movimientos/txt/     → resúmenes movimientos_*.txt
  reportes/movimientos/jsonl/   → bitácoras movimientos_*.jsonl

Ctrl+C:
  - La bitácora .jsonl se cierra con flush + fsync antes de propagar.
  - El resumen JSON/TXT se genera con lo que haya hasta ese momento.
  - Los archivos ya movidos permanecen movidos; no se deshace nada.

La impresión usa rich si está disponible; si no, cae a ANSI puro.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import ntpath
import os
import posixpath
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
ZONA_MEXICO = timezone(timedelta(hours=-6))
def _id_utc6():
    return datetime.now(ZONA_MEXICO).strftime('%Y%m%d_%H%M%S')
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    TIENE_RICH = True
except ImportError:
    Console = None      # type: ignore
    Panel = None        # type: ignore
    Table = None        # type: ignore
    Rule = None         # type: ignore
    rich_escape = lambda texto: str(texto)
    rich_box = None     # type: ignore
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
DORADO = '\033[38;5;220m'


def _imprimir(mensaje: str = '', estilo: str = '') -> None:
    if CONSOLA is not None:
        CONSOLA.print(mensaje, style=estilo or None)
    else:
        prefijo = ''
        sufijo = ''
        mapa = {
            'red': (ROJO, NC), 'yellow': (AMARILLO, NC), 'green': (VERDE, NC),
            'cyan': (CIAN, NC), 'magenta': (MAGENTA, NC), 'grey58': (GRIS, NC),
            'bold': (NEGRITA, NC), 'gold': (DORADO, NC),
        }
        if estilo in mapa:
            prefijo, sufijo = mapa[estilo]
        print(f'{prefijo}{mensaje}{sufijo}')


def _regla(titulo: str = '', estilo: str = 'bright_cyan') -> None:
    if CONSOLA is not None and Rule is not None:
        CONSOLA.print(Rule(titulo, style=estilo) if titulo else Rule(style=estilo))
    else:
        print()
        if titulo:
            print(f'{CIAN}{NEGRITA}{titulo}{NC}')
        print('─' * 72)


# ============================================================================
# Excepciones
# ============================================================================
class FechasNoPreservables(OSError):
    pass


# ============================================================================
# Telegram (importado desde core.telegram)
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
# Windows: fechas de 64 bits (100 ns)
# ============================================================================
def _windows_basico(ruta, fechas=None):
    """Usa enteros de 64 bits (unidades de 100 ns), sin pérdida por float."""
    from ctypes import wintypes

    class BasicInfo(ctypes.Structure):
        _fields_ = [
            ('CreationTime', ctypes.c_int64),
            ('LastAccessTime', ctypes.c_int64),
            ('LastWriteTime', ctypes.c_int64),
            ('ChangeTime', ctypes.c_int64),
            ('FileAttributes', wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    for nombre in ('GetFileInformationByHandleEx', 'SetFileInformationByHandle'):
        funcion = getattr(kernel, nombre)
        funcion.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        funcion.restype = wintypes.BOOL

    acceso = 0x80 | (0x100 if fechas is not None else 0)
    handle = kernel.CreateFileW(
        str(Path(ruta).resolve()), acceso, 7, None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = BasicInfo()
        if fechas is None:
            correcto = kernel.GetFileInformationByHandleEx(
                handle, 0, ctypes.byref(info), ctypes.sizeof(info))
        else:
            for nombre in ('CreationTime', 'LastAccessTime', 'LastWriteTime', 'ChangeTime'):
                setattr(info, nombre, fechas[nombre])
            info.FileAttributes = 0
            correcto = kernel.SetFileInformationByHandle(
                handle, 0, ctypes.byref(info), ctypes.sizeof(info))
        if not correcto:
            raise ctypes.WinError(ctypes.get_last_error())
        return {
            campo[0]: int(getattr(info, campo[0]))
            for campo in BasicInfo._fields_ if campo[0] != 'FileAttributes'
        }
    finally:
        kernel.CloseHandle(handle)


def capturar_fechas(ruta):
    st = Path(ruta).stat()
    fechas: Dict[str, Any] = dict(
        acceso_ns=st.st_atime_ns,
        modificacion_ns=st.st_mtime_ns,
        cambio_metadatos_ns=st.st_ctime_ns,
    )
    if hasattr(st, 'st_birthtime_ns'):
        fechas['creacion_ns'] = st.st_birthtime_ns
    elif hasattr(st, 'st_birthtime'):
        fechas['creacion_ns'] = int(st.st_birthtime * 1_000_000_000)
    if os.name == 'nt':
        fechas['windows_100ns'] = _windows_basico(ruta)
    return fechas


def restaurar_fechas(ruta, fechas, estricto=True):
    if os.name == 'nt' and 'windows_100ns' in fechas:
        _windows_basico(ruta, fechas['windows_100ns'])
        actuales = _windows_basico(ruta)
        if actuales != fechas['windows_100ns']:
            raise FechasNoPreservables(
                'El destino no conservó exactamente las cuatro fechas de Windows.')
    else:
        if estricto:
            raise FechasNoPreservables(
                'Este sistema no permite restaurar todas las fechas (ctime/creación).')
        os.utime(ruta, ns=(fechas['acceso_ns'], fechas['modificacion_ns']))
        st = Path(ruta).stat()
        if (st.st_atime_ns, st.st_mtime_ns) != (
            fechas['acceso_ns'], fechas['modificacion_ns']
        ):
            raise FechasNoPreservables(
                'El destino no conserva la precisión de las fechas de acceso y modificación.')


# ============================================================================
# Fecha histórica (opcional, requiere ExifTool)
# ============================================================================
def obtener_fecha_mas_antigua(ruta, fechas=None):
    """Fecha visible opcional: mínimo entre filesystem y ExifTool.

    Devuelve nanosegundos y evidencia de los metadatos consultados.
    Las fechas sin zona horaria se interpretan en la zona local.
    """
    fechas = fechas or capturar_fechas(ruta)
    candidatas = [
        fechas[k] for k in ('acceso_ns', 'modificacion_ns', 'creacion_ns')
        if fechas.get(k, 0) > 0
    ]
    if fechas.get('windows_100ns'):
        w = fechas['windows_100ns']
        candidatas.extend(
            (w[k] - 116444736000000000) * 100
            for k in ('CreationTime', 'LastAccessTime', 'LastWriteTime')
            if w[k] > 116444736000000000
        )

    metadatos, avisos = {}, []
    exiftool = shutil.which('exiftool')
    if exiftool:
        try:
            respuesta = subprocess.run(
                [exiftool, '-j', '-G1:4', '-a', '-api', 'QuickTimeUTC=1',
                 '-Time:All', '-fast', str(Path(ruta).resolve())],
                capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=15,
            )
            if respuesta.returncode != 0:
                avisos.append(
                    f'ExifTool terminó con código {respuesta.returncode}: '
                    f'{respuesta.stderr.strip()}')
            elif respuesta.stdout.strip():
                objetos = json.loads(respuesta.stdout)
                if not isinstance(objetos, list) or not objetos or \
                        not isinstance(objetos[0], dict):
                    raise ValueError(
                        'La respuesta de ExifTool no contiene un objeto de metadatos válido.')
                metadatos = objetos[0]
                for valor in metadatos.values():
                    if not isinstance(valor, str):
                        continue
                    patron = re.match(
                        r'^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?',
                        valor)
                    if not patron:
                        continue
                    try:
                        partes = patron.groups()
                        texto = (
                            f'{partes[0]}-{partes[1]}-{partes[2]}T'
                            f'{partes[3]}:{partes[4]}:{partes[5]}'
                            + (partes[6] or '') + (partes[7] or '')
                        )
                        dt = datetime.fromisoformat(texto.replace('Z', '+00:00'))
                        if dt.year > 1971:
                            candidatas.append(int(dt.timestamp() * 1_000_000_000))
                    except (ValueError, OverflowError, OSError):
                        continue
        except (subprocess.TimeoutExpired, OSError, ValueError, TypeError, IndexError) as exc:
            avisos.append(f'No se pudieron consultar las fechas de ExifTool: {exc}')
    else:
        avisos.append('ExifTool no está instalado; se usan las fechas del sistema de archivos.')

    if not candidatas:
        raise ValueError('No hay ninguna fecha original válida para aplicar.')
    return min(candidatas), metadatos, avisos


# ============================================================================
# Helpers de hashing y sistema de archivos
# ============================================================================
def _sha256(ruta):
    h = hashlib.sha256()
    with Path(ruta).open('rb') as archivo:
        for bloque in iter(lambda: archivo.read(4 * 1024 * 1024), b''):
            h.update(bloque)
    return h.hexdigest()


def _identidad(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _destino_libre(carpeta, nombre):
    candidato = carpeta / nombre
    indice = 1
    while candidato.exists() or candidato.is_symlink():
        candidato = carpeta / f'{Path(nombre).stem}_{indice}{Path(nombre).suffix}'
        indice += 1
    return candidato


# ============================================================================
# Movimiento de un archivo
# ============================================================================
def mover_archivo(origen, destino, estricto=True, hash_esperado='',
                  tamanio_esperado=None, registrar=None, fecha_visible=False):
    """Verifica contenido y fechas antes de retirar el origen. No sobrescribe."""
    origen, destino = Path(origen), Path(destino)

    if fecha_visible and estricto:
        raise ValueError(
            'La fecha más antigua es una normalización opcional, '
            'incompatible con el modo estricto.')
    if estricto and os.name != 'nt':
        raise FechasNoPreservables(
            'Modo estricto disponible solo donde pueden restaurarse '
            'las cuatro fechas (Windows).')
    if origen.is_symlink() or not origen.is_file():
        raise ValueError(f'El origen no es un archivo regular o es un enlace: {origen}')
    if origen.resolve() == destino.resolve():
        raise ValueError('El origen y destino coinciden.')
    if destino.exists() or destino.is_symlink():
        raise FileExistsError(f'El destino ya existe: {destino}')

    antes = origen.stat()
    if not stat.S_ISREG(antes.st_mode):
        raise ValueError('Solo se admiten archivos regulares.')

    fechas = capturar_fechas(origen)
    registro = dict(
        origen=str(origen), destino=str(destino),
        fechas_originales=fechas,
        modo='estricto' if estricto else 'acceso y modificación',
        estado='preparado',
    )

    def evento(estado, **extra):
        registro.update(estado=estado, **extra)
        if registrar:
            registrar(dict(registro))

    evento('preparado')
    destino.parent.mkdir(parents=True, exist_ok=True)

    creado = renombrado = completado = False
    fechas_aplicar = dict(fechas)

    try:
        if fecha_visible:
            fecha_ns, metadatos, avisos = obtener_fecha_mas_antigua(origen, fechas)
            fechas_aplicar.update(acceso_ns=fecha_ns, modificacion_ns=fecha_ns)
            if 'windows_100ns' in fechas:
                ticks = fecha_ns // 100 + 116444736000000000
                fechas_aplicar['windows_100ns'] = dict(fechas['windows_100ns'])
                fechas_aplicar['windows_100ns'].update(
                    CreationTime=ticks,
                    LastAccessTime=ticks,
                    LastWriteTime=ticks,
                )
            evento(
                'fecha visible seleccionada',
                fecha_visible_ns=fecha_ns,
                fechas_metadatos=metadatos,
                avisos_fechas=avisos,
                modo='fecha más antigua',
            )

        sha = _sha256(origen)
        if _identidad(origen.stat()) != _identidad(antes):
            raise ValueError('El vídeo cambió mientras se verificaba; vuelve a comparar.')
        if hash_esperado and sha != hash_esperado:
            raise ValueError('El SHA256 difiere del reporte; vuelve a escanear y comparar.')
        if tamanio_esperado is not None and antes.st_size != tamanio_esperado:
            raise ValueError('El tamaño difiere del reporte; vuelve a comparar.')

        restaurar_fechas(origen, fechas, estricto)
        mismo_volumen = antes.st_dev == destino.parent.stat().st_dev
        evento('verificado', sha256=sha)

        if os.name == 'nt' and mismo_volumen:
            os.rename(origen, destino)
            renombrado = True
            evento('renombrado')
            restaurar_fechas(destino, fechas_aplicar, estricto)
        else:
            with origen.open('rb') as entrada, destino.open('xb') as salida:
                creado = True
                shutil.copyfileobj(entrada, salida, 4 * 1024 * 1024)
                salida.flush()
                os.fsync(salida.fileno())
            evento('copiado')
            if _sha256(destino) != sha:
                raise ValueError('La copia no coincide con el SHA256 del origen.')
            if _identidad(origen.stat()) != _identidad(antes) or _sha256(origen) != sha:
                raise ValueError('El origen cambió durante la copia; no se retirará.')
            shutil.copystat(origen, destino)
            restaurar_fechas(destino, fechas_aplicar, estricto)
            restaurar_fechas(origen, fechas, estricto)
            evento('copia y fechas verificadas',
                   fechas_destino=capturar_fechas(destino))
            origen.unlink()

        completado = True
        registro.update(
            estado='movido',
            fechas_destino=capturar_fechas(destino),
            sha256=sha,
        )
        try:
            evento('movido')
        except OSError as exc:
            registro['aviso'] = (
                f'Archivo movido; no se pudo registrar el último evento: {exc}')
        return registro

    except BaseException:
        if not completado:
            if renombrado and destino.exists() and not origen.exists():
                os.rename(destino, origen)
            elif creado and destino.exists():
                destino.unlink()
            if origen.exists() and _identidad(origen.stat()) == _identidad(antes):
                restaurar_fechas(origen, fechas, estricto)
        raise


# ============================================================================
# Plan de movimiento desde JSON de comparación visual
# ============================================================================
def crear_plan(datos):
    """Plan de movimiento que distingue dispositivos incluso con rutas idénticas."""
    if not isinstance(datos, dict) or not isinstance(datos.get('resultados'), list):
        raise ValueError('Selecciona un JSON de comparación VIDUP, no uno de huellas.')

    categorias = {}
    informacion = {}
    for hallazgo in datos['resultados']:
        if not isinstance(hallazgo, dict):
            raise ValueError('El reporte contiene hallazgos inválidos.')
        lados = {}
        for lado in ('a', 'b'):
            ruta = hallazgo.get('ruta_' + lado)
            if not ruta:
                raise ValueError('El reporte tiene rutas incompletas.')
            info = dict(hallazgo.get('info_' + lado) or {})
            clave = (info.get('dispositivo_id') or '', ruta)
            lados[lado] = clave
            informacion[clave] = info

        rec = hallazgo.get('recomendacion') or {}
        confirmado = hallazgo.get('tipo') in ('duplicado_exacto', 'duplicado_visual')
        conservar = (rec.get('conservar_dispositivo_id') or '', rec.get('conservar_ruta'))
        eliminar = (rec.get('eliminar_dispositivo_id') or '', rec.get('eliminar_ruta'))
        # Para reportes antiguos sin ID, localizar por ruta solo si no es ambigua.
        if confirmado and conservar[1] and eliminar[1]:
            for nombre, clave in [('conservar', conservar), ('eliminar', eliminar)]:
                posibles = [x for x in lados.values() if x == clave]
                if not posibles and not clave[0]:
                    posibles = [x for x in lados.values() if x[1] == clave[1]]
                if len(posibles) == 1:
                    categorias.setdefault(posibles[0], set()).add(nombre)
                else:
                    for x in lados.values():
                        categorias.setdefault(x, set()).add('revisar')
        else:
            for clave in lados.values():
                categorias.setdefault(clave, set()).add('revisar')

    plan = []
    for clave, etiquetas in categorias.items():
        info = informacion[clave]
        plan.append(dict(
            origen=clave[1],
            categoria=next(iter(etiquetas)) if len(etiquetas) == 1 else 'revisar',
            hash_esperado=info.get('hash_archivo', ''),
            tamanio_esperado=info.get('tamanio_bytes'),
            dispositivo_id=clave[0],
            dispositivo=info.get('dispositivo', ''),
        ))
    return sorted(plan, key=lambda x: (x['categoria'], x['dispositivo_id'], x['origen']))


def _actualizar_bases(bases, registros, dispositivo_id=None):
    """Actualiza las rutas de los vídeos movidos en una o varias bases SQLite o Neon."""
    from .database import conectar, cerrar, _usa_neon
    avisos = []
    for base in dict.fromkeys(bases):
        if not base or str(base).endswith('.json'):
            continue
        if base != 'neon_cloud' and not Path(base).is_file():
            continue

        conn = None
        try:
            conn = conectar(base)
            from .database import _es_pg
            is_pg = _es_pg(conn)
            
            if is_pg:
                conn.autocommit = False
                cursor = conn.cursor()
            else:
                cursor = conn.cursor()
                conn.execute('BEGIN IMMEDIATE')
            
            for r in registros:
                if r['estado'] != 'movido':
                    continue
                nuevo_nombre = Path(r['destino']).name
                nota = f"Movido a: {r.get('categoria', 'otra carpeta')}"
                
                query_sel = "SELECT id, dispositivo_id FROM videos WHERE ruta=" + ("%s" if is_pg else "?")
                cursor.execute(query_sel, (r['origen'],))
                filas = cursor.fetchall()
                
                for fila in filas:
                    vid_id = fila[0] if is_pg else fila['id']
                    d_id = fila[1] if is_pg else fila['dispositivo_id']
                    d_id_str = str(d_id or '').strip()
                    
                    if dispositivo_id and d_id_str != dispositivo_id:
                        continue
                        
                    nueva_clave = f"{d_id_str}|{r['destino']}" if d_id_str else r['destino']
                    # Evitar que la ruta antigua vuelva desde Neon al sincronizar.
                    if nueva_clave != (f"{d_id_str}|{r['origen']}" if d_id_str else r['origen']):
                        from core.sincronizacion import registrar_eliminacion
                        registrar_eliminacion(conn,
                            f"{d_id_str}|{r['origen']}" if d_id_str else r['origen'],
                            confirmar=False)
                    
                    query_upd = "UPDATE videos SET ruta=" + ("%s" if is_pg else "?") + ", nombre=" + ("%s" if is_pg else "?") + ", clave_unica=" + ("%s" if is_pg else "?") + ", error=" + ("%s" if is_pg else "?") + " WHERE id=" + ("%s" if is_pg else "?")
                    cursor.execute(query_upd, (r['destino'], nuevo_nombre, nueva_clave, nota, vid_id))
            
            conn.commit()
            if is_pg:
                cursor.close()
        except Exception as exc:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            avisos.append(f'No se actualizaron las rutas en {base}: {exc}')
        finally:
            if conn:
                cerrar(conn)
    return avisos


# ============================================================================
# Helpers de rutas de reportes
# ============================================================================
def _subcarpetas_movimientos(carpeta_reportes: Path) -> Dict[str, Path]:
    """Recibe reportes/movimientos y devuelve sus subcarpetas."""
    base = Path(carpeta_reportes)
    sub = {}
    for nombre in ('json', 'txt', 'jsonl'):
        p = base / nombre
        p.mkdir(parents=True, exist_ok=True)
        sub[nombre] = p
    return sub


def _modulo_ruta(ruta: str):
    """Elige el módulo de rutas sin depender del sistema que ejecuta VIDUP."""
    return ntpath if re.match(r'^[A-Za-z]:[\\/]', ruta) or '\\' in ruta else posixpath


def _raiz_visible(ruta: str) -> str:
    """Obtiene una raíz útil para agrupar rutas Android, POSIX o Windows."""
    modulo = _modulo_ruta(ruta)
    normalizada = modulo.normpath(ruta)
    partes = [p for p in re.split(r'[\\/]+', normalizada) if p]
    if modulo is posixpath and len(partes) >= 4 and partes[:3] == ['storage', 'emulated', '0']:
        return '/' + '/'.join(partes[:4])
    if modulo is ntpath and len(partes) >= 2:
        return modulo.join(partes[0] + modulo.sep, partes[1])
    padre = modulo.dirname(normalizada)
    return padre or normalizada


def _elegir_indices(total: int, mensaje: str) -> List[int]:
    """Lee una selección como 1,3-5 o T, con cancelación mediante 0."""
    for _ in range(5):
        try:
            respuesta = input(f'{mensaje} [T=todos, 0=cancelar]: ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            return []
        if respuesta in ('0', '00', 'cancelar'):
            return []
        if respuesta in ('t', 'todos', '*'):
            return list(range(total))
        elegidos = set()
        try:
            for parte in respuesta.split(','):
                limites = parte.strip().split('-', 1)
                inicio = int(limites[0])
                fin = int(limites[-1])
                if inicio > fin or inicio < 1 or fin > total:
                    raise ValueError
                elegidos.update(range(inicio - 1, fin))
            if elegidos:
                return sorted(elegidos)
        except ValueError:
            pass
        _imprimir('Selección inválida. Usa números, rangos como 2-4, T o 0.', 'yellow')
    return []


def _seleccionar_videos_huellas(videos: List[dict]) -> Tuple[List[dict], str, str]:
    """Selecciona vídeos del reporte por ruta base, carpeta exacta o archivo."""
    _regla('Seleccionar vídeos del dispositivo', 'bright_magenta')
    _imprimir('[bold deep_sky_blue1]Mover por[/]')
    _imprimir('  [bold gold1]1.[/] [cyan]Rutas completas[/] (incluye sus subcarpetas)')
    _imprimir('  [bold gold1]2.[/] [green]Carpetas[/] (carpeta padre exacta)')
    _imprimir('  [bold gold1]3.[/] [magenta]Archivos[/] (vídeos individuales)')
    try:
        modo = input('Opción [1]: ').strip() or '1'
    except (EOFError, KeyboardInterrupt):
        return [], '', ''
    if modo not in ('1', '2', '3'):
        _imprimir('Opción no válida.', 'yellow')
        return [], '', ''

    grupos: Dict[str, List[dict]] = {}
    if modo == '1':
        tipo = 'ruta'
        for video in videos:
            grupos.setdefault(_raiz_visible(str(video['ruta'])), []).append(video)
    elif modo == '2':
        tipo = 'carpeta'
        for video in videos:
            ruta = str(video['ruta'])
            grupos.setdefault(_modulo_ruta(ruta).dirname(ruta), []).append(video)
    else:
        tipo = 'archivo'
        grupos = {str(video['ruta']): [video] for video in videos}

    opciones = sorted(grupos.items(), key=lambda item: item[0].casefold())
    for indice, (ruta, elementos) in enumerate(opciones, 1):
        color = 'deep_sky_blue1' if tipo == 'ruta' else ('spring_green2' if tipo == 'carpeta' else 'plum1')
        _imprimir(f'  [bold gold1]{indice:>3}.[/] [{color}]{rich_escape(ruta)}[/] '
                  f'[grey70]({len(elementos)} vídeo(s))[/]')
        if tipo == 'archivo':
            detalle = elementos[0].get('detalle_faltantes') or 'sin detalle'
            _imprimir(f'       [orange3]Falta:[/] {rich_escape(str(detalle))}')

    indices = _elegir_indices(len(opciones), 'Selecciona una o varias opciones')
    seleccionados, valores = [], []
    for indice in indices:
        valor, elementos = opciones[indice]
        valores.append(valor)
        seleccionados.extend(elementos)
    unicos = {str(item['ruta']): item for item in seleccionados}
    return list(unicos.values()), tipo, '; '.join(valores)


def mover_desde_reporte_huellas(ruta_json, directorio_destino, carpeta_reportes,
                                 db_path=None):
    """Mueve vídeos del reporte 7.5 que pertenecen al dispositivo actual."""
    from .database import info_dispositivo_actual

    with Path(ruta_json).open(encoding='utf-8-sig') as archivo:
        datos = json.load(archivo)
    if not isinstance(datos, dict) or not isinstance(datos.get('dispositivos'), list):
        raise ValueError('Selecciona un JSON de vídeos faltantes de huellas generado por el submenú 7.5.')
    if not (datos.get('tipo_informe') == 'videos_faltantes_huellas'
            or 'total_videos_faltantes_huellas' in datos):
        raise ValueError('El JSON no corresponde a un reporte de vídeos faltantes de huellas.')

    actual = info_dispositivo_actual()
    actual_id = str(actual['dispositivo_id']).strip()
    dispositivo = next((item for item in datos['dispositivos']
                        if isinstance(item, dict)
                        and str(item.get('dispositivo_id') or '').strip() == actual_id), None)
    if dispositivo is None:
        incluidos = ', '.join(f'{d.get("dispositivo", "N/D")} (ID {d.get("dispositivo_id", "N/D")})'
                              for d in datos['dispositivos'] if isinstance(d, dict))
        raise ValueError(f'El reporte no contiene vídeos del dispositivo actual, {actual["dispositivo"]} '
                         f'(ID {actual_id}). Dispositivos incluidos: {incluidos or "ninguno"}.')

    videos = [v for v in dispositivo.get('videos', [])
              if isinstance(v, dict) and isinstance(v.get('ruta'), str) and v['ruta'].strip()]
    _imprimir(Panel.fit(
        f'[bold bright_white]{rich_escape(str(dispositivo.get("dispositivo") or actual["dispositivo"]))}[/]\n'
        f'[cyan]ID:[/] [spring_green2]{rich_escape(actual_id)}[/]\n'
        f'[cyan]Vídeos disponibles:[/] [bold gold1]{len(videos)}[/]',
        title='[bold plum1]Reporte 7.5 · dispositivo actual[/]', border_style='bright_cyan'
    ) if Panel is not None else f'Dispositivo: {actual["dispositivo"]} (ID {actual_id}); vídeos: {len(videos)}')
    if not videos:
        _imprimir('No hay vídeos válidos para mover en este dispositivo.', 'yellow')
        return

    seleccionados, tipo_seleccion, valor_seleccion = _seleccionar_videos_huellas(videos)
    if not seleccionados:
        _imprimir('Movimiento cancelado; no se seleccionaron vídeos.', 'yellow')
        return

    destino = Path(directorio_destino).expanduser().resolve()
    _imprimir(f'\n[cyan]Destino:[/] [bold spring_green2]{rich_escape(str(destino))}[/]')
    _imprimir(f'[cyan]Seleccionados:[/] [bold gold1]{len(seleccionados)} vídeo(s)[/]')
    _imprimir('  [gold1]1.[/] Estricto: conservar todas las fechas (Windows).')
    _imprimir('  [gold1]2.[/] Compatible: conservar acceso y modificación.')
    _imprimir('  [gold1]3.[/] Fecha histórica: aplicar la fecha más antigua.')
    modo = _pedir_modo('3')
    if modo == '0' or not _pedir_confirmacion():
        return
    estricto = modo == '1'
    if estricto and os.name != 'nt':
        _imprimir('Movimiento cancelado: el modo estricto solo está disponible en Windows.', 'red')
        return

    sub = _subcarpetas_movimientos(Path(carpeta_reportes))
    identificador = _id_utc6()
    bitacora = sub['jsonl'] / f'movimientos_huellas_{identificador}.jsonl'
    registros = []
    inicio = time.monotonic()
    with bitacora.open('x', encoding='utf-8') as log:
        def registrar(evento):
            evento = dict(evento, dispositivo_id=actual_id,
                          dispositivo=dispositivo.get('dispositivo') or actual['dispositivo'])
            log.write(json.dumps(dict(fecha=datetime.now(ZONA_MEXICO).isoformat(), **evento),
                                     ensure_ascii=False) + '\n')
            log.flush()
            try:
                os.fsync(log.fileno())
            except OSError:
                pass

        for indice, video in enumerate(seleccionados, 1):
            origen = Path(video['ruta'])
            try:
                objetivo = _destino_libre(destino, origen.name)
                resultado = mover_archivo(
                    origen, objetivo, estricto, '', video.get('tamanio_bytes'),
                    registrar, fecha_visible=modo == '3')
            except (OSError, ValueError) as exc:
                resultado = {'origen': str(origen), 'estado': 'omitido', 'error': str(exc)}
                registrar(resultado)
            resultado.update(
                dispositivo_id=actual_id,
                dispositivo=dispositivo.get('dispositivo') or actual['dispositivo'],
                tamanio_bytes=video.get('tamanio_bytes'),
                categoria='huellas_faltantes', seleccion_tipo=tipo_seleccion,
                seleccion_valor=valor_seleccion,
            )
            registros.append(resultado)
            color = 'spring_green2' if resultado['estado'] == 'movido' else 'orange3'
            _imprimir(f'[{color}][{indice}/{len(seleccionados)}] {resultado["estado"]}:[/] '
                      f'[bright_white]{rich_escape(origen.name)}[/]')

    avisos = _actualizar_bases([db_path] if db_path else [], registros, dispositivo_id=actual_id)
    resumen = {
        'tipo_informe': 'movimiento_videos_faltantes_huellas',
        'operacion': 'movimiento', 'fecha': datetime.now(ZONA_MEXICO).isoformat(),
        'reporte_origen': str(ruta_json), 'destino': str(destino),
        'dispositivo_origen': dispositivo, 'dispositivo_ejecutor': actual,
        'seleccion_tipo': tipo_seleccion, 'seleccion_valor': valor_seleccion,
        'registros': registros, 'avisos': avisos, 'bitacora': str(bitacora),
        'tiempo_seg': time.monotonic() - inicio,
    }
    ruta_resumen = sub['json'] / f'movimientos_huellas_{identificador}.json'
    ruta_resumen.write_text(json.dumps(resumen, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    ruta_txt = sub['txt'] / f'movimientos_huellas_{identificador}.txt'
    lineas = ['MOVIMIENTO DESDE REPORTE DE HUELLAS — VIDUP',
              f'Dispositivo: {dispositivo.get("dispositivo")} (ID {actual_id})',
              f'Reporte: {ruta_json}', f'Destino: {destino}',
              f'Selección: {tipo_seleccion} — {valor_seleccion}', '']
    lineas.extend(f'{r["estado"].upper()} | {r["origen"]} | {r.get("destino", "-")}' for r in registros)
    lineas.extend(f'AVISO | {aviso}' for aviso in avisos)
    ruta_txt.write_text('\n'.join(lineas) + '\n', encoding='utf-8')
    movidos = sum(r['estado'] == 'movido' for r in registros)
    _imprimir(f'\n[bold spring_green2]Movimiento terminado: {movidos} movido(s), '
              f'{len(registros) - movidos} omitido(s).[/]')
    _imprimir(f'[cyan]Resumen JSON:[/] [bright_white]{rich_escape(str(ruta_resumen))}[/]')
    _imprimir(f'[cyan]Resumen TXT :[/] [bright_white]{rich_escape(str(ruta_txt))}[/]')


# ============================================================================
# Modo: mover_interactivo
# ============================================================================
def mover_interactivo(ruta_reporte, directorio_destino, carpeta_reportes, db_path=None):
    from .database import info_dispositivo_actual
    from .reporter import _fmt_seg

    with Path(ruta_reporte).open(encoding='utf-8-sig') as archivo:
        datos = json.load(archivo)

    disp_reporte = (datos.get('metadatos') or {}).get('dispositivo') or {}
    disp_actual = info_dispositivo_actual()
    disp_id_actual = disp_actual['dispositivo_id']

    _imprimir(f'\nDispositivo actual     : {disp_actual["dispositivo"]} '
              f'(ID {disp_id_actual})', 'cyan')
    if disp_reporte:
        _imprimir(f'Dispositivo del reporte: '
                  f'{disp_reporte.get("dispositivo", "?")} '
                  f'(ID {disp_reporte.get("dispositivo_id", "?")})', 'cyan')

    _notificar_inicio('Mover vídeos (comparativa)',
                      f'{len(datos.get("resultados", []))} hallazgos')

    exactos = [h for h in datos.get("resultados", []) if h.get("tipo") == "duplicado_exacto"]
    if exactos:
        _imprimir(f'\nSe encontraron {len(exactos)} duplicados exactos.', 'cyan')
        _imprimir('¿Cómo prefieres decidir cuál archivo de cada par se moverá?', 'white')
        _imprimir('  1. Usar la recomendación automática del reporte (por defecto)', 'white')
        _imprimir('  2. Elegir manualmente par por par', 'white')
        _imprimir('  3. Priorizar por ruta (ej. conservar los de Ubuntu y mover los de Windows)', 'white')
        
        intentos = 0
        opcion_exactos = '1'
        while intentos < 5:
            try:
                resp = input('Opción [1]: ').strip() or '1'
            except (EOFError, KeyboardInterrupt):
                resp = '1'
            if resp in ('1', '2', '3'):
                opcion_exactos = resp
                break
            intentos += 1
            
        if opcion_exactos == '2':
            for i, h in enumerate(exactos, 1):
                ra = h.get('ruta_a', '')
                rb = h.get('ruta_b', '')
                _imprimir(f'\nPar {i}/{len(exactos)}:', 'cyan')
                _imprimir(f'  1. Conservar: {ra}\n     Mover    : {rb}', 'green')
                _imprimir(f'  2. Conservar: {rb}\n     Mover    : {ra}', 'yellow')
                
                eleccion = '1'
                for _ in range(5):
                    try:
                        eleccion = input('Opción [1]: ').strip() or '1'
                    except (EOFError, KeyboardInterrupt):
                        break
                    if eleccion in ('1', '2'):
                        break
                
                rec = h.get('recomendacion', {})
                if eleccion == '1':
                    rec.update(conservar_ruta=ra, eliminar_ruta=rb,
                               conservar_dispositivo_id=(h.get('info_a') or {}).get('dispositivo_id', ''),
                               eliminar_dispositivo_id=(h.get('info_b') or {}).get('dispositivo_id', ''),
                               conservar_nombre=h.get('nombre_a', ''), eliminar_nombre=h.get('nombre_b', ''))
                else:
                    rec.update(conservar_ruta=rb, eliminar_ruta=ra,
                               conservar_dispositivo_id=(h.get('info_b') or {}).get('dispositivo_id', ''),
                               eliminar_dispositivo_id=(h.get('info_a') or {}).get('dispositivo_id', ''),
                               conservar_nombre=h.get('nombre_b', ''), eliminar_nombre=h.get('nombre_a', ''))
                h['recomendacion'] = rec

        elif opcion_exactos == '3':
            _imprimir('\nEscribe un fragmento de ruta para filtrar (ej. "Windows" o "/home/haggy"):', 'cyan')
            try:
                filtro = input('Fragmento: ').strip()
            except (EOFError, KeyboardInterrupt):
                filtro = ''
            
            if filtro:
                _imprimir('Si la ruta contiene ese fragmento, ¿quieres CONSERVARLA en su lugar o MOVERLA?', 'cyan')
                _imprimir('  1. Conservarla', 'white')
                _imprimir('  2. Moverla', 'white')
                
                accion = '1'
                for _ in range(5):
                    try:
                        accion = input('Opción [1]: ').strip() or '1'
                    except (EOFError, KeyboardInterrupt):
                        break
                    if accion in ('1', '2'):
                        break
                
                modificados = 0
                for h in exactos:
                    ra, rb = h.get('ruta_a', ''), h.get('ruta_b', '')
                    a_tiene, b_tiene = filtro in ra, filtro in rb
                    
                    if a_tiene == b_tiene:
                        continue
                        
                    preferido_a = (a_tiene and accion == '1') or (b_tiene and accion == '2')
                        
                    rec = h.get('recomendacion', {})
                    if preferido_a:
                        rec.update(conservar_ruta=ra, eliminar_ruta=rb,
                                   conservar_dispositivo_id=(h.get('info_a') or {}).get('dispositivo_id', ''),
                                   eliminar_dispositivo_id=(h.get('info_b') or {}).get('dispositivo_id', ''),
                                   conservar_nombre=h.get('nombre_a', ''), eliminar_nombre=h.get('nombre_b', ''))
                    else:
                        rec.update(conservar_ruta=rb, eliminar_ruta=ra,
                                   conservar_dispositivo_id=(h.get('info_b') or {}).get('dispositivo_id', ''),
                                   eliminar_dispositivo_id=(h.get('info_a') or {}).get('dispositivo_id', ''),
                                   conservar_nombre=h.get('nombre_b', ''), eliminar_nombre=h.get('nombre_a', ''))
                    h['recomendacion'] = rec
                    modificados += 1
                
                _imprimir(f'Se reasignaron las prioridades de {modificados} pares.', 'green')

    plan = crear_plan(datos)

    plan_local = []
    plan_ajeno = []
    for item in plan:
        d_id = item.get('dispositivo_id') or ''
        if d_id and d_id != disp_id_actual:
            plan_ajeno.append(item)
        else:
            if item.get('categoria') == 'eliminar':
                plan_local.append(item)

    if plan_ajeno:
        _imprimir(f'\n⚠️ {len(plan_ajeno)} vídeo(s) del reporte pertenecen a otro '
                  f'dispositivo y se omitirán:', 'yellow')
        for item in plan_ajeno[:5]:
            _imprimir(f'   • {item["origen"]}  [dispositivo_id={item["dispositivo_id"]}]', 'grey58')
        if len(plan_ajeno) > 5:
            _imprimir(f'   ... y {len(plan_ajeno) - 5} más.', 'grey58')

    plan = plan_local
    if not plan:
        _imprimir('\nNo quedan vídeos marcados para eliminar en este dispositivo.', 'yellow')
        return

    destino = Path(directorio_destino).expanduser().resolve()
    _imprimir('\nPlan de movimiento (solo este dispositivo):', 'cyan')
    for categoria in ('conservar', 'eliminar', 'revisar'):
        _imprimir(f'  {categoria}: '
                  f'{sum(x["categoria"] == categoria for x in plan)} vídeos', 'white')
    _imprimir(f'Destino: {destino}', 'white')
    _imprimir('La carpeta eliminar contiene archivos para revisión; '
              'VIDUP no los borra.', 'grey58')

    _imprimir('\n1. Estricto: conservar y verificar todas las fechas (Windows).', 'white')
    _imprimir('2. Compatible: conservar acceso y modificación; '
              'registrar otras fechas originales.', 'white')
    _imprimir('3. Fecha histórica (predeterminado): aplicar la fecha más antigua '
              'del filesystem y metadatos MP4.', 'white')
    _imprimir('0. Cancelar.', 'white')
    if os.name != 'nt':
        _imprimir('Linux/Android no permiten restaurar ctime ni garantizar '
                  'creación al copiar. El modo 1 no moverá archivos.', 'yellow')

    modo = _pedir_modo('3')
    if modo == '0':
        return
    estricto = modo == '1'
    if estricto and os.name != 'nt':
        _imprimir('Movimiento cancelado: este sistema no puede preservar todas '
                  'las fechas en modo estricto.', 'red')
        return

    if not _pedir_confirmacion():
        return

    sub = _subcarpetas_movimientos(carpeta_reportes)
    identificador = _id_utc6()
    bitacora = sub['jsonl'] / f'movimientos_{identificador}.jsonl'
    registros, identidades = [], set()
    inicio = time.monotonic()

    manejador_anterior = signal.getsignal(signal.SIGINT)
    interrumpido = False

    def manejar_ctrl_c(signum, frame):
        nonlocal interrumpido
        if not interrumpido:
            interrumpido = True
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, manejar_ctrl_c)

    try:
        with bitacora.open('x', encoding='utf-8') as log:
            def registrar(evento):
                log.write(json.dumps(dict(
                    fecha=datetime.now(ZONA_MEXICO).isoformat(), **evento),
                    ensure_ascii=False) + '\n')
                log.flush()
                try:
                    os.fsync(log.fileno())
                except OSError:
                    pass

            for indice, item in enumerate(plan, 1):
                if interrumpido:
                    break
                origen = Path(item['origen'])
                categoria = item['categoria']
                try:
                    st = origen.stat()
                    identidad = (st.st_dev, st.st_ino) if st.st_ino else str(origen.resolve())
                    if identidad in identidades:
                        raise ValueError('Alias de un archivo que ya se procesó.')
                    identidades.add(identidad)
                    carpeta = destino / categoria
                    carpeta.mkdir(parents=True, exist_ok=True)
                    objetivo = _destino_libre(carpeta, origen.name)
                    resultado = mover_archivo(
                        origen, objetivo, estricto,
                        item['hash_esperado'], item['tamanio_esperado'],
                        registrar, fecha_visible=modo == '3',
                    )
                except KeyboardInterrupt:
                    interrumpido = True
                    break
                except (OSError, ValueError) as exc:
                    resultado = dict(
                        origen=str(origen), categoria=categoria,
                        estado='omitido', error=str(exc))
                    registrar(resultado)
                resultado['categoria'] = categoria
                registros.append(resultado)
                _imprimir(f'[{indice}/{len(plan)}] {resultado["estado"]}: {origen.name}',
                          'green' if resultado['estado'] == 'movido' else 'yellow')
    finally:
        signal.signal(signal.SIGINT, manejador_anterior)

    bases = datos.get('metadatos', {}).get('bases_de_datos', [])
    if db_path and db_path not in bases:
        bases.append(db_path)
    avisos = _actualizar_bases(bases, registros, dispositivo_id=disp_id_actual)

    resumen = dict(
        reporte_origen=str(ruta_reporte),
        destino=str(destino),
        registros=registros,
        avisos=avisos,
        bitacora=str(bitacora),
        tiempo_seg=time.monotonic() - inicio,
        dispositivo_ejecutor=disp_actual,
        dispositivo_reporte=disp_reporte,
        interrumpido=interrumpido,
    )
    base = sub['json'] / f'movimientos_{identificador}'
    base.with_suffix('.json').write_text(
        json.dumps(resumen, ensure_ascii=False, indent=4) + '\n',
        encoding='utf-8',
    )

    _escribir_resumen_txt_visual(
        sub['txt'] / f'movimientos_{identificador}.txt',
        ruta_reporte, disp_actual, disp_reporte, destino, registros, avisos,
    )

    movidos = sum(r['estado'] == 'movido' for r in registros)
    omitidos = sum(r['estado'] != 'movido' for r in registros)
    _imprimir(f'\nMovidos: {movidos}; omitidos: {omitidos}',
              'green' if not interrumpido else 'yellow')
    _imprimir(f'Bitácora: {bitacora}', 'grey58')
    _imprimir(f'Resumen:  {base}.json / {base}.txt', 'grey58')
    for aviso in avisos:
        _imprimir(aviso, 'yellow')

    _notificar_fin('Mover vídeos (comparativa)', not interrumpido,
                   f'{movidos} movidos, {omitidos} omitidos')


def _escribir_resumen_txt_visual(destino, ruta_reporte, disp_actual, disp_reporte,
                                  destino_path, registros, avisos):
    from .reporter import _fmt_seg
    lineas = [
        'MOVIMIENTO DE VÍDEOS — VIDUP',
        f'Reporte de origen: {ruta_reporte}',
        f'Dispositivo ejecutor : {disp_actual["dispositivo"]} '
        f'(ID {disp_actual["dispositivo_id"]})',
        f'Plataforma           : {disp_actual["plataforma"]} '
        f'({disp_actual["maquina"]})',
    ]
    if disp_reporte:
        lineas.append(
            f'Dispositivo del reporte: '
            f'{disp_reporte.get("dispositivo", "N/D")} '
            f'(ID {disp_reporte.get("dispositivo_id", "N/D")})')
    lineas.extend([
        f'Destino: {destino_path}',
        f'Tiempo: {_fmt_seg(sum(r.get("tiempo_seg", 0) for r in registros))}',
        '',
    ])

    nombres = {
        'acceso_ns': 'Acceso', 'modificacion_ns': 'Modificación',
        'cambio_metadatos_ns': 'Cambio de metadatos', 'creacion_ns': 'Creación',
    }
    etiquetas_windows = {
        'CreationTime': 'Creación', 'LastAccessTime': 'Acceso',
        'LastWriteTime': 'Modificación', 'ChangeTime': 'Cambio de metadatos',
    }
    for r in registros:
        lineas.extend([
            f'Estado: {r["estado"]}',
            f'Origen: {r["origen"]}',
            f'Destino: {r.get("destino", "Sin movimiento")}',
            f'Categoría: {r["categoria"]}',
            f'Error: {r.get("error", "Ninguno")}',
        ])
        if r.get('aviso'):
            lineas.append('Aviso: ' + r['aviso'])
        if r.get('fecha_visible_ns'):
            lineas.append(
                'Fecha visible aplicada: '
                + datetime.fromtimestamp(r['fecha_visible_ns'] / 1e9,
                                          timezone.utc).isoformat())
        for aviso in r.get('avisos_fechas', []):
            lineas.append('Aviso de fechas: ' + aviso)
        if r.get('fechas_metadatos'):
            lineas.append('Fechas consultadas en metadatos (ExifTool):')
            lineas.extend(f'  {clave}: {valor}' for clave, valor in r['fechas_metadatos'].items())
        for clave in ('fechas_originales', 'fechas_destino'):
            if clave in r:
                lineas.append('Fechas originales:' if clave == 'fechas_originales'
                              else 'Fechas en destino:')
                for campo, valor in r[clave].items():
                    if campo == 'windows_100ns':
                        for campo_w, ticks in valor.items():
                            fecha = datetime.fromtimestamp(
                                (ticks - 116444736000000000) / 1e7, timezone.utc
                            ).isoformat()
                            lineas.append(
                                f'  {etiquetas_windows[campo_w]} (Windows): '
                                f'{fecha} UTC (valor exacto: {ticks} unidades de 100 ns)')
                    else:
                        fecha = datetime.fromtimestamp(valor / 1e9, timezone.utc).isoformat()
                        lineas.append(
                            f'  {nombres.get(campo, campo)}: {fecha} UTC '
                            f'(valor exacto: {valor} ns)')
        lineas.append('')
    lineas.extend(avisos)

    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text('\n'.join(lineas) + '\n', encoding='utf-8')


# ============================================================================
# Modo: mover_desde_sha256
# ============================================================================
def mover_desde_sha256(ruta_json, directorio_destino, carpeta_reportes,
                       grupos=('nuevos',), db_path=None):
    """Mueve vídeos listados en un JSON de comparación SHA256."""
    from .database import info_dispositivo_actual
    from .reporter import _fmt_seg

    with Path(ruta_json).open(encoding='utf-8-sig') as archivo:
        datos = json.load(archivo)

    if not isinstance(datos, dict):
        raise ValueError('El JSON no es válido.')

    tipo = datos.get('tipo_informe', '')
    if tipo not in ('sha256_carpeta', 'sha256_comparacion_offline'):
        raise ValueError(
            'Este reporte no es de comparación SHA256. '
            'Usa el JSON generado por la opción de SHA256.')

    disp_actual = info_dispositivo_actual()
    disp_id_actual = disp_actual['dispositivo_id']

    items: List[dict] = []
    for grupo in grupos:
        if grupo not in ('nuevos', 'ya_registrados_aqui', 'coincidencias'):
            raise ValueError(f'Grupo desconocido: {grupo}')
        lista = datos.get(grupo) or []
        if not isinstance(lista, list):
            raise ValueError(f'El campo "{grupo}" del JSON no es una lista.')
        for item in lista:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item['_grupo'] = grupo
            items.append(item)

    if not items:
        _imprimir('No hay vídeos que mover en los grupos seleccionados.', 'yellow')
        return

    disp_origen_json = datos.get('dispositivo_actual') or datos.get('dispositivo') or {}
    disp_id_json = disp_origen_json.get('dispositivo_id', '') \
        if isinstance(disp_origen_json, dict) else ''
    if disp_id_json and disp_id_json != disp_id_actual:
        _imprimir(f'\n⚠️ ADVERTENCIA DE DISPOSITIVO', 'yellow')
        _imprimir(f'  El JSON fue generado en: '
                  f'{disp_origen_json.get("dispositivo", "?")} (ID {disp_id_json})', 'yellow')
        _imprimir(f'  Dispositivo actual    : {disp_actual["dispositivo"]} '
                  f'(ID {disp_id_actual})', 'yellow')
        if not _pedir_confirmacion():
            _imprimir('Movimiento cancelado por seguridad.', 'yellow')
            return

    existentes = []
    omitidos_por_falta = 0
    for item in items:
        ruta = item.get('ruta', '')
        if not ruta:
            continue
        p = Path(ruta)
        if not p.is_file():
            omitidos_por_falta += 1
            continue
        existentes.append(item)

    if omitidos_por_falta:
        _imprimir(f'\n⚠️ {omitidos_por_falta} vídeo(s) del JSON no existen '
                  f'en disco y se omitirán.', 'yellow')
    if not existentes:
        _imprimir('No queda ningún vídeo válido para mover.', 'yellow')
        return

    destino = Path(directorio_destino).expanduser().resolve()
    _imprimir('\nPlan de movimiento desde JSON SHA256:', 'cyan')
    _imprimir(f'  JSON de origen : {ruta_json}', 'white')
    _imprimir(f'  Destino        : {destino}', 'white')
    for grupo in grupos:
        n = sum(1 for x in existentes if x.get('_grupo') == grupo)
        _imprimir(f'  {grupo:<22}: {n} vídeos', 'white')
    _imprimir(f'  Omitidos (no existen): {omitidos_por_falta}', 'grey58')
    _imprimir('La operación no borra archivos; los mueve y verifica SHA256.', 'grey58')

    _imprimir('\n1. Estricto: conservar y verificar todas las fechas (Windows).', 'white')
    _imprimir('2. Compatible: conservar acceso y modificación.', 'white')
    _imprimir('3. Fecha histórica (predeterminado): aplicar la fecha más antigua.', 'white')
    _imprimir('0. Cancelar.', 'white')
    if os.name != 'nt':
        _imprimir('Linux/Android no permiten restaurar ctime ni garantizar '
                  'creación al copiar. El modo 1 no moverá archivos.', 'yellow')

    modo = _pedir_modo('3')
    if modo == '0':
        return
    estricto = modo == '1'
    if estricto and os.name != 'nt':
        _imprimir('Movimiento cancelado: este sistema no puede preservar todas '
                  'las fechas en modo estricto.', 'red')
        return

    if not _pedir_confirmacion():
        return

    sub = _subcarpetas_movimientos(carpeta_reportes)
    identificador = _id_utc6()
    bitacora = sub['jsonl'] / f'movimientos_sha256_{identificador}.jsonl'
    registros: List[dict] = []
    identidades = set()
    inicio = time.monotonic()

    manejador_anterior = signal.getsignal(signal.SIGINT)
    interrumpido = False

    def manejar_ctrl_c(signum, frame):
        nonlocal interrumpido
        if not interrumpido:
            interrumpido = True
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, manejar_ctrl_c)

    try:
        with bitacora.open('x', encoding='utf-8') as log:
            def registrar(evento):
                log.write(json.dumps(dict(
                    fecha=datetime.now(ZONA_MEXICO).isoformat(), **evento),
                    ensure_ascii=False) + '\n')
                log.flush()
                try:
                    os.fsync(log.fileno())
                except OSError:
                    pass

            for indice, item in enumerate(existentes, 1):
                if interrumpido:
                    break
                origen = Path(item['ruta'])
                grupo = item.get('_grupo', 'nuevos')
                try:
                    st = origen.stat()
                    identidad = (st.st_dev, st.st_ino) if st.st_ino else str(origen.resolve())
                    if identidad in identidades:
                        raise ValueError('Alias de un archivo ya procesado.')
                    identidades.add(identidad)

                    carpeta_grupo = destino / grupo
                    carpeta_grupo.mkdir(parents=True, exist_ok=True)
                    objetivo = _destino_libre(carpeta_grupo, origen.name)

                    resultado = mover_archivo(
                        origen, objetivo, estricto,
                        item.get('sha256', ''), item.get('tamanio_bytes'),
                        registrar, fecha_visible=(modo == '3'),
                    )
                except KeyboardInterrupt:
                    interrumpido = True
                    break
                except (OSError, ValueError) as exc:
                    resultado = dict(
                        origen=str(origen), categoria=grupo,
                        estado='omitido', error=str(exc))
                    registrar(resultado)
                resultado['categoria'] = grupo
                registros.append(resultado)
                _imprimir(f'[{indice}/{len(existentes)}] {resultado["estado"]}: {origen.name}',
                          'green' if resultado['estado'] == 'movido' else 'yellow')
    finally:
        signal.signal(signal.SIGINT, manejador_anterior)

    bases = datos.get('bases_de_datos') or ([datos['ruta_db']] if datos.get('ruta_db') else [])
    if db_path and db_path not in bases:
        bases.append(db_path)
    avisos = _actualizar_bases(bases, registros, dispositivo_id=disp_id_actual)

    resumen = dict(
        reporte_origen=str(ruta_json),
        destino=str(destino),
        grupos=list(grupos),
        registros=registros,
        avisos=avisos,
        bitacora=str(bitacora),
        tiempo_seg=time.monotonic() - inicio,
        dispositivo_ejecutor=disp_actual,
        dispositivo_reporte=disp_origen_json,
        interrumpido=interrumpido,
    )
    base = sub['json'] / f'movimientos_sha256_{identificador}'
    base.with_suffix('.json').write_text(
        json.dumps(resumen, ensure_ascii=False, indent=4) + '\n',
        encoding='utf-8',
    )

    _escribir_resumen_txt_sha256(
        sub['txt'] / f'movimientos_sha256_{identificador}.txt',
        ruta_json, disp_actual, destino, grupos, registros, avisos,
    )

    movidos = sum(1 for r in registros if r['estado'] == 'movido')
    omitidos = len(registros) - movidos
    _imprimir(f'\nMovidos: {movidos}; omitidos: {omitidos}',
              'green' if not interrumpido else 'yellow')
    _imprimir(f'Bitácora : {bitacora}', 'grey58')
    _imprimir(f'Resumen  : {base}.json / {base}.txt', 'grey58')
    for aviso in avisos:
        _imprimir(aviso, 'yellow')

    _notificar_fin('Mover desde JSON SHA256', not interrumpido,
                   f'{movidos} movidos, {omitidos} omitidos')


def _escribir_resumen_txt_sha256(destino, ruta_json, disp_actual, destino_path,
                                  grupos, registros, avisos):
    from .reporter import _fmt_seg
    lineas = [
        'MOVIMIENTO DE VÍDEOS DESDE JSON SHA256 — VIDUP',
        f'Reporte de origen    : {ruta_json}',
        f'Dispositivo ejecutor : {disp_actual["dispositivo"]} '
        f'(ID {disp_actual["dispositivo_id"]})',
        f'Destino              : {destino_path}',
        f'Grupos movidos       : {", ".join(grupos)}',
        f'Tiempo               : '
        f'{_fmt_seg(sum(r.get("tiempo_seg", 0) for r in registros))}',
        '',
    ]
    nombres_fecha = {
        'acceso_ns': 'Acceso', 'modificacion_ns': 'Modificación',
        'cambio_metadatos_ns': 'Cambio de metadatos', 'creacion_ns': 'Creación',
    }
    etiquetas_w = {
        'CreationTime': 'Creación', 'LastAccessTime': 'Acceso',
        'LastWriteTime': 'Modificación', 'ChangeTime': 'Cambio de metadatos',
    }
    for r in registros:
        lineas.extend([
            f'Estado    : {r["estado"]}',
            f'Grupo     : {r["categoria"]}',
            f'Origen    : {r["origen"]}',
            f'Destino   : {r.get("destino", "Sin movimiento")}',
            f'Error     : {r.get("error", "Ninguno")}',
        ])
        if r.get('aviso'):
            lineas.append('Aviso: ' + r['aviso'])
        if r.get('fecha_visible_ns'):
            lineas.append(
                'Fecha visible aplicada: '
                + datetime.fromtimestamp(r['fecha_visible_ns'] / 1e9,
                                          timezone.utc).isoformat())
        for aviso in r.get('avisos_fechas', []):
            lineas.append('Aviso de fechas: ' + aviso)
        if r.get('fechas_metadatos'):
            lineas.append('Fechas consultadas en metadatos (ExifTool):')
            lineas.extend(f'  {clave}: {valor}' for clave, valor in r['fechas_metadatos'].items())
        for clave in ('fechas_originales', 'fechas_destino'):
            if clave in r:
                lineas.append('Fechas originales:' if clave == 'fechas_originales'
                              else 'Fechas en destino:')
                for campo, valor in r[clave].items():
                    if campo == 'windows_100ns':
                        for cw, ticks in valor.items():
                            fecha = datetime.fromtimestamp(
                                (ticks - 116444736000000000) / 1e7, timezone.utc
                            ).isoformat()
                            lineas.append(
                                f'  {etiquetas_w[cw]} (Windows): {fecha} UTC')
                    else:
                        fecha = datetime.fromtimestamp(valor / 1e9, timezone.utc).isoformat()
                        lineas.append(
                            f'  {nombres_fecha.get(campo, campo)}: {fecha} UTC')
        lineas.append('')

    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text('\n'.join(lineas) + '\n', encoding='utf-8')


# ============================================================================
# Helpers de input
# ============================================================================
def _pedir_modo(defecto: str = '3') -> str:
    intentos = 0
    while True:
        try:
            valor = input(f'Modo [{defecto}]: ').strip() or defecto
        except (EOFError, KeyboardInterrupt):
            return '0'
        if valor in ('0', '1', '2', '3'):
            return valor
        intentos += 1
        if intentos >= 5:
            return '0'
        _imprimir('Elige 0, 1, 2 o 3.', 'yellow')


def _pedir_confirmacion() -> bool:
    intentos = 0
    while True:
        try:
            valor = input('¿Ejecutar este plan de movimiento? (s/n) [n]: ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if valor in ('s', 'si', 'sí', 'y', 'yes'):
            return True
        if valor in ('n', 'no', ''):
            return False
        intentos += 1
        if intentos >= 5:
            return False
        _imprimir('Responde s o n.', 'yellow')


# ============================================================================
# Eliminación comprobada de archivos locales (Windows, Linux, macOS, Android)
# ============================================================================
def _eliminar_candidatos_verificados(candidatos, carpeta_reportes, reporte, bases):
    """Borra SOLO vídeos locales con SHA256 probado y consentimiento por archivo.

    Un duplicado ubicado en otro dispositivo es evidencia del reporte,
    NO una confirmación de que el archivo remoto todavía exista.
    """
    from .database import info_dispositivo_actual
    import re

    disp = info_dispositivo_actual()
    dispositivo_id = disp['dispositivo_id']
    validos = []
    for item in candidatos:
        ruta = item.get('ruta', '')
        sha = (item.get('sha256') or '').lower()
        if (item.get('dispositivo_id') != dispositivo_id
                or not re.fullmatch(r'[a-f0-9]{64}', sha)):
            continue
        archivo = Path(ruta)
        if archivo.is_symlink() or not archivo.is_file():
            continue
        validos.append(dict(item, ruta=str(archivo), sha256=sha))
    unicos = list({(i['dispositivo_id'], i['ruta']): i for i in validos}.values())
    if not unicos:
        _imprimir('No hay archivos locales con SHA256 y duplicado confirmado en el reporte.', 'yellow')
        return {'eliminados': 0, 'omitidos': 0}
    _imprimir('\nARCHIVOS CANDIDATOS A ELIMINACIÓN PERMANENTE:', 'yellow')
    for idx, item in enumerate(unicos, 1):
        _imprimir(f'  {idx}. {item["ruta"]}', 'white')
        _imprimir(f'     SHA256: {item["sha256"][:16]}... | copia: {item.get("respaldo_registrado", "N/D")}', 'cyan')
    _imprimir('ADVERTENCIA: no es posible comprobar físicamente una copia en otro dispositivo desconectado.', 'yellow')
    _imprimir('No existe papelera ni recuperación automática. Solo se eliminarán archivos del dispositivo actual.', 'yellow')
    try:
        from core.presentacion import confirma_frase
        if not confirma_frase(input('Para autorizar el lote, escribe ELIMINAR (otra respuesta cancela): '), 'ELIMINAR'):
            return {'eliminados': 0, 'omitidos': len(unicos)}
    except (EOFError, KeyboardInterrupt):
        return {'eliminados': 0, 'omitidos': len(unicos)}

    sub = _subcarpetas_movimientos(carpeta_reportes)
    sello = _id_utc6()
    bitacora = sub['jsonl'] / f'eliminaciones_{sello}.jsonl'
    registros = []

    with bitacora.open('x', encoding='utf-8') as log:
        for indice, item in enumerate(unicos, 1):
            archivo = Path(item['ruta'])
            registro = dict(fecha=datetime.now(ZONA_MEXICO).isoformat(),
                            origen=str(archivo), dispositivo_id=dispositivo_id,
                            sha256_esperado=item['sha256'], estado='omitido')
            try:
                if archivo.is_symlink() or not archivo.is_file():
                    registro['motivo'] = 'El archivo desapareció o es un enlace'
                else:
                    antes = archivo.stat()
                    sha_actual = _sha256(archivo)
                    if _identidad(archivo.stat()) != _identidad(antes):
                        registro['motivo'] = 'El archivo cambió durante la validación'
                    elif sha_actual != item['sha256']:
                        registro['motivo'] = 'El SHA256 difiere del reporte'
                    elif (item.get('tamanio_bytes') is not None and
                          int(item['tamanio_bytes']) != antes.st_size):
                        registro['motivo'] = 'El tamaño difiere del reporte'
                    else:
                        archivo.unlink()
                        registro['estado'] = 'eliminado'
                        registro['sha256_comprobado'] = sha_actual
            except (OSError, ValueError, KeyboardInterrupt, EOFError) as exc:
                registro['motivo'] = str(exc) or 'Cancelado'
            log.write(json.dumps(registro, ensure_ascii=False) + '\n')
            log.flush()
            os.fsync(log.fileno())
            registros.append(registro)
            _imprimir(f'[{indice}/{len(unicos)}] {registro["estado"]}: {archivo.name}',
                      'green' if registro['estado'] == 'eliminado' else 'yellow')

    # La baja de la huella es posterior al borrado. Un error no se silencia.
    avisos = []
    if any(x['estado'] == 'eliminado' for x in registros):
        from . import database
        from .sincronizacion import obtener_ruta_db_local_dispositivo
        bases_a_actualizar = list(dict.fromkeys(bases or []))
        ruta_local_def = str(obtener_ruta_db_local_dispositivo())
        if ruta_local_def not in bases_a_actualizar and Path(ruta_local_def).is_file():
            bases_a_actualizar.append(ruta_local_def)

        for base in bases_a_actualizar:
            try:
                if str(base) == 'neon_cloud':
                    if database.estado_conexion()['modo'] != 'neon':
                        avisos.append('No se actualizó Neon: no hay conexión activa.')
                        continue
                    conn = database.conectar('neon_cloud')
                elif Path(str(base)).is_file() and Path(str(base)).suffix.lower() == '.db':
                    conn = database.conectar_sqlite(str(base))
                else:
                    continue
                try:
                    for r in registros:
                        if r['estado'] == 'eliminado':
                            if not database.eliminar_por_ruta(conn, r['origen'], dispositivo_id):
                                avisos.append(f'No se retiró huella de {r["origen"]} en {base}.')
                finally:
                    database.cerrar(conn)
            except Exception as exc:
                avisos.append(f'No se actualizó base {base}: {exc}')

    resumen = {'reporte_origen': str(reporte), 'dispositivo_ejecutor': disp,
               'registros': registros, 'bitacora': str(bitacora), 'avisos': avisos}
    salida = sub['json'] / f'eliminaciones_{sello}.json'
    salida.write_text(json.dumps(resumen, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (sub['txt'] / f'eliminaciones_{sello}.txt').write_text(
        f'Reporte origen: {reporte}\nDispositivo: {disp["dispositivo"]} (ID {dispositivo_id})\n'
        + '\n'.join(f'{r["estado"]}: {r["origen"]} | {r.get("motivo", "")}' for r in registros)
        + '\n' + '\n'.join(avisos) + '\n', encoding='utf-8')
    _imprimir(f'Registro de eliminación: {salida}', 'cyan')
    for aviso in avisos:
        _imprimir(aviso, 'yellow')
    return {'eliminados': sum(x['estado'] == 'eliminado' for x in registros),
            'omitidos': sum(x['estado'] != 'eliminado' for x in registros), 'avisos': avisos}


def eliminar_desde_comparativa(ruta_reporte, carpeta_reportes):
    from .database import info_dispositivo_actual
    with Path(ruta_reporte).open(encoding='utf-8-sig') as fh:
        datos = json.load(fh)
        
    if not isinstance(datos, dict) or not isinstance(datos.get('resultados'), list):
        raise ValueError('Se necesita un reporte JSON de comparación válido.')
        
    disp_actual = info_dispositivo_actual()
    disp_id_actual = disp_actual['dispositivo_id']
    
    exactos = [r for r in datos['resultados'] if r.get('tipo') == 'duplicado_exacto']
    if not exactos:
        _imprimir('No se encontraron duplicados exactos en el reporte.', 'yellow')
        return
        
    dispositivos = {}
    for h in exactos:
        info_a = h.get('info_a', {})
        info_b = h.get('info_b', {})
        for info, otra_info in [(info_a, info_b), (info_b, info_a)]:
            d_id = info.get('dispositivo_id')
            if d_id:
                if d_id not in dispositivos:
                    dispositivos[d_id] = {'nombre': info.get('dispositivo', 'Desconocido'), 'archivos': {}}
                ruta = info.get('ruta')
                if ruta:
                    sha = info.get('hash_archivo', '')
                    tam = info.get('tamanio_bytes')
                    respaldo = f'{otra_info.get("ruta", "")} (ID {otra_info.get("dispositivo_id", "N/D")})'
                    dispositivos[d_id]['archivos'][ruta] = {
                        'ruta': ruta, 'dispositivo_id': d_id, 'sha256': sha, 
                        'tamanio_bytes': tam, 'respaldo_registrado': respaldo
                    }
                    
    if not dispositivos:
        _imprimir('No se detectaron IDs de dispositivos en los duplicados.', 'yellow')
        return
        
    _imprimir('\nSe encontraron duplicados exactos en los siguientes dispositivos:', 'cyan')
    lista_disp = list(dispositivos.items())
    for i, (d_id, data) in enumerate(lista_disp, 1):
        _imprimir(f'  [gold1]{i}.[/] {data["nombre"]} (ID {d_id}) - {len(data["archivos"])} vídeos a eliminar')
        
    opcion = input('\nElige el número del dispositivo del que deseas ELIMINAR los vídeos (0 para cancelar): ').strip()
    if not opcion or opcion == '0':
        _imprimir('Cancelado.', 'yellow')
        return
        
    try:
        idx = int(opcion) - 1
        if idx < 0 or idx >= len(lista_disp):
            raise ValueError
    except ValueError:
        _imprimir('Opción inválida.', 'red')
        return
        
    d_id_elegido, data_elegida = lista_disp[idx]
    
    if d_id_elegido != disp_id_actual:
        _imprimir(f'\n[ERROR] Elegiste el dispositivo "{data_elegida["nombre"]}", pero tu equipo actual es "{disp_actual["dispositivo"]}".', 'red')
        _imprimir('Por seguridad, SOLO puedes eliminar vídeos físicamente si te encuentras en su dispositivo original.', 'red')
        return
        
    candidatos = list(data_elegida['archivos'].values())
    return _eliminar_candidatos_verificados(candidatos, carpeta_reportes, ruta_reporte,
        (datos.get('metadatos') or {}).get('bases_de_datos', []))


def eliminar_desde_sha256(ruta_json, carpeta_reportes):
    from .database import info_dispositivo_actual
    with Path(ruta_json).open(encoding='utf-8-sig') as fh:
        datos = json.load(fh)
    if datos.get('tipo_informe') not in ('sha256_carpeta', 'sha256_comparacion_offline', 'comparacion_sha256'):
        raise ValueError('Se necesita un reporte JSON SHA256 de comparación.')
    disp_id = info_dispositivo_actual()['dispositivo_id']
    es_comparacion_json = datos.get('tipo_informe') == 'sha256_comparacion_offline'
    origen = (datos.get('dispositivo_origen_json') if es_comparacion_json
              else datos.get('dispositivo_actual') or datos.get('dispositivo')) or {}
    origen_id = origen.get('dispositivo_id') if isinstance(origen, dict) else ''
    if not es_comparacion_json and origen_id != disp_id:
        raise ValueError('Solo se pueden eliminar archivos desde el dispositivo que generó el reporte.')
    candidatos = []
    for item in datos.get('coincidencias') or []:
        if es_comparacion_json and (item.get('dispositivo_id_origen') or origen_id) != disp_id:
            continue
        refs = item.get('coincidencias_en_otros_dispositivos') or []
        refs = [r for r in refs if r.get('dispositivo_id') and r['dispositivo_id'] != disp_id]
        if not refs:
            continue
        referencia = refs[0]
        candidatos.append(dict(ruta=item.get('ruta'), dispositivo_id=disp_id,
            sha256=item.get('sha256'), tamanio_bytes=item.get('tamanio_bytes'),
            respaldo_registrado=f'{referencia.get("ruta")} (ID {referencia.get("dispositivo_id")})'))
    return _eliminar_candidatos_verificados(candidatos, carpeta_reportes, ruta_json,
        datos.get('bases_de_datos') or [datos.get('ruta_db', '')])


# ============================================================================
# Modo: vaciar_carpeta_eliminar
# ============================================================================
def vaciar_carpeta_eliminar(directorio_eliminar, db_path):
    from .database import conectar, cerrar, eliminar_por_ruta, _usa_neon, info_dispositivo_actual
    from .almacenamiento import carpeta_reportes as _carpeta_reportes
    
    carpeta = Path(directorio_eliminar).expanduser().resolve()
    if not carpeta.is_dir():
        _imprimir(f'No se encontró la carpeta: {carpeta}', 'red')
        return
        
    videos = [p for p in carpeta.iterdir() if p.is_file() and p.suffix.lower() in {
        '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v',
        '.mpg', '.mpeg', '.ts', '.3gp', '.ogv', '.rm', '.rmvb', '.vob',
        '.divx', '.m2ts'}]
        
    if not videos:
        _imprimir('La carpeta está vacía o no contiene vídeos.', 'yellow')
        return
        
    _imprimir(f'\nADVERTENCIA: Se eliminarán {len(videos)} vídeos permanentemente del disco y de la base de datos.', 'red')
    _imprimir(f'Carpeta: {carpeta}', 'cyan')
    
    intentos = 0
    confirmado = False
    while intentos < 5:
        try:
            resp = input('¿Estás completamente seguro de continuar? (s/n) [n]: ').strip().lower()
            if resp in ('s', 'si', 'sí', 'y', 'yes'):
                confirmado = True
                break
            elif resp in ('n', 'no', ''):
                break
        except (EOFError, KeyboardInterrupt):
            break
        intentos += 1
        
    if not confirmado:
        _imprimir('Operación cancelada.', 'yellow')
        return
        
    try:
        from core.presentacion import confirma_frase
        if not confirma_frase(input('Escribe BORRAR TODO para confirmar: '), 'BORRAR TODO'):
            _imprimir('Confirmación incorrecta. Cancelado.', 'yellow')
            return
    except (EOFError, KeyboardInterrupt):
        return

    disp_actual = info_dispositivo_actual()
    disp_id = disp_actual['dispositivo_id']
    conn = conectar(db_path)
    
    sub = _subcarpetas_movimientos(_carpeta_reportes() / 'movimientos')
    sello = _id_utc6()
    bitacora = sub['jsonl'] / f'vaciado_eliminar_{sello}.jsonl'

    eliminados_disco = 0
    eliminados_db = 0
    errores = []
    registros_log = []
    
    try:
        with bitacora.open('x', encoding='utf-8') as log:
            for video in videos:
                ruta_str = str(video)
                evento = {
                    'fecha': datetime.now(ZONA_MEXICO).isoformat(),
                    'origen': ruta_str,
                    'nombre': video.name,
                    'dispositivo_id': disp_id,
                    'estado': 'omitido',
                    'eliminado_db': False,
                }
                try:
                    video.unlink()
                    eliminados_disco += 1
                    evento['estado'] = 'eliminado'
                    if eliminar_por_ruta(conn, ruta_str, disp_id):
                        eliminados_db += 1
                        evento['eliminado_db'] = True
                except Exception as e:
                    errores.append(f'{video.name}: {e}')
                    evento['error'] = str(e)

                log.write(json.dumps(evento, ensure_ascii=False) + '\n')
                log.flush()
                try:
                    os.fsync(log.fileno())
                except OSError:
                    pass
                registros_log.append(evento)
    finally:
        cerrar(conn)

    resumen = {
        'fecha': datetime.now(ZONA_MEXICO).isoformat(),
        'carpeta': str(carpeta),
        'dispositivo_ejecutor': disp_actual,
        'total_videos': len(videos),
        'eliminados_disco': eliminados_disco,
        'eliminados_db': eliminados_db,
        'errores': errores,
        'bitacora': str(bitacora),
        'registros': registros_log,
    }
    salida_json = sub['json'] / f'vaciado_eliminar_{sello}.json'
    salida_txt = sub['txt'] / f'vaciado_eliminar_{sello}.txt'
    salida_json.write_text(json.dumps(resumen, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    salida_txt.write_text(
        f'VACIADO DE CARPETA ELIMINAR — {resumen["fecha"]}\n'
        f'Carpeta: {carpeta}\n'
        f'Dispositivo: {disp_actual["dispositivo"]} (ID {disp_id})\n'
        f'Eliminados del disco: {eliminados_disco}/{len(videos)}\n'
        f'Eliminados de BD: {eliminados_db}/{len(videos)}\n\n'
        + '\n'.join(f'{r["estado"].upper()} | BD={r["eliminado_db"]} | {r["origen"]} {r.get("error", "")}' for r in registros_log)
        + '\n',
        encoding='utf-8',
    )
        
    _imprimir(f'\nVídeos eliminados del disco: {eliminados_disco}/{len(videos)}', 'green')
    _imprimir(f'Registros eliminados de la BD: {eliminados_db}/{len(videos)}', 'green')
    _imprimir(f'Reporte guardado en: {salida_json}', 'cyan')
    
    if errores:
        _imprimir('\nErrores durante el borrado:', 'red')
        for err in errores:
            _imprimir(f'  - {err}', 'yellow')