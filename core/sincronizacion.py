# -*- coding: utf-8 -*-
"""Sincronización VIDUP, revisión 2.4: SQLite primero, Neon como repositorio.

No se aplican automáticamente marcas de eliminación de ninguna base.
No hay descargas destructivas ni INSERT OR REPLACE. El plan completo se muestra
antes de modificar datos; cualquier conflicto se deja intacto y se informa.
Las operaciones se repiten de forma idempotente tras fallos de red.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.database import (
    _es_pg, _usa_neon, cerrar, conectar_sqlite, info_dispositivo_actual,
)
from core.horario import fmt_ahora_utc6
from core.presentacion import imprimir

DIR_SCRIPT = Path(__file__).resolve().parent.parent
VIDEO_COLUMNS = (
    'clave_unica', 'ruta', 'nombre', 'duracion', 'ancho', 'alto', 'fps',
    'codec_video', 'codec_audio', 'hash_archivo', 'hash_rapido',
    'frame_intervalo', 'audio_huella', 'tamanio_bytes', 'bitrate_video_kbps',
    'fecha_escaneo', 'dispositivo', 'dispositivo_id', 'dispositivo_origen',
    'dispositivo_origen_id', 'error',
)
# No se sobrescriben identificadores, rutas, estado de error ni datos existentes
# usando solamente un aviso de sincronización.
MEJORABLES = (
    'hash_archivo', 'hash_rapido', 'duracion', 'ancho', 'alto', 'fps',
    'codec_video', 'codec_audio', 'audio_huella', 'tamanio_bytes',
    'bitrate_video_kbps', 'frame_intervalo',
)
NUMERICOS = {
    'duracion', 'ancho', 'alto', 'fps', 'tamanio_bytes',
    'bitrate_video_kbps', 'frame_intervalo',
}
LOTE = 100


class ErrorSincronizacion(RuntimeError):
    """Fallo verdadero: nunca equivale a cero cambios aplicados con éxito."""


class SincronizacionCancelada(ErrorSincronizacion):
    """El usuario canceló la operación después de revisar el plan."""


@dataclass
class PlanSincronizacion:
    subir: List[dict] = field(default_factory=list)
    bajar: List[dict] = field(default_factory=list)
    meta_nube: List[Tuple[str, dict]] = field(default_factory=list)
    meta_local: List[Tuple[str, dict]] = field(default_factory=list)
    frames_nube: List[str] = field(default_factory=list)
    frames_local: List[str] = field(default_factory=list)
    conflictos: List[str] = field(default_factory=list)
    protegidos: List[str] = field(default_factory=list)

    @property
    def cambios(self) -> int:
        return (len(self.subir) + len(self.bajar) + len(self.meta_nube) +
                len(self.meta_local) + len(self.frames_nube) + len(self.frames_local))


def obtener_ruta_db_local_dispositivo() -> Path:
    """Preserva la ruta histórica del equipo principal y las DB existentes."""
    info = info_dispositivo_actual()
    # Mantener exactamente la ruta de la instalación de escritorio existente.
    if info.get('dispositivo_id') == 'b7f7b638fce2d':
        return Path(r'C:\Users\tlahu\Desktop\Prueba_VIDUP\haggyescritorio_windows_amd64.db')
    nombre = re.sub(r'[^a-zA-Z0-9_\-]', '_',
                    info.get('dispositivo', 'dispositivo').lower()).strip('_') or 'dispositivo'
    ruta = DIR_SCRIPT / f'{nombre}.db'
    antigua = DIR_SCRIPT / 'vidup.db'
    if ruta.is_file():
        return ruta
    if antigua.is_file():
        try:
            antigua.rename(ruta)
            return ruta
        except OSError:
            return antigua
    return ruta


def asegurar_tabla_eliminados(conn: Any) -> None:
    sql = ('CREATE TABLE IF NOT EXISTS videos_eliminados '
           '(clave_unica TEXT PRIMARY KEY, fecha_eliminacion TEXT NOT NULL)')
    if _es_pg(conn):
        with conn.cursor() as cur:
            cur.execute(sql)
    else:
        conn.execute(sql)
    conn.commit()


def registrar_eliminacion(conn: Any, clave_unica: str, *, confirmar: bool = True) -> None:
    """Guarda eliminación SOLO en la base elegida, nunca en la otra.

    Suprimir el registro de un vídeo no implica sincronizar la eliminación. La
    marca local evita reimportarlo desde una nube desactualizada.
    """
    if not clave_unica:
        return
    sql_pg = ('INSERT INTO videos_eliminados (clave_unica, fecha_eliminacion) '
              'VALUES (%s, %s) ON CONFLICT (clave_unica) DO UPDATE '
              'SET fecha_eliminacion = EXCLUDED.fecha_eliminacion')
    sql_sq = ('INSERT INTO videos_eliminados (clave_unica, fecha_eliminacion) '
              'VALUES (?, ?) ON CONFLICT (clave_unica) DO UPDATE '
              'SET fecha_eliminacion = excluded.fecha_eliminacion')
    # La tabla se crea al abrir la BD; no silenciar errores de registro.
    if _es_pg(conn):
        with conn.cursor() as cur:
            cur.execute(sql_pg, (clave_unica, fmt_ahora_utc6()))
    else:
        conn.execute(sql_sq, (clave_unica, fmt_ahora_utc6()))
    if confirmar:
        conn.commit()


def _conectar_nube_directa() -> Any:
    """Crea conexión EXPLÍCITA a Neon; nunca degrada a SQLite silenciosamente."""
    if os.environ.get('VIDUP_FORZAR_OFFLINE', '').strip().lower() in ('1', 'true', 'yes', 'sí'):
        raise ErrorSincronizacion('Modo fuera de línea activo: Neon no se abrirá.')
    url = os.environ.get('NEON_DATABASE_URL', '').strip()
    if not url:
        raise ErrorSincronizacion('Falta NEON_DATABASE_URL.')
    try:
        import psycopg2
        from core.database import _SQL_CREAR_TABLAS_POSTGRES, _migrar_esquema
        conn = psycopg2.connect(url, connect_timeout=12,
                               options='-c statement_timeout=110000')
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(_SQL_CREAR_TABLAS_POSTGRES)
        _migrar_esquema(conn, es_pg=True)
        conn.autocommit = False
        return conn
    except Exception as exc:
        raise ErrorSincronizacion(f'Neon no se pudo abrir: {exc}') from exc


def _leer_videos(conn: Any) -> Dict[str, dict]:
    campos = ('id',) + VIDEO_COLUMNS
    sql = 'SELECT ' + ', '.join(campos) + ' FROM videos'
    if _es_pg(conn):
        with conn.cursor() as cur:
            cur.execute(sql)
            filas = cur.fetchall()
    else:
        filas = conn.execute(sql).fetchall()
    resultado = {}
    for fila in filas:
        video = dict(zip(campos, fila))
        clave = video['clave_unica']
        if not clave:
            raise ErrorSincronizacion('Se encontró un vídeo con clave_unica vacía.')
        if clave in resultado:
            raise ErrorSincronizacion(f'Clave duplicada en base: {clave}')
        resultado[clave] = video
    return resultado


def _huellas_resumen(conn: Any) -> Dict[int, Tuple[int, str]]:
    """Digest por vídeo sin cargar todas las huellas visuales en memoria."""
    sql = 'SELECT video_id, posicion, hash_valor FROM frame_hashes ORDER BY video_id, posicion'
    if _es_pg(conn):
        # Cursor servidor: streaming para bibliotecas grandes.
        cursor = conn.cursor(name='vidup_frame_digest')
        cursor.itersize = 2000
        cursor.execute(sql)
    else:
        cursor = conn.execute(sql)
    digests: Dict[int, Tuple[int, str]] = {}
    anterior = None
    cuenta = 0
    hash_actual = hashlib.sha256()
    try:
        for vid, pos, valor in cursor:
            vid = int(vid)
            if anterior != vid:
                if anterior is not None:
                    digests[anterior] = (cuenta, hash_actual.hexdigest())
                anterior, cuenta, hash_actual = vid, 0, hashlib.sha256()
            hash_actual.update(f'{pos}:'.encode())
            hash_actual.update(str(valor).encode('utf-8', 'replace'))
            hash_actual.update(b'\0')
            cuenta += 1
        if anterior is not None:
            digests[anterior] = (cuenta, hash_actual.hexdigest())
    finally:
        cursor.close()
    return digests


def _ausente(campo: str, valor: Any) -> bool:
    if valor is None or valor == '':
        return True
    if campo == 'hash_rapido' and valor == 'sha256_solo':
        return True
    if campo in NUMERICOS:
        # frame_intervalo=5 es el valor predeterminado; no asumir que es ausente.
        return float(valor or 0) <= 0
    return False


def _enriquecimientos(destino: dict, fuente: dict) -> dict:
    """Sólo completa campos vacíos: nunca sustituye valores existentes."""
    return {campo: fuente[campo] for campo in MEJORABLES
            if _ausente(campo, destino.get(campo)) and
            not _ausente(campo, fuente.get(campo))}


def _diferencias(destino: dict, fuente: dict) -> List[str]:
    ignorar = {'id', 'fecha_escaneo'}
    return [c for c in VIDEO_COLUMNS if c not in ignorar and
            not _ausente(c, destino.get(c)) and not _ausente(c, fuente.get(c))
            and destino.get(c) != fuente.get(c)]


def calcular_plan(local: Dict[str, dict], nube: Dict[str, dict],
                  frames_loc: Dict[int, Tuple[int, str]],
                  frames_nube: Dict[int, Tuple[int, str]],
                  eliminados_loc: set[str]) -> PlanSincronizacion:
    """Plan puro, comprobable sin conexión real y sin modificar ninguna DB."""
    plan = PlanSincronizacion()
    resumen_vacio = (0, '')
    for clave, v in local.items():
        vn = nube.get(clave)
        if vn is None:
            plan.subir.append(v)
            continue
        # Un mismo archivo/ruta puede haber cambiado de contenido. Jamás
        # mezclar huellas SHA256, audio o fotogramas si dos SHA256 difieren.
        if (v.get('hash_archivo') and vn.get('hash_archivo') and
                v['hash_archivo'] != vn['hash_archivo']):
            plan.conflictos.append(f'{clave}: SHA256 distinto; enriquecimiento bloqueado')
            continue
        faltante_nube = _enriquecimientos(vn, v)
        faltante_local = _enriquecimientos(v, vn)
        if faltante_nube:
            plan.meta_nube.append((clave, faltante_nube))
        if faltante_local:
            plan.meta_local.append((clave, faltante_local))
        d1 = frames_loc.get(int(v['id']), resumen_vacio)
        d2 = frames_nube.get(int(vn['id']), resumen_vacio)
        if d1[0] and not d2[0]:
            plan.frames_nube.append(clave)
        elif d2[0] and not d1[0]:
            plan.frames_local.append(clave)
        elif d1[0] and d2[0] and d1 != d2:
            plan.conflictos.append(f'{clave}: huellas visuales diferentes')
        diferencias = _diferencias(v, vn)
        if diferencias:
            plan.conflictos.append(f'{clave}: metadatos distintos ({", ".join(diferencias)})')
    for clave, v in nube.items():
        if clave not in local:
            if clave in eliminados_loc:
                plan.protegidos.append(clave)
            else:
                plan.bajar.append(v)
    # No duplicar rutas que sólo difieren por los separadores. Se dejan en
    # conflicto sin elegir arbitrariamente cuál es la versión correcta.
    local_normal = {}
    nube_normal = {}
    for clave in local:
        local_normal.setdefault(clave.replace('\\', '/'), set()).add(clave)
    for clave in nube:
        nube_normal.setdefault(clave.replace('\\', '/'), set()).add(clave)
    aliases = set()
    for normal in local_normal.keys() & nube_normal.keys():
        for a in local_normal[normal]:
            for b in nube_normal[normal]:
                if a != b:
                    aliases.add(a)
                    aliases.add(b)
                    plan.conflictos.append(f'{a} / {b}: rutas equivalentes; no duplicar')
    if aliases:
        plan.subir = [v for v in plan.subir if v['clave_unica'] not in aliases]
        plan.bajar = [v for v in plan.bajar if v['clave_unica'] not in aliases]

    return plan


def _imprimir_plan(plan: PlanSincronizacion, n_local: int, n_nube: int) -> None:
    imprimir(f'[cyan]PLAN DE SINCRONIZACIÓN · SQLite {n_local} vídeos · Neon {n_nube} vídeos[/]')
    imprimir(f'  Subir nuevos: {len(plan.subir)} · Bajar nuevos: {len(plan.bajar)}')
    imprimir(f'  Completar metadatos en Neon: {len(plan.meta_nube)} · SQLite: {len(plan.meta_local)}')
    imprimir(f'  Completar huellas en Neon: {len(plan.frames_nube)} · SQLite: {len(plan.frames_local)}')
    imprimir(f'  Conflictos NO sobrescritos: {len(plan.conflictos)}')
    imprimir(f'  Eliminaciones locales protegidas: {len(plan.protegidos)}')
    imprimir('  [yellow]Borrados automáticos: 0. Marcas de eliminación: NO se propagan.[/]')
    for item in plan.conflictos[:10]:
        imprimir(f'  [yellow]CONFLICTO: {item}[/]')
    if len(plan.conflictos) > 10:
        imprimir(f'  [yellow]... otros {len(plan.conflictos) - 10} conflictos.[/]')


def _guardar_plan(plan: PlanSincronizacion, ruta_local: Path,
                  n_local: int, n_nube: int) -> Path:
    """Bitácora del plan calculado; no es copia de seguridad de una depuración."""
    carpeta = DIR_SCRIPT / 'reportes' / 'sincronizacion' / 'json'
    carpeta.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    archivo = carpeta / ('plan_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.json')
    contenido = {
        'fecha': datetime.now().isoformat(timespec='seconds'),
        'sqlite': str(ruta_local), 'neon': 'configurada (URL omitida)',
        'videos_locales': n_local, 'videos_neon': n_nube,
        'subir': [v['clave_unica'] for v in plan.subir],
        'bajar': [v['clave_unica'] for v in plan.bajar],
        'completar_metadatos_neon': [k for k, _ in plan.meta_nube],
        'completar_metadatos_local': [k for k, _ in plan.meta_local],
        'completar_huellas_neon': plan.frames_nube,
        'completar_huellas_local': plan.frames_local,
        'conflictos_no_resueltos': plan.conflictos,
        'descargas_bloqueadas_por_eliminacion_local': plan.protegidos,
        'eliminaciones_automaticas': 0,
        'nota': 'El plan es informativo. Sin confirmación no se aplican cambios.'
    }
    with archivo.open('x', encoding='utf-8') as fp:
        json.dump(contenido, fp, ensure_ascii=False, indent=2)
    return archivo


def _frames_por_ids_local(conn: Any, ids: List[int]) -> dict[int, list]:
    if not ids:
        return {}
    signos = ', '.join('?' for _ in ids)
    datos = conn.execute(
        'SELECT video_id, posicion, hash_valor FROM frame_hashes '
        f'WHERE video_id IN ({signos}) ORDER BY video_id, posicion', ids).fetchall()
    sal = {}
    for id_, pos, huella in datos:
        sal.setdefault(int(id_), []).append((int(pos), huella))
    return sal


def _frames_por_ids_nube(conn: Any, ids: List[int]) -> dict[int, list]:
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute('SELECT video_id, posicion, hash_valor FROM frame_hashes '
                    'WHERE video_id = ANY(%s) ORDER BY video_id, posicion', (ids,))
        filas = cur.fetchall()
    salida = {}
    for id_, pos, huella in filas:
        salida.setdefault(int(id_), []).append((int(pos), huella))
    return salida


def _aplicar_plan(conn_loc: Any, conn_nube: Any, plan: PlanSincronizacion,
                  locales: dict, remotos: dict) -> Tuple[int, int, int]:
    """Sólo altas y enriquecimientos. NUNCA DELETE videos/frame_hashes."""
    import psycopg2.extras
    subida = bajada = 0
    columnas = ', '.join(VIDEO_COLUMNS)
    placeholders_sq = ', '.join('?' for _ in VIDEO_COLUMNS)
    sql_insert_sq = (f'INSERT INTO videos ({columnas}) VALUES ({placeholders_sq}) '
                     'ON CONFLICT (clave_unica) DO NOTHING')
    sql_insert_pg = (f'INSERT INTO videos ({columnas}) VALUES %s '
                     'ON CONFLICT (clave_unica) DO NOTHING RETURNING id, clave_unica')

    # ALTA EN NEON — en lotes, incluyendo las huellas en la misma transacción.
    for inicio in range(0, len(plan.subir), LOTE):
        lote = plan.subir[inicio:inicio+LOTE]
        ids = [int(v['id']) for v in lote]
        frames = _frames_por_ids_local(conn_loc, ids)
        valores = [tuple(v[c] for c in VIDEO_COLUMNS) for v in lote]
        with conn_nube.cursor() as cur:
            devueltos = psycopg2.extras.execute_values(
                cur, sql_insert_pg, valores, page_size=LOTE, fetch=True)
            if len(devueltos) != len(lote):
                raise ErrorSincronizacion('Neon cambió después de calcular el plan. Cancelado.')
            mapa = {clave: int(id_) for id_, clave in devueltos}
            frames_valores = [(mapa[v['clave_unica']], pos, valor)
                              for v in lote for pos, valor in frames.get(int(v['id']), [])]
            if frames_valores:
                psycopg2.extras.execute_values(
                    cur, 'INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES %s '
                         'ON CONFLICT (video_id, posicion) DO NOTHING',
                    frames_valores, page_size=1000)
        subida += len(lote)

    # ENRIQUECIMIENTO REMOTO — actualización condicionada al estado leído.
    for clave, cambios in plan.meta_nube:
        original = remotos[clave]
        campos = list(cambios)
        sentencia = 'UPDATE videos SET ' + ', '.join(f'{c} = %s' for c in campos)
        sentencia += ' WHERE id = %s AND clave_unica = %s'
        with conn_nube.cursor() as cur:
            cur.execute(sentencia, tuple(cambios[c] for c in campos) +
                        (int(original['id']), clave))
            if cur.rowcount != 1:
                raise ErrorSincronizacion('Un vídeo cambió en Neon durante la sincronización.')
        subida += 1

    for inicio in range(0, len(plan.frames_nube), LOTE):
        claves = plan.frames_nube[inicio:inicio+LOTE]
        registros = [locales[k] for k in claves]
        frames = _frames_por_ids_local(conn_loc, [int(v['id']) for v in registros])
        valores = [(int(remotos[k]['id']), pos, h)
                   for k in claves for pos, h in frames.get(int(locales[k]['id']), [])]
        if valores:
            with conn_nube.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur, 'INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES %s '
                         'ON CONFLICT (video_id, posicion) DO NOTHING',
                    valores, page_size=1000)
        subida += len(claves)

    # ALTA EN SQLITE — nunca INSERT OR REPLACE (evita cascadas de eliminación).
    for inicio in range(0, len(plan.bajar), LOTE):
        lote = plan.bajar[inicio:inicio+LOTE]
        frames = _frames_por_ids_nube(conn_nube, [int(v['id']) for v in lote])
        for v in lote:
            resultado = conn_loc.execute(sql_insert_sq, tuple(v[c] for c in VIDEO_COLUMNS))
            if resultado.rowcount != 1:
                raise ErrorSincronizacion('SQLite cambió después de calcular el plan. Cancelado.')
            id_nuevo = int(resultado.lastrowid)
            if frames.get(int(v['id'])):
                conn_loc.executemany(
                    'INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES (?, ?, ?)',
                    [(id_nuevo, pos, h) for pos, h in frames[int(v['id'])]])
            bajada += 1

    for clave, cambios in plan.meta_local:
        original = locales[clave]
        campos = list(cambios)
        sentencia = 'UPDATE videos SET ' + ', '.join(f'{c} = ?' for c in campos)
        sentencia += ' WHERE id = ? AND clave_unica = ?'
        resultado = conn_loc.execute(sentencia, tuple(cambios[c] for c in campos) +
                                    (int(original['id']), clave))
        if resultado.rowcount != 1:
            raise ErrorSincronizacion('Un vídeo cambió en SQLite durante la sincronización.')
        bajada += 1

    for inicio in range(0, len(plan.frames_local), LOTE):
        claves = plan.frames_local[inicio:inicio+LOTE]
        frames = _frames_por_ids_nube(conn_nube, [int(remotos[k]['id']) for k in claves])
        valores = [(int(locales[k]['id']), pos, h)
                   for k in claves for pos, h in frames.get(int(remotos[k]['id']), [])]
        if valores:
            conn_loc.executemany(
                'INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES (?, ?, ?)',
                valores)
        bajada += len(claves)
    return subida, bajada, 0


def sincronizar_local_y_nube(*, confirmar: bool = True,
                            ruta_local: Optional[str] = None,
                            solo_plan: bool = False) -> Tuple[int, int, int]:
    """Prepara/ejecuta sincronización manual segura, jamás elimina registros.

    En errores lanza ErrorSincronizacion; NUNCA devuelve (0,0,0) como éxito falso.
    `solo_plan` sirve para inspeccionar sin mutaciones (salvo inicializar SQLite
    si aún no existe; la base de escritorio se debe conservar intacta).
    """
    if os.environ.get('VIDUP_FORZAR_OFFLINE', '').strip().lower() in ('1', 'true', 'yes', 'sí'):
        raise ErrorSincronizacion('La sincronización está deshabilitada por VIDUP_FORZAR_OFFLINE.')
    if not _usa_neon():
        raise ErrorSincronizacion('No hay credenciales o controlador para Neon.')

    ruta = Path(ruta_local) if ruta_local else obtener_ruta_db_local_dispositivo()
    if not ruta.is_file():
        raise ErrorSincronizacion(
            f'No existe la SQLite local {ruta}. Escanea primero o selecciona otra base; '
            'no se creará una base vacía durante una sincronización.')

    conn_loc = None
    conn_nube = None
    nube_confirmada = False
    try:
        conn_loc = conectar_sqlite(str(ruta))
        conn_nube = _conectar_nube_directa()
        # Una sola sincronización por vez entre clientes VIDUP 2.4.
        with conn_nube.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_xact_lock(%s)', (24061111,))
            bloqueo = cur.fetchone()
            if not bloqueo or not bloqueo[0]:
                raise ErrorSincronizacion('Otro VIDUP está sincronizando; inténtalo después.')
        conn_loc.execute('BEGIN IMMEDIATE')

        locales = _leer_videos(conn_loc)
        remotos = _leer_videos(conn_nube)
        hashes_local = _huellas_resumen(conn_loc)
        hashes_nube = _huellas_resumen(conn_nube)
        elim_loc = {row[0] for row in conn_loc.execute(
            'SELECT clave_unica FROM videos_eliminados')}
        plan = calcular_plan(locales, remotos, hashes_local, hashes_nube, elim_loc)
        _imprimir_plan(plan, len(locales), len(remotos))
        archivo_plan = _guardar_plan(plan, ruta, len(locales), len(remotos))
        imprimir(f'[cyan]Plan verificable guardado: {archivo_plan}[/]')

        if solo_plan:
            return 0, 0, 0
        if plan.cambios == 0:
            if plan.conflictos:
                imprimir('[yellow]Sin cambios automáticos; hay conflictos por revisar.[/]')
            else:
                imprimir('[green]Sin cambios pendientes. No se eliminaron registros.[/]')
            return 0, 0, 0

        if confirmar:
            from core.presentacion import pedir_si_no
            if not pedir_si_no('¿Aplicar exclusivamente las altas y enriquecimientos mostrados?', False):
                raise SincronizacionCancelada('No se aplicaron cambios.')

        subida, bajada, eliminados = _aplicar_plan(conn_loc, conn_nube, plan, locales, remotos)
        with conn_nube.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM videos')
            total_nube = int(cur.fetchone()[0])
        total_local = int(conn_loc.execute('SELECT COUNT(*) FROM videos').fetchone()[0])
        if (total_local != len(locales) + len(plan.bajar) or
                total_nube != len(remotos) + len(plan.subir)):
            raise ErrorSincronizacion('Los conteos finales no corresponden al plan. Se canceló.')
        if list(conn_loc.execute('PRAGMA foreign_key_check')):
            raise ErrorSincronizacion('La base local tiene huellas huérfanas. Se canceló.')
        # PostgreSQL primero; un error posterior de SQLite no se anuncia como
        # éxito: las altas son idempotentes y se recuperan en el siguiente intento.
        conn_nube.commit()
        nube_confirmada = True
        conn_loc.commit()
        imprimir(f'[green]Sincronización aplicada: {subida} subidas, {bajada} bajadas, '
                 f'{eliminados} borrados.[/]')
        if plan.conflictos:
            imprimir(f'[yellow]Quedan {len(plan.conflictos)} conflictos sin sobrescribir.[/]')
        return subida, bajada, eliminados
    except (SincronizacionCancelada, ErrorSincronizacion):
        raise
    except Exception as exc:
        sufijo = (' Los cambios confirmados en Neon pueden mantenerse y son '
                  'recuperables en un reintento.' if nube_confirmada else '')
        raise ErrorSincronizacion(f'Fallo real de sincronización: {exc}.{sufijo}') from exc
    finally:
        for conexion in (conn_nube, conn_loc):
            if conexion is not None:
                try:
                    conexion.rollback()
                except Exception:
                    pass
                cerrar(conexion)


def sincronizar_al_iniciar() -> None:
    """Desactivada para preservar catálogos locales existentes.

    La sincronización necesita revisión humana del plan en Configuración > 7.
    """
    return
