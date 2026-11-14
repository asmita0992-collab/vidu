# -*- coding: utf-8 -*-
"""Bandeja de análisis auxiliares de VIDUP 2.6.

Guarda huellas analizadas en otra computadora separadas del catálogo activo.
Las publicaciones opcionales en Neon NUNCA crean vídeos, cambian propietarios,
rutas, SHA256 o marcas de eliminación. La aplicación directa, bajo una frase
explícita, solo completa datos ausentes si el registro remoto coincide con la
identidad original. La importación ordinaria corresponde al propietario.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from core.database import conectar_sqlite
from core.horario import fmt_ahora_utc6, id_utc6

_TABLA = 'vidup_escaneos_pendientes'
_SCHEMA = 'VIDUP_ANALISIS_EXTERNO_V1'

_SQLITE = f'''CREATE TABLE IF NOT EXISTS {_TABLA} (
    id TEXT PRIMARY KEY, paquete_id TEXT NOT NULL,
    propietario_id TEXT NOT NULL, procesador_id TEXT NOT NULL,
    clave_unica TEXT NOT NULL, sha256 TEXT NOT NULL,
    ruta_original TEXT NOT NULL, datos_json TEXT NOT NULL,
    fecha_registro TEXT NOT NULL, estado TEXT NOT NULL DEFAULT 'pendiente'
    CHECK (estado IN ('pendiente', 'publicado', 'aplicado'))
);
CREATE INDEX IF NOT EXISTS idx_pendientes_propietario ON {_TABLA}(propietario_id, estado);'''

_POSTGRES = f'''CREATE TABLE IF NOT EXISTS {_TABLA} (
    id TEXT PRIMARY KEY, paquete_id TEXT NOT NULL,
    propietario_id TEXT NOT NULL, procesador_id TEXT NOT NULL,
    clave_unica TEXT NOT NULL, sha256 TEXT NOT NULL,
    ruta_original TEXT NOT NULL, datos_json JSONB NOT NULL,
    generacion TEXT NOT NULL, fecha_registro TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    estado TEXT NOT NULL DEFAULT 'pendiente'
    CHECK (estado IN ('pendiente', 'aplicado'))
);'''


def _id_dato(propietario_id: str, dato: dict) -> str:
    """Idempotencia estable del contenido y del registro original."""
    raw = json.dumps({'propietario':propietario_id, 'dato':dato},
                     ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _verificar_dato(dato: dict, propietario_id: str) -> None:
    clave = str(dato.get('clave_unica') or '')
    sha = str(dato.get('hash_archivo') or '').lower()
    ruta = str(dato.get('ruta_original') or '')
    if not propietario_id or '|' in propietario_id or not clave.startswith(propietario_id + '|'):
        raise ValueError('La clave de vídeo no corresponde al propietario indicado.')
    if not ruta or not (len(sha) == 64 and all(c in '0123456789abcdef' for c in sha)):
        raise ValueError('El registro pendiente requiere ruta original y SHA256 válido.')
    frames = dato.get('frame_hashes') or []
    if not isinstance(frames, list) or len(frames) > 200000 or any(
        not isinstance(x,str) or len(x)>1024 for x in frames):
        raise ValueError('Huellas visuales mal formadas o demasiado grandes.')
    if not frames and not dato.get('audio_huella'):
        raise ValueError('El trabajo no contiene huellas nuevas.')
    if len(json.dumps(dato, ensure_ascii=False)) > 12_000_000:
        raise ValueError('Resultado individual demasiado grande (12 MB).')


def guardar_local(db: str, paquete: dict) -> dict:
    """Almacena resultados como pendientes en SQLite; no toca el catálogo."""
    if paquete.get('tipo') != _SCHEMA:
        raise ValueError('Formato de paquete externo no reconocido.')
    propietario_id = str(paquete.get('propietario_id') or '')
    procesador_id = str((paquete.get('procesador') or {}).get('id') or '')
    if not procesador_id:
        raise ValueError('Falta identidad del equipo que procesó los vídeos.')
    elementos = list(paquete.get('resultados') or [])
    if len(elementos) > 2000:
        raise ValueError('El paquete contiene demasiados elementos.')
    conn = conectar_sqlite(db)
    agregados = 0
    paquete_id = hashlib.sha256(json.dumps(paquete,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    try:
        conn.executescript(_SQLITE)
        conn.execute('BEGIN IMMEDIATE')
        for dato in elementos:
            _verificar_dato(dato, propietario_id)
            cur = conn.execute(f'''INSERT OR IGNORE INTO {_TABLA}
             (id, paquete_id, propietario_id, procesador_id, clave_unica,
              sha256, ruta_original, datos_json, fecha_registro)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
             (_id_dato(propietario_id, dato),paquete_id,propietario_id,procesador_id,
              dato['clave_unica'],dato['hash_archivo'].lower(),dato['ruta_original'],
              json.dumps(dato,ensure_ascii=False),fmt_ahora_utc6()))
            agregados += cur.rowcount
        conn.commit()
        return {'agregados':agregados,'total':len(elementos),'paquete_id':paquete_id}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def pendientes_locales(db: str, *, propietario_id: str | None = None) -> list[dict]:
    """Consulta la bandeja local sin cargar el contenido de las huellas."""
    conn = conectar_sqlite(db)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (_TABLA,)).fetchone():
            return []
        consulta = (f'SELECT id, propietario_id, procesador_id, clave_unica, '
                    f'fecha_registro, estado FROM {_TABLA}')
        args: tuple = ()
        if propietario_id:
            consulta += ' WHERE propietario_id=?'
            args = (propietario_id,)
        return [dict(f) for f in conn.execute(consulta+' ORDER BY fecha_registro DESC',args)]
    finally:
        conn.close()


def _abrir_neon():
    from core.database import TIENE_PSYCOPG2, psycopg2
    if os.environ.get('VIDUP_FORZAR_OFFLINE','').lower() in ('1','true','si','sí','yes'):
        raise ConnectionError('VIDUP está en modo sin conexión; Neon no se modificó.')
    if not TIENE_PSYCOPG2 or not os.environ.get('NEON_DATABASE_URL'):
        raise ConnectionError('Neon o psycopg2 no disponibles; el trabajo permanece en SQLite.')
    return psycopg2.connect(os.environ['NEON_DATABASE_URL'],connect_timeout=8)


def _generacion_actual(conn) -> str:
    with conn.cursor() as cur:
        cur.execute('SELECT generacion FROM vidup_sync_catalogo WHERE id=1')
        valor = cur.fetchone()
    if not valor or not valor[0]:
        raise RuntimeError('Neon todavía no tiene una generación protegida.')
    return str(valor[0])


def _valida_fila_remota(cur, propietario_id: str, dato: dict) -> tuple[int, str | None] | None:
    """Selecciona y bloquea exactamente el vídeo de origen, sin alterarlo."""
    cur.execute('''SELECT id, hash_archivo, ruta, tamanio_bytes, dispositivo_id, audio_huella
                   FROM videos WHERE clave_unica=%s FOR UPDATE''', (dato['clave_unica'],))
    fila = cur.fetchone()
    if (not fila or str(fila[1] or '').lower()!=str(dato['hash_archivo']).lower() or
        str(fila[2])!=str(dato['ruta_original']) or
        int(fila[3] or 0)!=int(dato['tamanio_bytes'] or 0) or
        str(fila[4])!=propietario_id):
        return None
    cur.execute('SELECT fecha_eliminacion FROM videos_eliminados WHERE clave_unica=%s',
                (dato['clave_unica'],))
    baja = cur.fetchone()
    if baja:
        # Un registro activo puede tener una marca histórica anterior al
        # último escaneo; se deja pendiente para evitar una reactivación ciega.
        return None
    return int(fila[0]), fila[5]


def subir_pendientes_neon(db: str, *, propietario_id: str | None = None) -> dict:
    """Publica en una tabla separada; NO actualiza videos ni frame_hashes."""
    local = conectar_sqlite(db)
    nube = None
    informe = {'enviados':0, 'ya_existentes':0, 'conflictos':[], 'sin_cambios':False}
    try:
        if not local.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'",
                             (_TABLA,)).fetchone():
            return {**informe,'sin_cambios':True}
        sql = (f'SELECT id, propietario_id, procesador_id, paquete_id, datos_json '
               f'FROM {_TABLA} WHERE estado IN (\'pendiente\',\'publicado\')')
        args = ()
        if propietario_id:
            sql += ' AND propietario_id=?'
            args = (propietario_id,)
        trabajos = local.execute(sql,args).fetchall()
        if not trabajos:
            return {**informe,'sin_cambios':True}
        nube = _abrir_neon()
        with nube.cursor() as cur:
            generacion = _generacion_actual(nube)
            cur.execute(_POSTGRES)
            for trabajo in trabajos:
                dato = json.loads(trabajo['datos_json'])
                propietario = trabajo['propietario_id']
                try:
                    _verificar_dato(dato, propietario)
                    if _valida_fila_remota(cur, propietario, dato) is None:
                        informe['conflictos'].append({'clave':dato['clave_unica'],
                            'motivo':'El registro Neon no coincide o posee una eliminación'})
                        continue
                    cur.execute(f'''INSERT INTO {_TABLA}
                      (id, paquete_id, propietario_id, procesador_id, clave_unica,
                       sha256, ruta_original, datos_json, generacion)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                      ON CONFLICT(id) DO NOTHING''',
                      (trabajo['id'],trabajo['paquete_id'],propietario,
                       trabajo['procesador_id'],dato['clave_unica'],dato['hash_archivo'],
                       dato['ruta_original'],trabajo['datos_json'],generacion))
                    if cur.rowcount:
                        informe['enviados'] += 1
                    else:
                        informe['ya_existentes'] += 1
                except (ValueError, TypeError) as exc:
                    informe['conflictos'].append({'clave':dato.get('clave_unica'), 'motivo':str(exc)})
            nube.commit()
        # Marcar solo después del commit real de Neon; si falla SQLite, el
        # reenvío se resuelve por idempotencia.
        for trabajo in trabajos:
            if not any(c['clave']==json.loads(trabajo['datos_json'])['clave_unica']
                       for c in informe['conflictos']):
                local.execute(f"UPDATE {_TABLA} SET estado='publicado' WHERE id=?",
                              (trabajo['id'],))
        local.commit()
        return informe
    except BaseException:
        if nube:
            nube.rollback()
        local.rollback()
        raise
    finally:
        local.close()
        if nube:
            nube.close()


def obtener_pendientes_neon(propietario_id: str, limite: int = 1000,
                           *, procesador_id: str | None = None) -> dict:
    """Descarga propuestas vigentes del propietario sin modificar Neon."""
    from core.database import conectar_neon_solo_lectura
    conn = conectar_neon_solo_lectura()
    try:
        generacion = _generacion_actual(conn)
        with conn.cursor() as cur:
            cur.execute('SELECT to_regclass(%s)', (_TABLA,))
            if not cur.fetchone()[0]:
                return {'generacion':generacion,'resultados':[],'ids':[]}
            filtro = ' AND procesador_id=%s' if procesador_id else ''
            argumentos = [propietario_id,generacion]
            if procesador_id:
                argumentos.append(procesador_id)
            argumentos.append(max(1,min(1000,limite)))
            cur.execute(f'''SELECT id, datos_json FROM {_TABLA}
                           WHERE propietario_id=%s AND generacion=%s
                             AND estado='pendiente' {filtro} ORDER BY fecha_registro
                           LIMIT %s''',tuple(argumentos))
            filas = cur.fetchall()
        return {'generacion':generacion,'resultados':[
            (f[1] if isinstance(f[1],dict) else json.loads(f[1])) for f in filas],
                'ids':[f[0] for f in filas]}
    finally:
        conn.close()


def marcar_neon_aplicados(ids: list[str], propietario_id: str) -> None:
    """Confirma la recepción tras el commit local; repetir no duplica huellas."""
    if not ids:
        return
    conn = _abrir_neon()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {_TABLA} SET estado='aplicado' WHERE id=ANY(%s) AND propietario_id=%s",
                        (ids,propietario_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def aplicar_pendientes_en_neon(ids: list[str], propietario_id: str,
                               *, confirmar: bool = False,
                               procesador_id: str | None = None) -> dict:
    """Aplicación directa EXCEPCIONAL: solo huellas ausentes, no catálogos.

    Requiere permiso explícito de UI; comprueba el catálogo de Neon bajo
    bloqueo por fila y no altera identidad ni ruta. Un conflicto aborta el lote.
    """
    if not confirmar:
        raise PermissionError('Debe confirmarse expresamente la aplicación remota.')
    if not ids:
        return {'actualizados':0,'sin_cambios':0,'respaldo':None}
    conn = _abrir_neon()
    try:
        with conn.cursor() as cur:
            # Compartir el mismo candado transaccional con el sincronizador.
            cur.execute('SELECT pg_advisory_xact_lock(%s)', (24061111,))
            generacion = _generacion_actual(conn)
            cur.execute('SELECT set_config(%s,%s,true)',('vidup.catalog_generation',generacion))
            filtro = ' AND procesador_id=%s' if procesador_id else ''
            args = [ids,propietario_id,generacion]
            if procesador_id:
                args.append(procesador_id)
            cur.execute(f'''SELECT id, datos_json FROM {_TABLA} WHERE id=ANY(%s)
                           AND propietario_id=%s AND generacion=%s AND estado='pendiente'
                           {filtro} ORDER BY id FOR UPDATE''',tuple(args))
            trabajos = cur.fetchall()
            if len(trabajos)!=len(set(ids)):
                raise RuntimeError('Cambió la bandeja pendiente o la generación de Neon; cancelado.')
            cambios=[]
            para_backup=[]
            for id_trabajo, raw in trabajos:
                dato = raw if isinstance(raw,dict) else json.loads(raw)
                _verificar_dato(dato,propietario_id)
                fila = _valida_fila_remota(cur,propietario_id,dato)
                if fila is None:
                    raise RuntimeError('Registro remoto no coincide; cancelado: '+dato['clave_unica'])
                video_id,audio_actual = fila
                cur.execute('SELECT posicion,hash_valor FROM frame_hashes WHERE video_id=%s ORDER BY posicion',(video_id,))
                anteriores=cur.fetchall()
                nuevas=[(i,h) for i,h in enumerate(dato.get('frame_hashes') or [])]
                if anteriores and nuevas and anteriores!=nuevas:
                    raise RuntimeError('Huellas visuales distintas; cancelado: '+dato['clave_unica'])
                if audio_actual and dato.get('audio_huella') and audio_actual!=dato['audio_huella']:
                    raise RuntimeError('Huellas de audio distintas; cancelado: '+dato['clave_unica'])
                cur.execute('SELECT fecha_escaneo,frame_intervalo FROM videos WHERE id=%s',(video_id,))
                fecha_anterior, intervalo_anterior = cur.fetchone()
                para_backup.append({'clave':dato['clave_unica'],'audio_anterior':audio_actual,
                                    'frames_anteriores':anteriores,
                                    'fecha_escaneo':str(fecha_anterior),
                                    'frame_intervalo':intervalo_anterior})
                cambios.append((id_trabajo,video_id,dato,not anteriores and bool(nuevas),
                                not audio_actual and bool(dato.get('audio_huella'))))
            # Archivo de rescate antes de cualquier escritura remota.
            from core.escaneo_externo import _carpeta
            ruta_backup = _carpeta()/f'neon_antes_huellas_externas_{id_utc6()}.json'
            ruta_backup.write_text(json.dumps({'generacion':generacion,
                'propietario_id':propietario_id,'datos':para_backup},ensure_ascii=False,indent=2),encoding='utf8')
            if not ruta_backup.is_file() or not ruta_backup.stat().st_size:
                raise RuntimeError('No se pudo verificar el respaldo previo.')
            actualizados=sin_cambios=0
            for trabajo_id,video_id,dato,agrega_frames,agrega_audio in cambios:
                if agrega_frames:
                    cur.executemany('INSERT INTO frame_hashes(video_id,posicion,hash_valor) VALUES(%s,%s,%s)',
                        [(video_id,i,h) for i,h in enumerate(dato['frame_hashes'])])
                if agrega_audio or agrega_frames:
                    cur.execute('''UPDATE videos SET
                       audio_huella=CASE WHEN audio_huella IS NULL OR audio_huella='' THEN %s ELSE audio_huella END,
                       frame_intervalo=CASE WHEN %s THEN %s ELSE frame_intervalo END,
                       fecha_escaneo=%s WHERE id=%s''',
                       (dato.get('audio_huella'), bool(agrega_frames),
                        float(dato.get('frame_intervalo') or 10),fmt_ahora_utc6(), video_id))
                    actualizados += 1
                else:
                    sin_cambios += 1
                cur.execute(f"UPDATE {_TABLA} SET estado='aplicado' WHERE id=%s",(trabajo_id,))
        conn.commit()
        return {'actualizados':actualizados,'sin_cambios':sin_cambios,'respaldo':str(ruta_backup)}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
