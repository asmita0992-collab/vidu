# -*- coding: utf-8 -*-
"""Controles antirresurrección para el catálogo VIDUP (v2.4.5).

Distingue un catálogo local heredado de modificaciones locales posteriores a la
instalación, mediante marcas de cambios transaccionales. La autoridad de los
registros corresponde a su dispositivo de origen; una réplica no puede publicar
cambios de otro equipo. Un identificador de generación protege contra copias
SQLite anteriores a una sustitución integral de Neon.

No busca vídeos, no calcula SHA256, no elimina ficheros multimedia y no accede
a la red. Todas las marcas se almacenan dentro de la propia transacción SQLite.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable


SQL_CONTROL = """
CREATE TABLE IF NOT EXISTS vidup_sync_estado (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vidup_cambios_pendientes (
    clave_unica TEXT PRIMARY KEY,
    evento TEXT NOT NULL CHECK(evento IN ('video', 'eliminado')),
    fecha_cambio TEXT NOT NULL
);
"""


def _estado(conn: sqlite3.Connection, clave: str, defecto: str = '') -> str:
    fila = conn.execute('SELECT valor FROM vidup_sync_estado WHERE clave = ?',
                        (clave,)).fetchone()
    return str(fila[0]) if fila else defecto


def estado_generacion(conn: sqlite3.Connection) -> str:
    """Generación del catálogo Neon que ya adoptó esta base local."""
    return _estado(conn, 'generacion_aceptada')


def establecer_generacion(conn: sqlite3.Connection, valor: str) -> None:
    """Se llama solo tras constatar equivalencia o restaurar el catálogo."""
    conn.execute('INSERT INTO vidup_sync_estado (clave, valor) VALUES (?, ?) '
                 'ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor',
                 ('generacion_aceptada', str(valor)))


def suprimir_eventos(conn: sqlite3.Connection, valor: bool) -> None:
    """Evita que la descarga de Neon sea reinterpretada como cambios locales."""
    conn.execute('INSERT INTO vidup_sync_estado (clave, valor) VALUES (?, ?) '
                 'ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor',
                 ('sincronizando', '1' if valor else '0'))


def pendientes(conn: sqlite3.Connection) -> dict[str, str]:
    return {str(k): str(evt) for k, evt in conn.execute(
        'SELECT clave_unica, evento FROM vidup_cambios_pendientes')}


def marcar_sincronizados(conn: sqlite3.Connection, claves: Iterable[str]) -> None:
    conn.executemany('DELETE FROM vidup_cambios_pendientes WHERE clave_unica = ?',
                     ((str(clave),) for clave in set(claves)))


def instalar(conn: sqlite3.Connection, dispositivo_id: str) -> None:
    """Activa el diario solo para cambios posteriores a esta instalación.

    Los registros preexistentes NO se marcan como pendientes, por lo que una
    base antigua no puede publicar silenciosamente su historial a Neon.
    """
    if not dispositivo_id or '|' in dispositivo_id:
        raise ValueError('Identificador de dispositivo inválido para sincronización.')
    conn.executescript(SQL_CONTROL)
    conn.execute('INSERT INTO vidup_sync_estado (clave, valor) VALUES (?, ?) '
                 'ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor',
                 ('dispositivo_id', dispositivo_id))
    conn.execute("INSERT OR IGNORE INTO vidup_sync_estado VALUES ('sincronizando','0')")
    # Se crean con IF NOT EXISTS; nunca se reconstruye el diario al reabrir.
    cond_nuevo = """(SELECT valor FROM vidup_sync_estado WHERE clave='sincronizando')='0'
       AND NEW.dispositivo_id=(SELECT valor FROM vidup_sync_estado WHERE clave='dispositivo_id')"""
    cond_viejo = """(SELECT valor FROM vidup_sync_estado WHERE clave='sincronizando')='0'
       AND OLD.dispositivo_id=(SELECT valor FROM vidup_sync_estado WHERE clave='dispositivo_id')"""
    for nombre, tiempo, when, key, evento in (
        ('vidup_journal_video_insert', 'AFTER INSERT', cond_nuevo, 'NEW.clave_unica', 'video'),
        ('vidup_journal_video_update', 'AFTER UPDATE', cond_nuevo, 'NEW.clave_unica', 'video'),
        ('vidup_journal_video_delete', 'AFTER DELETE', cond_viejo, 'OLD.clave_unica', 'eliminado'),
    ):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {nombre}
          {tiempo} ON videos WHEN {when}
          BEGIN
            INSERT INTO vidup_cambios_pendientes(clave_unica, evento, fecha_cambio)
            VALUES ({key}, '{evento}', strftime('%Y-%m-%d %H:%M:%S','now','-6 hours'))
            ON CONFLICT(clave_unica) DO UPDATE SET evento=excluded.evento,
                fecha_cambio=excluded.fecha_cambio;
          END''')
    # Una modificación de clave equivale a dar de baja la ruta antigua.
    conn.execute('''CREATE TRIGGER IF NOT EXISTS vidup_journal_renombrado
        AFTER UPDATE OF clave_unica ON videos
        WHEN OLD.clave_unica <> NEW.clave_unica AND
          (SELECT valor FROM vidup_sync_estado WHERE clave='sincronizando')='0'
          AND OLD.dispositivo_id=(SELECT valor FROM vidup_sync_estado WHERE clave='dispositivo_id')
        BEGIN
          INSERT INTO videos_eliminados(clave_unica, fecha_eliminacion)
          VALUES(OLD.clave_unica, strftime('%Y-%m-%d %H:%M:%S','now','-6 hours'))
          ON CONFLICT(clave_unica) DO UPDATE SET fecha_eliminacion=excluded.fecha_eliminacion;
        END''')
    for nombre, tiempo in (('vidup_journal_marca_insert','AFTER INSERT'),
                            ('vidup_journal_marca_update','AFTER UPDATE')):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {nombre}
          {tiempo} ON videos_eliminados
          WHEN (SELECT valor FROM vidup_sync_estado WHERE clave='sincronizando')='0'
           AND substr(NEW.clave_unica, 1, length((SELECT valor FROM vidup_sync_estado
              WHERE clave='dispositivo_id')) + 1) =
              (SELECT valor FROM vidup_sync_estado WHERE clave='dispositivo_id') || '|'
          BEGIN
             INSERT INTO vidup_cambios_pendientes(clave_unica, evento, fecha_cambio)
             VALUES(NEW.clave_unica, 'eliminado', NEW.fecha_eliminacion)
             ON CONFLICT(clave_unica) DO UPDATE SET evento=excluded.evento,
                 fecha_cambio=excluded.fecha_cambio;
          END''')
    # Un DELETE normal de una fila propia debe ser registrable incluso si el
    # procedimiento llamante no escribe por separado videos_eliminados.
    conn.execute('''CREATE TRIGGER IF NOT EXISTS vidup_journal_baja_video
        AFTER DELETE ON videos
        WHEN (SELECT valor FROM vidup_sync_estado WHERE clave='sincronizando')='0'
          AND OLD.dispositivo_id=(SELECT valor FROM vidup_sync_estado WHERE clave='dispositivo_id')
        BEGIN
          INSERT INTO videos_eliminados(clave_unica, fecha_eliminacion)
          VALUES(OLD.clave_unica, strftime('%Y-%m-%d %H:%M:%S','now','-6 hours'))
          ON CONFLICT(clave_unica) DO UPDATE SET fecha_eliminacion=excluded.fecha_eliminacion;
        END''')
    conn.commit()
