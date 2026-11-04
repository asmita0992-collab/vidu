"""
Almacenamiento y recuperación de huellas en PostgreSQL (Neon Cloud) o SQLite local.

Detección automática del modo de conexión:
  - Si NEON_DATABASE_URL está definida Y hay conectividad Y psycopg2 está
    instalado, opera contra Neon.
  - Si algo de lo anterior falla, cae a SQLite local y NO reintenta en toda
    la sesión (evita timeouts repetidos sin Internet).
  - Si no hay NEON_DATABASE_URL, usa SQLite directamente sin probar Neon.

Cada fila se identifica por una clave_unica = dispositivo_id + '|' + ruta, de modo
que dos dispositivos con la misma ruta de archivo no colisionan ni se sobreescriben.

Identificación del dispositivo:
  - Android/Termux: getprop ro.product.model + ro.product.manufacturer +
    ro.hardware + ro.build.fingerprint (o serialno si está disponible).
  - Windows: UUID del producto (wmic o powershell) + hostname + arquitectura.
  - Linux: /etc/machine-id + hostname + arquitectura.
  - macOS: número de serie del hardware + hostname + arquitectura.
  - Fallback genérico: hostname + arquitectura + MAC.

El ID corto de 13 caracteres hexadecimales se persiste en:
  - Android/Termux: $HOME/.vidup_device_identity.json (y copia en DIR_SCRIPT).
  - Otros sistemas: DIR_SCRIPT/.vidup_device_identity.json.

La impresión de avisos se hace vía un helper que usa rich si está disponible
y ANSI puro como fallback.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ZONA_MEXICO = timezone(timedelta(hours=-6))

def _ahora_utc6_str() -> str:
    return datetime.now(ZONA_MEXICO).strftime('%Y-%m-%d %H:%M:%S')

try:
    import psycopg2
    import psycopg2.extras
    TIENE_PSYCOPG2 = True
except ImportError:
    TIENE_PSYCOPG2 = False

from .fingerprint import VideoFingerprint

# ============================================================================
# Colores ANSI (fallback si rich no está)
# ============================================================================
_NC = '\033[0m'
_ROJO = '\033[91m'
_AMARILLO = '\033[93m'
_VERDE = '\033[92m'
_GRIS = '\033[90m'
_CIAN = '\033[96m'

try:
    from rich.console import Console as _RichConsole
    _RICH_CONSOLA = _RichConsole(highlight=False, soft_wrap=True)
    _TIENE_RICH = True
except ImportError:
    _RICH_CONSOLA = None
    _TIENE_RICH = False


def _imprimir(mensaje: str, estilo: str = '') -> None:
    """Imprime usando rich si está disponible, si no ANSI."""
    if _RICH_CONSOLA is not None:
        _RICH_CONSOLA.print(mensaje, style=estilo or None)
    else:
        prefijo = ''
        sufijo = ''
        if estilo == 'red':
            prefijo, sufijo = _ROJO, _NC
        elif estilo == 'yellow':
            prefijo, sufijo = _AMARILLO, _NC
        elif estilo == 'green':
            prefijo, sufijo = _VERDE, _NC
        elif estilo == 'cyan':
            prefijo, sufijo = _CIAN, _NC
        elif estilo in ('bright_black', 'grey37'):
            prefijo, sufijo = _GRIS, _NC
        print(f'{prefijo}{mensaje}{sufijo}')


# ============================================================================
# SQL de creación de tablas
# ============================================================================
_SQL_CREAR_TABLAS_POSTGRES = """
CREATE TABLE IF NOT EXISTS videos (
    id SERIAL PRIMARY KEY,
    clave_unica TEXT NOT NULL UNIQUE,
    ruta TEXT NOT NULL,
    nombre TEXT NOT NULL,
    duracion DOUBLE PRECISION NOT NULL DEFAULT 0,
    ancho INTEGER NOT NULL DEFAULT 0,
    alto INTEGER NOT NULL DEFAULT 0,
    fps DOUBLE PRECISION NOT NULL DEFAULT 0,
    codec_video TEXT NOT NULL DEFAULT '',
    codec_audio TEXT NOT NULL DEFAULT '',
    hash_archivo TEXT NOT NULL DEFAULT '',
    hash_rapido TEXT NOT NULL DEFAULT '',
    frame_intervalo DOUBLE PRECISION NOT NULL DEFAULT 5.0,
    audio_huella TEXT,
    tamanio_bytes BIGINT NOT NULL DEFAULT 0,
    bitrate_video_kbps DOUBLE PRECISION NOT NULL DEFAULT 0,
    fecha_escaneo TEXT NOT NULL,
    dispositivo TEXT NOT NULL DEFAULT '',
    dispositivo_id TEXT NOT NULL DEFAULT '',
    dispositivo_origen TEXT NOT NULL DEFAULT '',
    dispositivo_origen_id TEXT NOT NULL DEFAULT '',
    error TEXT
);

CREATE TABLE IF NOT EXISTS frame_hashes (
    id SERIAL PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    posicion INTEGER NOT NULL,
    hash_valor TEXT NOT NULL,
    UNIQUE(video_id, posicion)
);

CREATE INDEX IF NOT EXISTS idx_videos_hash_archivo ON videos(hash_archivo);
CREATE INDEX IF NOT EXISTS idx_videos_hash_rapido ON videos(hash_rapido);
CREATE INDEX IF NOT EXISTS idx_videos_duracion ON videos(duracion);
CREATE INDEX IF NOT EXISTS idx_videos_nombre ON videos(nombre);
CREATE INDEX IF NOT EXISTS idx_videos_dispositivo_id ON videos(dispositivo_id);
CREATE INDEX IF NOT EXISTS idx_videos_clave_unica ON videos(clave_unica);
CREATE INDEX IF NOT EXISTS idx_frame_hashes_video_posicion ON frame_hashes(video_id, posicion);
"""

_SQL_CREAR_TABLAS_SQLITE = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clave_unica TEXT NOT NULL UNIQUE,
    ruta TEXT NOT NULL,
    nombre TEXT NOT NULL,
    duracion REAL NOT NULL DEFAULT 0,
    ancho INTEGER NOT NULL DEFAULT 0,
    alto INTEGER NOT NULL DEFAULT 0,
    fps REAL NOT NULL DEFAULT 0,
    codec_video TEXT NOT NULL DEFAULT '',
    codec_audio TEXT NOT NULL DEFAULT '',
    hash_archivo TEXT NOT NULL DEFAULT '',
    hash_rapido TEXT NOT NULL DEFAULT '',
    frame_intervalo REAL NOT NULL DEFAULT 5.0,
    audio_huella TEXT,
    tamanio_bytes INTEGER NOT NULL DEFAULT 0,
    bitrate_video_kbps REAL NOT NULL DEFAULT 0,
    fecha_escaneo TEXT NOT NULL,
    dispositivo TEXT NOT NULL DEFAULT '',
    dispositivo_id TEXT NOT NULL DEFAULT '',
    dispositivo_origen TEXT NOT NULL DEFAULT '',
    dispositivo_origen_id TEXT NOT NULL DEFAULT '',
    error TEXT
);

CREATE TABLE IF NOT EXISTS frame_hashes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL,
    posicion INTEGER NOT NULL,
    hash_valor TEXT NOT NULL,
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE,
    UNIQUE(video_id, posicion)
);

CREATE INDEX IF NOT EXISTS idx_videos_hash_archivo ON videos(hash_archivo);
CREATE INDEX IF NOT EXISTS idx_videos_hash_rapido ON videos(hash_rapido);
CREATE INDEX IF NOT EXISTS idx_videos_duracion ON videos(duracion);
CREATE INDEX IF NOT EXISTS idx_videos_nombre ON videos(nombre);
CREATE INDEX IF NOT EXISTS idx_videos_dispositivo_id ON videos(dispositivo_id);
CREATE INDEX IF NOT EXISTS idx_videos_clave_unica ON videos(clave_unica);
CREATE INDEX IF NOT EXISTS idx_frame_hashes_video_posicion ON frame_hashes(video_id, posicion);
"""


# ============================================================================
# Estado de conexión (cacheado en la sesión)
# ============================================================================
_ESTADO_CONEXION: Dict[str, Any] = {
    'en_linea': None,
    'modo': 'desconocido',
    'detalle': '',
}
_NEON_DESCARTADO: bool = False


def estado_conexion() -> dict:
    return dict(_ESTADO_CONEXION)


def describir_conexion() -> str:
    st = _ESTADO_CONEXION
    if st['modo'] == 'neon':
        return 'EN LÍNEA (Neon PostgreSQL en la nube)'
    if st['modo'] == 'sqlite':
        return 'FUERA DE LÍNEA (SQLite local)'
    if st['modo'] == 'sqlite_fallback':
        detalle = f' — {st["detalle"]}' if st.get('detalle') else ''
        return f'FUERA DE LÍNEA (SQLite local, respaldo por fallo de Neon{detalle})'
    return 'SIN CONEXIÓN INICIADA'


def _usa_neon() -> bool:
    if _NEON_DESCARTADO:
        return False
    return bool(TIENE_PSYCOPG2 and os.environ.get('NEON_DATABASE_URL'))


def _es_android() -> bool:
    return (
        'ANDROID_ROOT' in os.environ
        or 'ANDROID_DATA' in os.environ
        or 'com.termux' in os.environ.get('PREFIX', '')
        or 'com.termux' in os.environ.get('PATH', '')
    )


# ============================================================================
# Detección de conectividad
# ============================================================================
def _hay_conectividad_red(timeout: float = 3.0) -> bool:
    for host, puerto in (
        ('api.telegram.org', 443),
        ('1.1.1.1', 443),
        ('8.8.8.8', 53),
    ):
        try:
            with socket.create_connection((host, puerto), timeout=timeout):
                return True
        except (OSError, socket.error):
            continue
    return False


def _probar_conexion_neon(url: str, timeout: int = 5) -> Tuple[bool, str]:
    if not TIENE_PSYCOPG2:
        return False, 'psycopg2 no instalado'
    try:
        conn = psycopg2.connect(url, connect_timeout=timeout)
        conn.close()
        return True, ''
    except Exception as exc:
        return False, str(exc)[:200]


def _detectar_modo_conexion_una_vez() -> None:
    global _NEON_DESCARTADO

    if _ESTADO_CONEXION['modo'] in ('neon', 'sqlite', 'sqlite_fallback'):
        return

    if os.environ.get('VIDUP_FORZAR_OFFLINE', '').strip() in ('1', 'true', 'yes', 'sí'):
        _NEON_DESCARTADO = True
        _ESTADO_CONEXION.update(
            en_linea=False, modo='sqlite',
            detalle='VIDUP_FORZAR_OFFLINE activo',
        )
        return

    url = os.environ.get('NEON_DATABASE_URL', '').strip()
    if not url:
        _ESTADO_CONEXION.update(en_linea=False, modo='sqlite', detalle='')
        return

    if not TIENE_PSYCOPG2:
        _NEON_DESCARTADO = True
        _ESTADO_CONEXION.update(
            en_linea=False, modo='sqlite',
            detalle='psycopg2 no instalado',
        )
        return

    if not _hay_conectividad_red():
        _NEON_DESCARTADO = True
        _ESTADO_CONEXION.update(
            en_linea=False, modo='sqlite_fallback',
            detalle='sin conectividad de red',
        )
        return

    ok, detalle = _probar_conexion_neon(url, timeout=5)
    if ok:
        _ESTADO_CONEXION.update(en_linea=True, modo='neon', detalle='')
    else:
        _NEON_DESCARTADO = True
        _ESTADO_CONEXION.update(
            en_linea=False, modo='sqlite_fallback',
            detalle=detalle or 'fallo de conexión',
        )


def _calcular_clave_unica(dispositivo_id: str, ruta: str) -> str:
    disp = (dispositivo_id or '').strip()
    ruta = (ruta or '').strip()
    if not disp:
        return ruta
    return f'{disp}|{ruta}'


# ============================================================================
# Identificación del dispositivo (IUD_C + ID corto)
# ============================================================================
_ARCHIVO_IDENTIDAD_NOMBRE = '.vidup_device_identity.json'


def _ruta_identidad_primaria() -> Path:
    """Devuelve la ruta primaria donde se guarda la identidad del dispositivo.

    En Android/Termux se intenta usar $HOME para que sobreviva a una
    reinstalación del proyecto. En otros sistemas se usa la carpeta del script.
    """
    if _es_android():
        home = os.environ.get('HOME', '').strip()
        if home:
            try:
                home_path = Path(home).expanduser()
                if home_path.is_dir():
                    return home_path / _ARCHIVO_IDENTIDAD_NOMBRE
            except Exception:
                pass
    return _directorio_script() / _ARCHIVO_IDENTIDAD_NOMBRE


def _ruta_identidad_respaldo() -> Path:
    """Ruta de respaldo, siempre junto al script."""
    return _directorio_script() / _ARCHIVO_IDENTIDAD_NOMBRE


def _directorio_script() -> Path:
    return Path(__file__).resolve().parent.parent


def _obtener_propiedad_android(propiedad: str) -> str:
    """Lee una propiedad de Android vía getprop. Devuelve '' si falla."""
    try:
        resultado = subprocess.check_output(
            ['getprop', propiedad],
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return resultado.decode('utf-8', errors='replace').strip()
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return ''


def _obtener_id_windows() -> str:
    """UUID del producto en Windows: intenta wmic y luego PowerShell."""
    try:
        resultado = subprocess.check_output(
            ['wmic', 'csproduct', 'get', 'uuid'],
            stderr=subprocess.DEVNULL, timeout=5,
        )
        lineas = resultado.decode('utf-8', errors='ignore').splitlines()
        for linea in lineas[1:]:
            v = linea.strip()
            if v and v.upper() != 'UUID':
                return v
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass

    try:
        resultado = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command',
             '(Get-CimInstance Win32_ComputerSystemProduct).UUID'],
            stderr=subprocess.DEVNULL, timeout=5,
        )
        valor = resultado.decode('utf-8', errors='ignore').strip()
        if valor:
            return valor
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass
    return ''


def _obtener_id_linux() -> str:
    for ruta in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
        try:
            with open(ruta, 'r', encoding='utf-8') as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            continue
    return ''


def _obtener_id_macos() -> str:
    try:
        resultado = subprocess.check_output(
            ['system_profiler', 'SPHardwareDataType'],
            stderr=subprocess.DEVNULL, timeout=15,
        )
        for linea in resultado.decode('utf-8', errors='ignore').splitlines():
            if 'Serial Number' in linea:
                return linea.split(':', 1)[-1].strip()
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass
    return ''


def _componentes_identidad() -> List[str]:
    """Devuelve la lista de componentes según la plataforma."""
    so = platform.system()
    componentes: List[str] = []

    if _es_android() or so == 'Android':
        componentes = [
            _obtener_propiedad_android('ro.serialno'),
            _obtener_propiedad_android('ro.product.model'),
            _obtener_propiedad_android('ro.product.manufacturer'),
            _obtener_propiedad_android('ro.hardware'),
            _obtener_propiedad_android('ro.boot.hardware.sku'),
            _obtener_propiedad_android('ro.build.fingerprint'),
        ]
        if not any(componentes):
            componentes = [
                platform.node(),
                platform.machine(),
                str(uuid.getnode()),
            ]
    elif so == 'Windows':
        componentes = [
            _obtener_id_windows(),
            platform.node(),
            platform.machine(),
        ]
    elif so == 'Linux':
        componentes = [
            _obtener_id_linux(),
            platform.node(),
            platform.machine(),
        ]
    elif so == 'Darwin':
        componentes = [
            _obtener_id_macos(),
            platform.node(),
            platform.machine(),
        ]
    else:
        componentes = [
            platform.node(),
            platform.machine(),
            str(uuid.getnode()),
        ]

    return [c for c in componentes if c]


def _leer_identidad_guardada(ruta: Path) -> Optional[Dict[str, str]]:
    if not ruta.is_file():
        return None
    try:
        with ruta.open('r', encoding='utf-8') as f:
            datos = json.load(f)
        if isinstance(datos, dict) and 'IUD_C' in datos and 'ID' in datos:
            iud_c = str(datos['IUD_C']).strip()
            idc = str(datos['ID']).strip()
            if iud_c and idc:
                return {'IUD_C': iud_c, 'ID': idc}
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _escribir_identidad_guardada(ruta: Path, iud_c: str, idc: str) -> bool:
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        with ruta.open('w', encoding='utf-8') as f:
            json.dump({'IUD_C': iud_c, 'ID': idc}, f, indent=4)
        return True
    except OSError:
        return False


def _generar_identidad_desde_componentes() -> Tuple[str, str]:
    """Genera IUD_C (SHA-256) e ID (13 hex) desde los componentes del dispositivo."""
    componentes = _componentes_identidad()
    semilla = ''.join(componentes)

    if not semilla:
        iud_c = hashlib.sha256(b'vidup_contingencia').hexdigest()
    else:
        iud_c = hashlib.sha256(semilla.encode('utf-8')).hexdigest()

    idc = iud_c[:13]
    return iud_c, idc


def _identidad_dispositivo() -> Dict[str, str]:
    """Devuelve el IUD_C y el ID corto, leyendo o generando el archivo persistente."""
    ruta_principal = _ruta_identidad_primaria()
    ruta_respaldo = _ruta_identidad_respaldo()

    datos = _leer_identidad_guardada(ruta_principal)
    if datos is None and ruta_respaldo != ruta_principal:
        datos = _leer_identidad_guardada(ruta_respaldo)

    if datos is None:
        iud_c, idc = _generar_identidad_desde_componentes()
        datos = {'IUD_C': iud_c, 'ID': idc}

    if not ruta_principal.is_file():
        _escribir_identidad_guardada(ruta_principal, datos['IUD_C'], datos['ID'])
    if ruta_respaldo != ruta_principal and not ruta_respaldo.is_file():
        _escribir_identidad_guardada(ruta_respaldo, datos['IUD_C'], datos['ID'])

    return datos


_IDENTIDAD_CACHE: Optional[Dict[str, str]] = None


def _obtener_identidad_cacheada() -> Dict[str, str]:
    global _IDENTIDAD_CACHE
    if _IDENTIDAD_CACHE is None:
        _IDENTIDAD_CACHE = _identidad_dispositivo()
    return _IDENTIDAD_CACHE


def _identificador_dispositivo() -> str:
    """Etiqueta legible del equipo."""
    if _es_android():
        partes = [
            _obtener_propiedad_android('ro.product.manufacturer'),
            _obtener_propiedad_android('ro.product.model'),
        ]
        etiqueta = ' '.join(p for p in partes if p).strip()
        if etiqueta:
            return f'{etiqueta}|{platform.system()}|{platform.machine()}'

    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = 'desconocido'

    return f'{hostname}|{platform.system()}|{platform.machine()}'


def _identificador_dispositivo_id() -> str:
    """ID corto y estable del dispositivo (13 hex)."""
    return _obtener_identidad_cacheada()['ID']


def info_dispositivo_actual() -> Dict[str, Any]:
    """Identificación del dispositivo actual, para reportes."""
    datos = _obtener_identidad_cacheada()
    return {
        'dispositivo': _identificador_dispositivo(),
        'dispositivo_id': datos['ID'],
        'iud_c': datos['IUD_C'],
        'plataforma': platform.system(),
        'maquina': platform.machine(),
        'hostname': socket.gethostname(),
        'es_android': _es_android(),
        'ruta_identidad': str(_ruta_identidad_primaria()),
    }


# ============================================================================
# Migración de esquema
# ============================================================================
def _migrar_esquema(conn: Any, es_pg: bool) -> None:
    if es_pg:
        try:
            conn.autocommit = True
        except Exception:
            pass

        def ejecutar(sql: str, etiqueta: str) -> bool:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                return True
            except Exception as exc:
                _imprimir(f'[AVISO NEON] {etiqueta}: {exc}', 'yellow')
                return False

        ejecutar(
            "ALTER TABLE videos ADD COLUMN IF NOT EXISTS dispositivo_id TEXT NOT NULL DEFAULT ''",
            'No se pudo añadir dispositivo_id')
        ejecutar(
            "ALTER TABLE videos ADD COLUMN IF NOT EXISTS dispositivo_origen TEXT NOT NULL DEFAULT ''",
            'No se pudo añadir dispositivo_origen')
        ejecutar(
            "ALTER TABLE videos ADD COLUMN IF NOT EXISTS dispositivo_origen_id TEXT NOT NULL DEFAULT ''",
            'No se pudo añadir dispositivo_origen_id')
        ejecutar(
            "ALTER TABLE videos ADD COLUMN IF NOT EXISTS clave_unica TEXT NOT NULL DEFAULT ''",
            'No se pudo añadir clave_unica')
        ejecutar(
            """
            UPDATE videos
               SET clave_unica = CASE
                   WHEN dispositivo_id <> '' THEN dispositivo_id || '|' || ruta
                   ELSE ruta
               END
             WHERE clave_unica = ''
            """,
            'No se pudo rellenar clave_unica')
        ejecutar(
            "CREATE INDEX IF NOT EXISTS idx_videos_dispositivo_id ON videos(dispositivo_id)",
            'No se pudo crear idx_videos_dispositivo_id')
        ejecutar(
            "CREATE INDEX IF NOT EXISTS idx_videos_clave_unica ON videos(clave_unica)",
            'No se pudo crear idx_videos_clave_unica')
        ejecutar(
            """
            DO $$
            DECLARE
                nombre_constraint TEXT;
            BEGIN
                SELECT conname INTO nombre_constraint
                FROM pg_constraint
                WHERE conrelid = 'videos'::regclass
                  AND contype = 'u'
                  AND pg_get_constraintdef(oid) LIKE '%(ruta)%';
                IF nombre_constraint IS NOT NULL THEN
                    EXECUTE 'ALTER TABLE videos DROP CONSTRAINT ' || quote_ident(nombre_constraint);
                END IF;
            END $$;
            """,
            'No se pudo quitar UNIQUE(ruta) heredado')

        try:
            conn.autocommit = False
        except Exception:
            pass
    else:
        columnas = {f[1] for f in conn.execute('PRAGMA table_info(videos)').fetchall()}

        for columna, definicion in (
            ('dispositivo_id', "TEXT NOT NULL DEFAULT ''"),
            ('dispositivo_origen', "TEXT NOT NULL DEFAULT ''"),
            ('dispositivo_origen_id', "TEXT NOT NULL DEFAULT ''"),
            ('clave_unica', "TEXT NOT NULL DEFAULT ''"),
        ):
            if columna not in columnas:
                try:
                    conn.execute(f"ALTER TABLE videos ADD COLUMN {columna} {definicion}")
                except sqlite3.OperationalError as exc:
                    _imprimir(f'[AVISO SQLITE] No se pudo añadir {columna}: {exc}', 'yellow')

        columnas = {f[1] for f in conn.execute('PRAGMA table_info(videos)').fetchall()}
        if 'clave_unica' in columnas:
            try:
                conn.execute("""
                    UPDATE videos
                       SET clave_unica = CASE
                           WHEN dispositivo_id <> '' THEN dispositivo_id || '|' || ruta
                           ELSE ruta
                       END
                     WHERE clave_unica = '' OR clave_unica IS NULL
                """)
            except sqlite3.OperationalError as exc:
                _imprimir(f'[AVISO SQLITE] No se pudo rellenar clave_unica: {exc}', 'yellow')


def _es_pg(conn: Any) -> bool:
    if not TIENE_PSYCOPG2:
        return False
    try:
        return isinstance(conn, psycopg2.extensions.connection)
    except Exception:
        return False


def _abrir_sqlite(ruta_abs: str) -> sqlite3.Connection:
    conn = sqlite3.connect(ruta_abs, timeout=30.0, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 30000')
    if ruta_abs != ':memory:':
        try:
            conn.execute('PRAGMA journal_mode = WAL')
        except sqlite3.Error:
            pass
        conn.execute('PRAGMA synchronous = NORMAL')
    conn.executescript(_SQL_CREAR_TABLAS_SQLITE)
    _migrar_esquema(conn, es_pg=False)
    conn.commit()
    return conn


def conectar(ruta_db: str) -> Any:
    _detectar_modo_conexion_una_vez()

    if _ESTADO_CONEXION['modo'] == 'neon':
        try:
            url = os.environ['NEON_DATABASE_URL'].strip()
            conn = psycopg2.connect(url, connect_timeout=5)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(_SQL_CREAR_TABLAS_POSTGRES)
            _migrar_esquema(conn, es_pg=True)
            conn.autocommit = False
            return conn
        except Exception as exc:
            global _NEON_DESCARTADO
            _NEON_DESCARTADO = True
            _ESTADO_CONEXION.update(
                en_linea=False, modo='sqlite_fallback',
                detalle=str(exc)[:150],
            )
            _imprimir(f'[AVISO NEON] Fallo de conexión: {exc}. Se usará SQLite local.', 'yellow')

    if ruta_db != ':memory:':
        path_obj = Path(ruta_db).expanduser().resolve()
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        ruta_abs = str(path_obj)
    else:
        ruta_abs = ':memory:'

    return _abrir_sqlite(ruta_abs)


def conectar_sqlite(ruta_db: str) -> sqlite3.Connection:
    if ruta_db != ':memory:':
        path_obj = Path(ruta_db).expanduser().resolve()
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        ruta_abs = str(path_obj)
    else:
        ruta_abs = ':memory:'
    return _abrir_sqlite(ruta_abs)


def cerrar(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _identidad_fisica(ruta: str) -> Optional[Tuple[int, int]]:
    try:
        estado = Path(ruta).stat()
    except (OSError, ValueError):
        return None
    if not estado.st_ino:
        return None
    return estado.st_dev, estado.st_ino


def obtener_indices_existentes(conn: Any) -> Tuple[Set[Tuple[str, str]], Set[Tuple[str, str]]]:
    """Pares (ID de dispositivo, SHA256) y (ID de dispositivo, ruta).

    Consulta registros de cualquier método de escaneo; no modifica la base.
    IDs vacíos no se consideran identificadores de un dispositivo conocido.
    """
    if _es_pg(conn):
        with conn.cursor() as cur:
            cur.execute('SELECT dispositivo_id, hash_archivo, ruta FROM videos')
            filas = cur.fetchall()
    else:
        filas = conn.execute('SELECT dispositivo_id, hash_archivo, ruta FROM videos').fetchall()

    hashes: Set[Tuple[str, str]] = set()
    rutas: Set[Tuple[str, str]] = set()
    for disp, sha, ruta in filas:
        did = str(disp or '').strip()
        if not did:
            continue
        sha = str(sha or '').strip().lower()
        if len(sha) == 64 and all(c in '0123456789abcdef' for c in sha):
            hashes.add((did, sha))
        if ruta:
            rutas.add((did, str(ruta)))
    return hashes, rutas


# ============================================================================
# Guardar fingerprint
# ============================================================================
def guardar_fingerprint(
    conn: Any,
    fp: VideoFingerprint,
    dispositivo: Optional[str] = None,
    dispositivo_id: Optional[str] = None,
    dispositivo_origen: Optional[str] = None,
    dispositivo_origen_id: Optional[str] = None,
) -> Optional[int]:
    """Inserta o actualiza una huella y sus hashes de frames."""
    ancho, alto = fp.resolucion
    disp = dispositivo if dispositivo is not None else (fp.dispositivo or _identificador_dispositivo())
    disp_id = dispositivo_id if dispositivo_id is not None else (fp.dispositivo_id or _identificador_dispositivo_id())
    disp_origen = dispositivo_origen or ''
    disp_origen_id = dispositivo_origen_id or ''
    clave = _calcular_clave_unica(disp_id, fp.ruta)
    fecha_utc6 = _ahora_utc6_str()

    is_pg = _es_pg(conn)

    try:
        if is_pg:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO videos (
                        clave_unica, ruta, nombre, duracion, ancho, alto, fps,
                        codec_video, codec_audio, hash_archivo, hash_rapido,
                        frame_intervalo, audio_huella, tamanio_bytes,
                        bitrate_video_kbps, fecha_escaneo, dispositivo, dispositivo_id,
                        dispositivo_origen, dispositivo_origen_id, error
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(clave_unica) DO UPDATE SET
                        ruta = EXCLUDED.ruta,
                        nombre = EXCLUDED.nombre,
                        duracion = EXCLUDED.duracion,
                        ancho = EXCLUDED.ancho,
                        alto = EXCLUDED.alto,
                        fps = EXCLUDED.fps,
                        codec_video = EXCLUDED.codec_video,
                        codec_audio = EXCLUDED.codec_audio,
                        hash_archivo = EXCLUDED.hash_archivo,
                        hash_rapido = EXCLUDED.hash_rapido,
                        frame_intervalo = EXCLUDED.frame_intervalo,
                        audio_huella = EXCLUDED.audio_huella,
                        tamanio_bytes = EXCLUDED.tamanio_bytes,
                        bitrate_video_kbps = EXCLUDED.bitrate_video_kbps,
                        fecha_escaneo = EXCLUDED.fecha_escaneo,
                        dispositivo = EXCLUDED.dispositivo,
                        dispositivo_id = EXCLUDED.dispositivo_id,
                        dispositivo_origen = EXCLUDED.dispositivo_origen,
                        dispositivo_origen_id = EXCLUDED.dispositivo_origen_id,
                        error = EXCLUDED.error
                    RETURNING id;
                    """,
                    (
                        clave, fp.ruta, fp.nombre, fp.duracion, ancho, alto, fp.fps,
                        fp.codec_video, fp.codec_audio, fp.hash_archivo, fp.hash_rapido,
                        fp.frame_intervalo, fp.audio_huella, fp.tamanio_bytes,
                        fp.bitrate_video_kbps, fecha_utc6, disp, disp_id,
                        disp_origen, disp_origen_id, fp.error,
                    )
                )
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return None
                video_id = row['id']

                cur.execute("DELETE FROM frame_hashes WHERE video_id = %s", (video_id,))
                if fp.frame_hashes:
                    psycopg2.extras.execute_values(
                        cur,
                        "INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES %s",
                        [(video_id, pos, val) for pos, val in enumerate(fp.frame_hashes)]
                    )
            conn.commit()
            return video_id
        else:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                """
                INSERT INTO videos (
                    clave_unica, ruta, nombre, duracion, ancho, alto, fps,
                    codec_video, codec_audio, hash_archivo, hash_rapido,
                    frame_intervalo, audio_huella, tamanio_bytes,
                    bitrate_video_kbps, fecha_escaneo, dispositivo, dispositivo_id,
                    dispositivo_origen, dispositivo_origen_id, error
                )
                VALUES (
                    :clave_unica, :ruta, :nombre, :duracion, :ancho, :alto, :fps,
                    :codec_video, :codec_audio, :hash_archivo, :hash_rapido,
                    :frame_intervalo, :audio_huella, :tamanio_bytes,
                    :bitrate_video_kbps, :fecha_escaneo, :dispositivo, :dispositivo_id,
                    :dispositivo_origen, :dispositivo_origen_id, :error
                )
                ON CONFLICT(clave_unica) DO UPDATE SET
                    ruta = excluded.ruta,
                    nombre = excluded.nombre, duracion = excluded.duracion,
                    ancho = excluded.ancho, alto = excluded.alto, fps = excluded.fps,
                    codec_video = excluded.codec_video, codec_audio = excluded.codec_audio,
                    hash_archivo = excluded.hash_archivo, hash_rapido = excluded.hash_rapido,
                    frame_intervalo = excluded.frame_intervalo, audio_huella = excluded.audio_huella,
                    tamanio_bytes = excluded.tamanio_bytes, bitrate_video_kbps = excluded.bitrate_video_kbps,
                    fecha_escaneo = excluded.fecha_escaneo, dispositivo = excluded.dispositivo,
                    dispositivo_id = excluded.dispositivo_id,
                    dispositivo_origen = excluded.dispositivo_origen,
                    dispositivo_origen_id = excluded.dispositivo_origen_id,
                    error = excluded.error
                """,
                {
                    'clave_unica': clave, 'ruta': fp.ruta, 'nombre': fp.nombre, 'duracion': fp.duracion,
                    'ancho': ancho, 'alto': alto, 'fps': fp.fps,
                    'codec_video': fp.codec_video, 'codec_audio': fp.codec_audio,
                    'hash_archivo': fp.hash_archivo, 'hash_rapido': fp.hash_rapido,
                    'frame_intervalo': fp.frame_intervalo, 'audio_huella': fp.audio_huella,
                    'tamanio_bytes': fp.tamanio_bytes, 'bitrate_video_kbps': fp.bitrate_video_kbps,
                    'fecha_escaneo': fecha_utc6, 'dispositivo': disp, 'dispositivo_id': disp_id,
                    'dispositivo_origen': disp_origen, 'dispositivo_origen_id': disp_origen_id,
                    'error': fp.error,
                }
            )
            fila = conn.execute('SELECT id FROM videos WHERE clave_unica = ?', (clave,)).fetchone()
            if fila is None:
                conn.rollback()
                return None
            video_id = int(fila['id'])
            conn.execute('DELETE FROM frame_hashes WHERE video_id = ?', (video_id,))
            if fp.frame_hashes:
                conn.executemany(
                    "INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES (?, ?, ?)",
                    ((video_id, pos, val) for pos, val in enumerate(fp.frame_hashes)),
                )
            conn.commit()
            return video_id
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        _imprimir(f'[ERROR DB] {fp.nombre}: {exc}', 'red')
        return None


# ============================================================================
# Conversión de filas a VideoFingerprint
# ============================================================================
def _row_dict_a_fingerprint(fila: dict, frame_hashes: List[str]) -> VideoFingerprint:
    return VideoFingerprint(
        ruta=fila['ruta'],
        nombre=fila['nombre'],
        duracion=float(fila['duracion']),
        resolucion=(int(fila['ancho']), int(fila['alto'])),
        fps=float(fila['fps']),
        codec_video=fila['codec_video'],
        codec_audio=fila['codec_audio'],
        hash_archivo=fila['hash_archivo'],
        hash_rapido=fila['hash_rapido'],
        frame_hashes=frame_hashes,
        frame_intervalo=float(fila['frame_intervalo']),
        audio_huella=fila['audio_huella'],
        tamanio_bytes=int(fila['tamanio_bytes']),
        bitrate_video_kbps=float(fila['bitrate_video_kbps']),
        error=fila['error'],
        dispositivo=fila.get('dispositivo', '') or '',
        dispositivo_id=fila.get('dispositivo_id', '') or '',
    )


def _fila_sqlite_a_fingerprint(fila: sqlite3.Row, frame_hashes: List[str]) -> VideoFingerprint:
    claves = fila.keys()
    return VideoFingerprint(
        ruta=fila['ruta'],
        nombre=fila['nombre'],
        duracion=fila['duracion'],
        resolucion=(fila['ancho'], fila['alto']),
        fps=fila['fps'],
        codec_video=fila['codec_video'],
        codec_audio=fila['codec_audio'],
        hash_archivo=fila['hash_archivo'],
        hash_rapido=fila['hash_rapido'],
        frame_hashes=frame_hashes,
        frame_intervalo=fila['frame_intervalo'],
        audio_huella=fila['audio_huella'],
        tamanio_bytes=fila['tamanio_bytes'],
        bitrate_video_kbps=fila['bitrate_video_kbps'],
        error=fila['error'],
        dispositivo=(fila['dispositivo'] if 'dispositivo' in claves else '') or '',
        dispositivo_id=(fila['dispositivo_id'] if 'dispositivo_id' in claves else '') or '',
    )


# ============================================================================
# Consultas
# ============================================================================
def obtener_todos(conn: Any) -> List[VideoFingerprint]:
    is_pg = _es_pg(conn)

    if is_pg:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM videos WHERE error IS NULL ORDER BY fecha_escaneo DESC, id DESC")
            filas = [dict(r) for r in cur.fetchall()]
        if not filas:
            return []
        vistas: Set[Tuple[str, int, int]] = set()
        filas_unicas: List[dict] = []
        for fila in filas:
            identidad = _identidad_fisica(fila['ruta'])
            if identidad is not None:
                clave = (fila.get('dispositivo_id', '') or '', identidad[0], identidad[1])
                if clave in vistas:
                    continue
                vistas.add(clave)
            filas_unicas.append(fila)
        filas_unicas.sort(key=lambda f: f['duracion'])
        ids = [int(f['id']) for f in filas_unicas]
        hashes_por_video: Dict[int, List[str]] = {v_id: [] for v_id in ids}
        if ids:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    "SELECT video_id, hash_valor FROM frame_hashes WHERE video_id IN %s ORDER BY video_id, posicion",
                    (tuple(ids),))
                for fh in cur.fetchall():
                    hashes_por_video[int(fh['video_id'])].append(fh['hash_valor'])
        return [_row_dict_a_fingerprint(f, hashes_por_video[int(f['id'])]) for f in filas_unicas]
    else:
        filas = conn.execute("SELECT * FROM videos WHERE error IS NULL ORDER BY fecha_escaneo DESC, id DESC").fetchall()
        if not filas:
            return []
        vistas = set()
        filas_unicas = []
        for fila in filas:
            identidad = _identidad_fisica(fila['ruta'])
            if identidad is not None:
                disp_id = fila['dispositivo_id'] if 'dispositivo_id' in fila.keys() else ''
                clave = (disp_id or '', identidad[0], identidad[1])
                if clave in vistas:
                    continue
                vistas.add(clave)
            filas_unicas.append(fila)
        filas_unicas.sort(key=lambda f: f['duracion'])
        ids = [int(f['id']) for f in filas_unicas]
        hashes_por_video = {v_id: [] for v_id in ids}
        if ids:
            marcadores = ','.join('?' for _ in ids)
            for fh in conn.execute(
                f"SELECT video_id, hash_valor FROM frame_hashes WHERE video_id IN ({marcadores}) ORDER BY video_id, posicion",
                ids,):
                hashes_por_video[int(fh['video_id'])].append(fh['hash_valor'])
        return [_fila_sqlite_a_fingerprint(f, hashes_por_video[int(f['id'])]) for f in filas_unicas]


def obtener_por_hash_archivo(conn: Any, hash_archivo: str) -> Optional[VideoFingerprint]:
    if not hash_archivo:
        return None
    is_pg = _es_pg(conn)
    if is_pg:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM videos WHERE hash_archivo = %s LIMIT 1", (hash_archivo,))
            fila = cur.fetchone()
            if fila is None:
                return None
            fila_dict = dict(fila)
            cur.execute("SELECT hash_valor FROM frame_hashes WHERE video_id = %s ORDER BY posicion", (fila_dict['id'],))
            hashes = [f[0] for f in cur.fetchall()]
            return _row_dict_a_fingerprint(fila_dict, hashes)
    else:
        fila = conn.execute("SELECT * FROM videos WHERE hash_archivo = ? LIMIT 1", (hash_archivo,)).fetchone()
        if fila is None:
            return None
        hashes = [f['hash_valor'] for f in conn.execute(
            "SELECT hash_valor FROM frame_hashes WHERE video_id = ? ORDER BY posicion", (fila['id'],))]
        return _fila_sqlite_a_fingerprint(fila, hashes)


def existe_ruta(conn: Any, ruta: str, dispositivo_id: Optional[str] = None) -> bool:
    disp_id = dispositivo_id
    if disp_id is None:
        disp_id = _identificador_dispositivo_id()
    clave = _calcular_clave_unica(disp_id, ruta)
    is_pg = _es_pg(conn)
    if is_pg:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM videos WHERE clave_unica = %s LIMIT 1", (clave,))
            if cur.fetchone() is not None:
                return True
            identidad = _identidad_fisica(ruta)
            if identidad is None:
                return False
            cur.execute("SELECT ruta, dispositivo_id FROM videos WHERE dispositivo_id = %s", (disp_id,))
            for row in cur.fetchall():
                if _identidad_fisica(row[0]) == identidad:
                    return True
        return False
    else:
        coincidencia = conn.execute("SELECT 1 FROM videos WHERE clave_unica = ? LIMIT 1", (clave,)).fetchone()
        if coincidencia is not None:
            return True
        identidad = _identidad_fisica(ruta)
        if identidad is None:
            return False
        for fila in conn.execute('SELECT ruta, dispositivo_id FROM videos WHERE dispositivo_id = ?', (disp_id,)):
            if _identidad_fisica(fila['ruta']) == identidad:
                return True
        return False


# ============================================================================
# Eliminación
# ============================================================================
def eliminar_por_ruta(conn: Any, ruta: str, dispositivo_id: Optional[str] = None) -> bool:
    disp_id = dispositivo_id if dispositivo_id is not None else _identificador_dispositivo_id()
    clave = _calcular_clave_unica(disp_id, ruta)
    is_pg = _es_pg(conn)
    try:
        if is_pg:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM videos WHERE clave_unica = %s", (clave,))
                eliminado = cur.rowcount > 0
            conn.commit()
            return eliminado
        else:
            conn.execute('BEGIN IMMEDIATE')
            cursor = conn.execute('DELETE FROM videos WHERE clave_unica = ?', (clave,))
            eliminado = cursor.rowcount > 0
            conn.commit()
            return eliminado
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def eliminar_por_dispositivo_id(conn: Any, dispositivo_id: str) -> int:
    disp_id = (dispositivo_id or '').strip()
    if not disp_id:
        return 0
    is_pg = _es_pg(conn)
    try:
        if is_pg:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM videos WHERE dispositivo_id = %s", (disp_id,))
                count = cur.rowcount
            conn.commit()
            return count
        else:
            conn.execute('BEGIN IMMEDIATE')
            cursor = conn.execute('DELETE FROM videos WHERE dispositivo_id = ?', (disp_id,))
            count = cursor.rowcount
            conn.commit()
            return count
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


def reasignar_dispositivo_id(
    conn: Any,
    id_antiguo: str,
    id_nuevo: str,
) -> int:
    """Reasigna todas las huellas de un dispositivo_id antiguo al nuevo."""
    id_antiguo = (id_antiguo or '').strip()
    id_nuevo = (id_nuevo or '').strip()
    if not id_antiguo or not id_nuevo or id_antiguo == id_nuevo:
        return 0

    is_pg = _es_pg(conn)
    try:
        if is_pg:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    "SELECT id, ruta FROM videos WHERE dispositivo_id = %s",
                    (id_antiguo,))
                filas = cur.fetchall()
                actualizadas = 0
                for fila in filas:
                    nueva_clave = _calcular_clave_unica(id_nuevo, fila['ruta'])
                    try:
                        cur.execute(
                            "UPDATE videos SET dispositivo_id = %s, clave_unica = %s WHERE id = %s",
                            (id_nuevo, nueva_clave, fila['id']))
                        actualizadas += 1
                    except Exception as exc:
                        _imprimir(f'[AVISO] No se pudo reasignar id={fila["id"]}: {exc}', 'yellow')
            conn.commit()
            return actualizadas
        else:
            filas = conn.execute(
                "SELECT id, ruta FROM videos WHERE dispositivo_id = ?",
                (id_antiguo,)).fetchall()
            actualizadas = 0
            conn.execute('BEGIN IMMEDIATE')
            for fila in filas:
                nueva_clave = _calcular_clave_unica(id_nuevo, fila['ruta'])
                try:
                    conn.execute(
                        "UPDATE videos SET dispositivo_id = ?, clave_unica = ? WHERE id = ?",
                        (id_nuevo, nueva_clave, fila['id']))
                    actualizadas += 1
                except sqlite3.IntegrityError:
                    _imprimir(f'[AVISO] Colisión en id={fila["id"]}; se omite.', 'yellow')
            conn.commit()
            return actualizadas
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        _imprimir(f'[ERROR] Reasignación fallida: {exc}', 'red')
        return 0


# ============================================================================
# Estadísticas
# ============================================================================
def estadisticas(conn: Any) -> Dict[str, Any]:
    is_pg = _es_pg(conn)
    sql = """
        SELECT
            COUNT(*) AS total,
            COUNT(CASE WHEN error IS NULL THEN 1 END) AS con_huella,
            COUNT(CASE WHEN error IS NOT NULL THEN 1 END) AS con_error,
            ROUND(CAST(COALESCE(SUM(duracion), 0) / 3600.0 AS NUMERIC), 2) AS horas_totales,
            ROUND(CAST(COALESCE(SUM(tamanio_bytes), 0) / 1073741824.0 AS NUMERIC), 3) AS gb_totales
        FROM videos
    """
    if is_pg:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql)
            fila = dict(cur.fetchone())
            cur.execute("SELECT DISTINCT dispositivo FROM videos WHERE dispositivo <> '' ORDER BY dispositivo")
            dispositivos = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT DISTINCT dispositivo_origen FROM videos WHERE dispositivo_origen <> '' ORDER BY dispositivo_origen")
            dispositivos_origen = [r[0] for r in cur.fetchall()]
    else:
        base = conn.execute("""
            SELECT
                COUNT(*) AS total,
                COUNT(CASE WHEN error IS NULL THEN 1 END) AS con_huella,
                COUNT(CASE WHEN error IS NOT NULL THEN 1 END) AS con_error,
                COALESCE(SUM(duracion), 0) AS seg_totales,
                COALESCE(SUM(tamanio_bytes), 0) AS bytes_totales
            FROM videos
        """).fetchone()
        fila = {
            'total': base['total'] or 0,
            'con_huella': base['con_huella'] or 0,
            'con_error': base['con_error'] or 0,
            'horas_totales': round((base['seg_totales'] or 0) / 3600.0, 2),
            'gb_totales': round((base['bytes_totales'] or 0) / 1073741824.0, 3),
        }
        dispositivos = [f['dispositivo'] for f in conn.execute(
            "SELECT DISTINCT dispositivo FROM videos WHERE dispositivo <> '' ORDER BY dispositivo")]
        dispositivos_origen = [f['dispositivo_origen'] for f in conn.execute(
            "SELECT DISTINCT dispositivo_origen FROM videos WHERE dispositivo_origen <> '' ORDER BY dispositivo_origen")]
    return {
        'total_videos': int(fila['total'] or 0),
        'con_huella': int(fila['con_huella'] or 0),
        'con_error': int(fila['con_error'] or 0),
        'horas_totales': float(fila['horas_totales'] or 0.0),
        'gb_totales': float(fila['gb_totales'] or 0.0),
        'dispositivos': dispositivos,
        'dispositivos_origen': dispositivos_origen,
    }


# ============================================================================
# Exportación / importación
# ============================================================================
def exportar_json(conn: Any, ruta_json: str) -> int:
    is_pg = _es_pg(conn)
    if is_pg:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM videos ORDER BY id")
            filas = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT video_id, hash_valor FROM frame_hashes ORDER BY video_id, posicion")
            hashes_filas = cur.fetchall()
        hashes = {}
        for row in hashes_filas:
            hashes.setdefault(row['video_id'], []).append(row['hash_valor'])
        videos = [_row_dict_a_fingerprint(fila, hashes.get(fila['id'], [])) for fila in filas]
        detalles_por_clave = {}
        for fila in filas:
            clave = fila.get('clave_unica') or _calcular_clave_unica(fila.get('dispositivo_id', ''), fila['ruta'])
            detalles_por_clave[clave] = fila
    else:
        filas = conn.execute('SELECT * FROM videos ORDER BY id').fetchall()
        hashes = {}
        for fila in conn.execute('SELECT video_id, hash_valor FROM frame_hashes ORDER BY video_id, posicion'):
            hashes.setdefault(fila['video_id'], []).append(fila['hash_valor'])
        videos = [_fila_sqlite_a_fingerprint(fila, hashes.get(fila['id'], [])) for fila in filas]
        detalles_por_clave = {}
        for fila in filas:
            claves = fila.keys()
            clave = fila['clave_unica'] if 'clave_unica' in claves else fila['ruta']
            detalles_por_clave[clave] = dict(fila)

    datos: Dict[str, Any] = {
        'version': 2,
        'fecha_exportacion': _ahora_utc6_str(),
        'dispositivo_origen': _identificador_dispositivo(),
        'dispositivo_id_origen': _identificador_dispositivo_id(),
        'dispositivo_exportador': info_dispositivo_actual(),
        'estadisticas': estadisticas(conn),
        'videos': [],
    }
    for fp in videos:
        ancho, alto = fp.resolucion
        clave = _calcular_clave_unica(fp.dispositivo_id, fp.ruta)
        detalle = detalles_por_clave.get(clave, {})
        datos['videos'].append({
            'ruta': fp.ruta, 'nombre': fp.nombre, 'duracion': fp.duracion,
            'ancho': ancho, 'alto': alto, 'fps': fp.fps,
            'codec_video': fp.codec_video, 'codec_audio': fp.codec_audio,
            'hash_archivo': fp.hash_archivo, 'hash_rapido': fp.hash_rapido,
            'frame_hashes': fp.frame_hashes, 'frame_intervalo': fp.frame_intervalo,
            'audio_huella': fp.audio_huella, 'tamanio_bytes': fp.tamanio_bytes,
            'bitrate_video_kbps': fp.bitrate_video_kbps,
            'dispositivo': detalle.get('dispositivo', fp.dispositivo),
            'dispositivo_id': detalle.get('dispositivo_id', fp.dispositivo_id),
            'dispositivo_origen': detalle.get('dispositivo_origen', ''),
            'dispositivo_origen_id': detalle.get('dispositivo_origen_id', ''),
            'fecha_escaneo': detalle.get('fecha_escaneo', ''),
            'error': fp.error,
        })
    destino = Path(ruta_json).expanduser()
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open('w', encoding='utf-8') as archivo:
        json.dump(datos, archivo, ensure_ascii=False, indent=4)
        archivo.write('\n')
    return len(datos['videos'])


def importar_json(conn: Any, ruta_json: str, omitir_existentes: bool = True) -> Dict[str, int]:
    conteo = {'importados': 0, 'omitidos': 0, 'errores': 0}
    ruta = Path(ruta_json).expanduser()
    if not ruta.is_file():
        _imprimir(f'[ERROR] No se encontró el archivo: {ruta_json}', 'red')
        return conteo
    try:
        with ruta.open('r', encoding='utf-8') as archivo:
            datos = json.load(archivo)
    except (OSError, json.JSONDecodeError) as exc:
        _imprimir(f'[ERROR] No se pudo leer el JSON: {exc}', 'red')
        conteo['errores'] += 1
        return conteo
    videos = datos.get('videos', [])
    dispositivo_origen = datos.get('dispositivo_origen', '') or ''
    dispositivo_origen_id = datos.get('dispositivo_id_origen', '') or ''
    # El conjunto se actualiza después de cada inserción para omitir repetidos
    # dentro del propio archivo JSON (aunque las rutas difieran).
    hashes_registrados, rutas_registradas = obtener_indices_existentes(conn)
    for video in videos:
        if not isinstance(video, dict):
            conteo['errores'] += 1
            continue
        ruta_video = video.get('ruta', '')
        if not isinstance(ruta_video, str) or not ruta_video:
            conteo['errores'] += 1
            continue
        disp_id_registro = (
            video.get('dispositivo_id')
            or video.get('dispositivo_origen_id')
            or dispositivo_origen_id
            or _identificador_dispositivo_id()
        )
        disp_nombre_registro = (
            video.get('dispositivo')
            or video.get('dispositivo_origen')
            or dispositivo_origen
            or _identificador_dispositivo()
        )
        sha_video = str(video.get('hash_archivo') or '').strip().lower()
        # No duplicar ningún SHA256 ya escaneado para ese mismo dispositivo.
        # No se omite si el mismo SHA pertenece a un dispositivo DIFERENTE.
        if disp_id_registro and len(sha_video) == 64 \
                and (disp_id_registro, sha_video) in hashes_registrados:
            conteo['omitidos'] += 1
            continue
        if omitir_existentes and (
            (disp_id_registro, ruta_video) in rutas_registradas
            or existe_ruta(conn, ruta_video, dispositivo_id=disp_id_registro)
        ):
            conteo['omitidos'] += 1
            continue
        try:
            hashes = video.get('frame_hashes', [])
            fp = VideoFingerprint(
                ruta=ruta_video,
                nombre=str(video.get('nombre', '')),
                duracion=float(video.get('duracion', 0.0)),
                resolucion=(int(video.get('ancho', 0)), int(video.get('alto', 0))),
                fps=float(video.get('fps', 0.0)),
                codec_video=str(video.get('codec_video', '')),
                codec_audio=str(video.get('codec_audio', '')),
                hash_archivo=str(video.get('hash_archivo', '')),
                hash_rapido=str(video.get('hash_rapido', '')),
                frame_hashes=[str(valor) for valor in hashes],
                frame_intervalo=float(video.get('frame_intervalo', 5.0)),
                audio_huella=video.get('audio_huella'),
                tamanio_bytes=int(video.get('tamanio_bytes', 0)),
                error=video.get('error'),
                bitrate_video_kbps=float(video.get('bitrate_video_kbps', 0) or 0),
                dispositivo=disp_nombre_registro,
                dispositivo_id=disp_id_registro,
            )
            video_id = guardar_fingerprint(
                conn, fp,
                dispositivo=disp_nombre_registro,
                dispositivo_id=disp_id_registro,
                dispositivo_origen=dispositivo_origen,
                dispositivo_origen_id=dispositivo_origen_id,
            )
            if video_id is None:
                conteo['errores'] += 1
            else:
                conteo['importados'] += 1
                if disp_id_registro:
                    rutas_registradas.add((disp_id_registro, ruta_video))
                    if len(sha_video) == 64:
                        hashes_registrados.add((disp_id_registro, sha_video))
        except (TypeError, ValueError, OverflowError):
            conteo['errores'] += 1
    return conteo


def exportar_txt(conn: Any, ruta_txt: str) -> int:
    from .reporter import _fmt_seg, _fmt_bytes
    is_pg = _es_pg(conn)
    if is_pg:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM videos ORDER BY id")
            filas = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT video_id, posicion, hash_valor FROM frame_hashes ORDER BY video_id, posicion")
            hashes_filas = cur.fetchall()
        hashes = {}
        for row in hashes_filas:
            hashes.setdefault(row['video_id'], []).append(row)
    else:
        filas = [dict(f) for f in conn.execute('SELECT * FROM videos ORDER BY id').fetchall()]
        hashes_filas = conn.execute(
            'SELECT video_id, posicion, hash_valor FROM frame_hashes ORDER BY video_id, posicion').fetchall()
        hashes = {}
        for row in hashes_filas:
            hashes.setdefault(row['video_id'], []).append(dict(row))
    info_disp = info_dispositivo_actual()
    etiquetas = {
        'id': 'Identificador', 'clave_unica': 'Clave única', 'ruta': 'Ruta', 'nombre': 'Nombre',
        'duracion': 'Duración', 'ancho': 'Ancho (píxeles)', 'alto': 'Alto (píxeles)',
        'fps': 'Cuadros por segundo', 'codec_video': 'Códec de vídeo', 'codec_audio': 'Códec de audio',
        'hash_archivo': 'SHA256 del archivo', 'hash_rapido': 'Hash rápido',
        'frame_intervalo': 'Intervalo entre cuadros (segundos)', 'audio_huella': 'Huella de audio',
        'tamanio_bytes': 'Tamaño del archivo', 'bitrate_video_kbps': 'Bitrate de vídeo (kbps)',
        'fecha_escaneo': 'Fecha de escaneo (UTC-6)', 'dispositivo': 'Dispositivo',
        'dispositivo_id': 'ID dispositivo', 'dispositivo_origen': 'Dispositivo de origen',
        'dispositivo_origen_id': 'ID dispositivo de origen', 'error': 'Error',
    }
    lineas = [
        'EXPORTACIÓN DE BASE DE DATOS — VIDUP',
        f'Exportado desde dispositivo: {info_disp["dispositivo"]} (ID {info_disp["dispositivo_id"]})',
        f'Plataforma: {info_disp["plataforma"]} ({info_disp["maquina"]})',
        f'Modo de conexión: {describir_conexion()}',
        f'Vídeos registrados: {len(filas)}',
        '',
    ]
    for indice, fila in enumerate(filas, 1):
        lineas.extend(['=' * 72, f'VÍDEO {indice}: {fila["nombre"]}'])
        for clave in fila.keys():
            valor = fila[clave]
            if clave == 'duracion':
                valor = f'{_fmt_seg(valor)} ({valor} segundos)'
            elif clave == 'tamanio_bytes':
                valor = f'{_fmt_bytes(valor)} ({valor} bytes)'
            elif valor is None or valor == '':
                valor = 'No disponible' if clave != 'error' else 'Ninguno'
            lineas.append(f'{etiquetas.get(clave, clave.replace("_", " "))}: {valor}')
        hashes_video = hashes.get(fila['id'], [])
        lineas.append(f'Cuadros de la huella: {len(hashes_video)}')
        lineas.extend(f'  Cuadro {h["posicion"] + 1}: {h["hash_valor"]}' for h in hashes_video)
        lineas.append('')
    destino = Path(ruta_txt)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text('\n'.join(lineas) + '\n', encoding='utf-8')
    return len(filas)


# ============================================================================
# Volcado Neon -> SQLite
# ============================================================================
def volcar_todo_a_sqlite(
    ruta_destino: str,
    lote: int = 200,
    progreso=None,
) -> Dict[str, int]:
    import time as _time
    if not _usa_neon():
        raise RuntimeError('No hay conexión Neon activa.')
    destino = Path(ruta_destino).expanduser().resolve()
    destino.parent.mkdir(parents=True, exist_ok=True)
    conteo = {'videos': 0, 'frames': 0, 'errores': 0, 'lotes_fallidos': 0}
    conn_local = conectar_sqlite(str(destino))
    try:
        def abrir_neon():
            url = os.environ['NEON_DATABASE_URL'].strip()
            c = psycopg2.connect(url, connect_timeout=5)
            c.autocommit = True
            return c
        conn_neon = None
        try:
            conn_neon = abrir_neon()
        except Exception as exc:
            _imprimir(f'[ERROR] No se pudo conectar a Neon: {exc}', 'red')
            return conteo
        try:
            with conn_neon.cursor() as cur:
                cur.execute('SELECT COUNT(*) FROM videos')
                total = int(cur.fetchone()[0] or 0)
            if total == 0:
                return conteo
            ultimo_id = 0
            while True:
                filas = None
                try:
                    with conn_neon.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                        cur.execute(
                            'SELECT * FROM videos WHERE id > %s ORDER BY id LIMIT %s',
                            (ultimo_id, lote),)
                        filas = [dict(r) for r in cur.fetchall()]
                    if not filas:
                        break
                    ids_lote = [int(f['id']) for f in filas]
                    hashes_por_video: Dict[int, List[str]] = {i: [] for i in ids_lote}
                    with conn_neon.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                        cur.execute(
                            'SELECT video_id, hash_valor FROM frame_hashes '
                            'WHERE video_id IN %s ORDER BY video_id, posicion',
                            (tuple(ids_lote),),)
                        for fh in cur.fetchall():
                            hashes_por_video[int(fh['video_id'])].append(fh['hash_valor'])
                except Exception as exc:
                    conteo['lotes_fallidos'] += 1
                    _imprimir(f'[AVISO] Conexión a Neon interrumpida: {exc}', 'yellow')
                    _imprimir('        Reintentando en 3 s...', 'yellow')
                    try:
                        conn_neon.close()
                    except Exception:
                        pass
                    _time.sleep(3)
                    try:
                        conn_neon = abrir_neon()
                    except Exception as exc2:
                        _imprimir(f'[ERROR] Reintento fallido: {exc2}', 'red')
                        _time.sleep(5)
                        try:
                            conn_neon = abrir_neon()
                        except Exception as exc3:
                            _imprimir(f'[ERROR] Segundo reintento fallido: {exc3}', 'red')
                            break
                    continue
                conn_local.execute('BEGIN IMMEDIATE')
                for fila in filas:
                    try:
                        fp = _row_dict_a_fingerprint(fila, hashes_por_video[int(fila['id'])])
                        ancho, alto = fp.resolucion
                        clave = fila.get('clave_unica') or _calcular_clave_unica(
                            fila.get('dispositivo_id', ''), fila['ruta'])
                        conn_local.execute(
                            """
                            INSERT INTO videos (
                                clave_unica, ruta, nombre, duracion, ancho, alto, fps,
                                codec_video, codec_audio, hash_archivo, hash_rapido,
                                frame_intervalo, audio_huella, tamanio_bytes,
                                bitrate_video_kbps, fecha_escaneo, dispositivo, dispositivo_id,
                                dispositivo_origen, dispositivo_origen_id, error
                            )
                            VALUES (
                                :clave_unica, :ruta, :nombre, :duracion, :ancho, :alto, :fps,
                                :codec_video, :codec_audio, :hash_archivo, :hash_rapido,
                                :frame_intervalo, :audio_huella, :tamanio_bytes,
                                :bitrate_video_kbps, :fecha_escaneo, :dispositivo, :dispositivo_id,
                                :dispositivo_origen, :dispositivo_origen_id, :error
                            )
                            ON CONFLICT(clave_unica) DO UPDATE SET
                                ruta = excluded.ruta,
                                nombre = excluded.nombre, duracion = excluded.duracion,
                                ancho = excluded.ancho, alto = excluded.alto, fps = excluded.fps,
                                codec_video = excluded.codec_video, codec_audio = excluded.codec_audio,
                                hash_archivo = excluded.hash_archivo, hash_rapido = excluded.hash_rapido,
                                frame_intervalo = excluded.frame_intervalo, audio_huella = excluded.audio_huella,
                                tamanio_bytes = excluded.tamanio_bytes,
                                bitrate_video_kbps = excluded.bitrate_video_kbps,
                                fecha_escaneo = excluded.fecha_escaneo,
                                dispositivo = excluded.dispositivo,
                                dispositivo_id = excluded.dispositivo_id,
                                dispositivo_origen = excluded.dispositivo_origen,
                                dispositivo_origen_id = excluded.dispositivo_origen_id,
                                error = excluded.error
                            """,
                            {
                                'clave_unica': clave,
                                'ruta': fp.ruta, 'nombre': fp.nombre, 'duracion': fp.duracion,
                                'ancho': ancho, 'alto': alto, 'fps': fp.fps,
                                'codec_video': fp.codec_video, 'codec_audio': fp.codec_audio,
                                'hash_archivo': fp.hash_archivo, 'hash_rapido': fp.hash_rapido,
                                'frame_intervalo': fp.frame_intervalo, 'audio_huella': fp.audio_huella,
                                'tamanio_bytes': fp.tamanio_bytes,
                                'bitrate_video_kbps': fp.bitrate_video_kbps,
                                'fecha_escaneo': fila.get('fecha_escaneo') or '',
                                'dispositivo': fila.get('dispositivo') or '',
                                'dispositivo_id': fila.get('dispositivo_id') or '',
                                'dispositivo_origen': fila.get('dispositivo_origen') or '',
                                'dispositivo_origen_id': fila.get('dispositivo_origen_id') or '',
                                'error': fp.error,
                            },)
                        fila_local = conn_local.execute(
                            'SELECT id FROM videos WHERE clave_unica = ?', (clave,)).fetchone()
                        if fila_local is None:
                            conteo['errores'] += 1
                            continue
                        video_id_local = int(fila_local['id'])
                        conn_local.execute('DELETE FROM frame_hashes WHERE video_id = ?', (video_id_local,))
                        if fp.frame_hashes:
                            conn_local.executemany(
                                'INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES (?, ?, ?)',
                                ((video_id_local, pos, val) for pos, val in enumerate(fp.frame_hashes)),)
                            conteo['frames'] += len(fp.frame_hashes)
                        conteo['videos'] += 1
                    except Exception as exc:
                        conteo['errores'] += 1
                        _imprimir(f'[ERROR DB] {fila.get("nombre", "?")}: {exc}', 'red')
                conn_local.commit()
                ultimo_id = max(int(f['id']) for f in filas)
                if progreso is not None:
                    try:
                        progreso(conteo['videos'], total, conteo['errores'])
                    except Exception:
                        pass
        finally:
            try:
                if conn_neon is not None:
                    conn_neon.close()
            except Exception:
                pass
    finally:
        cerrar(conn_local)
    return conteo

# ============================================================================
# Restauración transaccional de Neon desde SQLite (opción expresa)
# ============================================================================
def reemplazar_neon_desde_sqlite(ruta_fuente: str, directorio_respaldos: str) -> Dict[str, Any]:
    """Sustituye todos los registros de Neon de forma atómica.

    Requisitos: conexión online real, copia previa verificada, SQLite íntegra.
    No afecta los archivos de vídeo. No utiliza la conexión automática fallback.
    La interfaz es responsable de exigir la confirmación textual explícita.
    """
    _detectar_modo_conexion_una_vez()
    if _ESTADO_CONEXION['modo'] != 'neon' or not TIENE_PSYCOPG2:
        raise RuntimeError('Neon no está disponible; no se sustituirá ninguna base.')
    fuente = Path(ruta_fuente).expanduser().resolve()
    if not fuente.is_file() or fuente.suffix.lower() not in ('.db', '.sqlite', '.sqlite3'):
        raise ValueError('Se necesita un archivo SQLite existente (.db/.sqlite/.sqlite3).')
    conexion_sqlite = sqlite3.connect(fuente.as_uri() + '?mode=ro', uri=True)
    conexion_sqlite.row_factory = sqlite3.Row
    conn_neon = None
    try:
        comprobacion = conexion_sqlite.execute('PRAGMA integrity_check').fetchone()[0]
        if comprobacion != 'ok':
            raise ValueError(f'La base SQLite origen no es íntegra: {comprobacion}')
        tablas = {x[0] for x in conexion_sqlite.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'videos', 'frame_hashes'} <= tablas:
            raise ValueError('La SQLite seleccionada no tiene las tablas videos y frame_hashes.')
        columnas = {x[1] for x in conexion_sqlite.execute('PRAGMA table_info(videos)')}
        requeridas = {'id', 'ruta', 'nombre', 'duracion', 'ancho', 'alto', 'fps',
                      'codec_video', 'codec_audio', 'hash_archivo', 'hash_rapido',
                      'frame_intervalo', 'audio_huella', 'tamanio_bytes',
                      'bitrate_video_kbps', 'fecha_escaneo', 'dispositivo', 'dispositivo_id',
                      'dispositivo_origen', 'dispositivo_origen_id', 'error', 'clave_unica'}
        if not requeridas <= columnas:
            raise ValueError('La fuente no tiene las columnas requeridas: ' +
                             ', '.join(sorted(requeridas - columnas)))
        if list(conexion_sqlite.execute('PRAGMA foreign_key_check')):
            raise ValueError('Hay referencias inválidas entre vídeos y sus fotogramas.')
        cantidad_origen = conexion_sqlite.execute('SELECT COUNT(*) FROM videos').fetchone()[0]
        frames_origen = conexion_sqlite.execute('SELECT COUNT(*) FROM frame_hashes').fetchone()[0]
        if not cantidad_origen:
            raise ValueError('La base de origen está vacía. No se permite vaciar Neon desde este menú.')
        if conexion_sqlite.execute('''SELECT COUNT(*) FROM videos
                     WHERE clave_unica IS NULL OR TRIM(clave_unica) = '' ''').fetchone()[0]:
            raise ValueError('Hay identificadores únicos vacíos en la SQLite.')
        if conexion_sqlite.execute('''SELECT COUNT(*) FROM (
                     SELECT clave_unica FROM videos GROUP BY clave_unica HAVING COUNT(*) > 1)''').fetchone()[0]:
            raise ValueError('La SQLite contiene claves únicas duplicadas.')

        directorio = Path(directorio_respaldos).expanduser().resolve()
        directorio.mkdir(parents=True, exist_ok=True)
        respaldo = directorio / ('neon_antes_reemplazo_' +
            datetime.now(ZONA_MEXICO).strftime('%Y%m%d_%H%M%S') + '.db')
        if respaldo.exists():
            raise FileExistsError(f'El respaldo ya existe: {respaldo}')

        # Exigir un respaldo antes de tocar el contenido remoto.
        conteo_backup = volcar_todo_a_sqlite(str(respaldo), lote=200)
        if conteo_backup['errores'] or conteo_backup['lotes_fallidos']:
            raise RuntimeError('El respaldo previo de Neon presentó errores; no se sustituirá.')
        with sqlite3.connect(respaldo) as prueba:
            videos_previos = prueba.execute('SELECT COUNT(*) FROM videos').fetchone()[0]
            frames_previos = prueba.execute('SELECT COUNT(*) FROM frame_hashes').fetchone()[0]
        if (videos_previos != conteo_backup['videos'] or
                frames_previos != conteo_backup['frames']):
            raise RuntimeError('El respaldo descargado no coincide con su recuento; cancelado.')

        conn_neon = psycopg2.connect(os.environ['NEON_DATABASE_URL'].strip(), connect_timeout=8)
        conn_neon.autocommit = False
        campos = ['clave_unica', 'ruta', 'nombre', 'duracion', 'ancho', 'alto', 'fps',
                  'codec_video', 'codec_audio', 'hash_archivo', 'hash_rapido',
                  'frame_intervalo', 'audio_huella', 'tamanio_bytes',
                  'bitrate_video_kbps', 'fecha_escaneo', 'dispositivo', 'dispositivo_id',
                  'dispositivo_origen', 'dispositivo_origen_id', 'error']
        asignacion = {}
        try:
            with conn_neon.cursor() as cur:
                cur.execute('LOCK TABLE videos IN ACCESS EXCLUSIVE MODE')
                cur.execute('LOCK TABLE frame_hashes IN ACCESS EXCLUSIVE MODE')
                cur.execute('SELECT COUNT(*) FROM videos')
                if int(cur.fetchone()[0]) != videos_previos:
                    raise RuntimeError('Neon cambió mientras se generaba el respaldo. Cancelado.')
                cur.execute('DELETE FROM frame_hashes')
                cur.execute('DELETE FROM videos')
                sql = (f'INSERT INTO videos ({", ".join(campos)}) VALUES %s RETURNING id, clave_unica')
                viejo_a_nuevo = {}
                cursor_sqlite = conexion_sqlite.execute('SELECT * FROM videos ORDER BY id')
                while True:
                    lote = cursor_sqlite.fetchmany(200)
                    if not lote:
                        break
                    valores = [tuple(f[c] for c in campos) for f in lote]
                    devueltos = psycopg2.extras.execute_values(
                        cur, sql, valores, page_size=200, fetch=True)
                    for new_id, clave in devueltos:
                        asignacion[clave] = new_id
                    for fila in lote:
                        viejo_a_nuevo[int(fila['id'])] = asignacion[fila['clave_unica']]
                cursor_frames = conexion_sqlite.execute(
                    'SELECT video_id, posicion, hash_valor FROM frame_hashes ORDER BY video_id, posicion')
                while True:
                    lote = cursor_frames.fetchmany(1000)
                    if not lote:
                        break
                    vals = [(viejo_a_nuevo[f['video_id']], f['posicion'], f['hash_valor']) for f in lote]
                    psycopg2.extras.execute_values(cur,
                        'INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES %s',
                        vals, page_size=1000)
                cur.execute('SELECT COUNT(*) FROM videos')
                if cur.fetchone()[0] != cantidad_origen:
                    raise RuntimeError('Conteo de vídeos restaurados no coincide; rollback.')
                cur.execute('SELECT COUNT(*) FROM frame_hashes')
                if cur.fetchone()[0] != frames_origen:
                    raise RuntimeError('Conteo de frames restaurados no coincide; rollback.')
            conn_neon.commit()
        except BaseException:
            conn_neon.rollback()
            raise
        return {'videos': cantidad_origen, 'frames': frames_origen,
                'videos_previos': videos_previos, 'respaldo_previo': str(respaldo)}
    finally:
        conexion_sqlite.close()
        if conn_neon:
            conn_neon.close()
