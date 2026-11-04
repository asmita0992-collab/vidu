# -*- coding: utf-8 -*-

"""Módulo para la sincronización bidireccional entre la base de datos local y la nube (Neon)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Tuple, Dict, Any, Set

from core.presentacion import imprimir, CONSOLA
from core.horario import fmt_ahora_utc6
from core.database import (
    _usa_neon, conectar, conectar_sqlite, cerrar, info_dispositivo_actual,
    _es_pg, _SQL_CREAR_TABLAS_SQLITE, _SQL_CREAR_TABLAS_POSTGRES
)

DIR_SCRIPT: Path = Path(__file__).resolve().parent.parent


def obtener_ruta_db_local_dispositivo() -> Path:
    """Retorna la ruta de la base de datos local según el nombre del dispositivo."""
    info_disp = info_dispositivo_actual()
    nombre_disp = info_disp.get('dispositivo', 'dispositivo')
    nombre_sanitizado = re.sub(r'[^a-zA-Z0-9_\-]', '_', nombre_disp.lower()).strip('_')
    if not nombre_sanitizado:
        nombre_sanitizado = 'dispositivo'
    
    ruta_dispositivo = DIR_SCRIPT / f"{nombre_sanitizado}.db"
    ruta_vidup = DIR_SCRIPT / "vidup.db"

    if ruta_dispositivo.is_file():
        return ruta_dispositivo
    elif ruta_vidup.is_file():
        # Si ya existe vidup.db pero no la del dispositivo, renombrarla/usarla
        try:
            ruta_vidup.rename(ruta_dispositivo)
            return ruta_dispositivo
        except OSError:
            return ruta_vidup
    return ruta_dispositivo


def asegurar_tabla_eliminados(conn: Any) -> None:
    """Crea la tabla videos_eliminados si no existe."""
    is_pg = _es_pg(conn)
    sql = """
    CREATE TABLE IF NOT EXISTS videos_eliminados (
        clave_unica TEXT PRIMARY KEY,
        fecha_eliminacion TEXT NOT NULL
    );
    """
    if is_pg:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    else:
        conn.execute(sql)
        conn.commit()


def registrar_eliminacion(conn: Any, clave_unica: str) -> None:
    """Registra una clave única eliminada en la tabla videos_eliminados."""
    if not clave_unica:
        return
    asegurar_tabla_eliminados(conn)
    fecha = fmt_ahora_utc6()
    is_pg = _es_pg(conn)
    try:
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO videos_eliminados (clave_unica, fecha_eliminacion) VALUES (%s, %s) ON CONFLICT (clave_unica) DO NOTHING",
                    (clave_unica, fecha)
                )
            conn.commit()
        else:
            conn.execute(
                "INSERT OR IGNORE INTO videos_eliminados (clave_unica, fecha_eliminacion) VALUES (?, ?)",
                (clave_unica, fecha)
            )
            conn.commit()
    except Exception:
        pass


def sincronizar_local_y_nube() -> Tuple[int, int, int]:
    """Sincroniza bidireccionalmente la base local y Neon en la nube.

    Retorna (subidos, descargados, eliminados_sincronizados).
    """
    if not _usa_neon():
        return 0, 0, 0

    ruta_local = obtener_ruta_db_local_dispositivo()
    try:
        conn_local = conectar_sqlite(str(ruta_local))
        conn_nube = conectar('neon_cloud')
    except Exception as exc:
        imprimir(f'[yellow]⚠ No se pudo establecer conexión para la sincronización: {exc}[/]')
        return 0, 0, 0

    try:
        asegurar_tabla_eliminados(conn_local)
        asegurar_tabla_eliminados(conn_nube)

        # --- 1. Sincronizar eliminaciones ---
        # Obtener eliminados de local
        cur_loc = conn_local.cursor()
        cur_loc.execute("SELECT clave_unica, fecha_eliminacion FROM videos_eliminados")
        elim_loc = dict(cur_loc.fetchall())

        # Obtener eliminados de nube
        with conn_nube.cursor() as cur_nube:
            cur_nube.execute("SELECT clave_unica, fecha_eliminacion FROM videos_eliminados")
            elim_nube = dict(cur_nube.fetchall())

        # Replicar eliminaciones de Local -> Nube
        elim_sincro = 0
        with conn_nube.cursor() as cur_nube:
            for clave, fecha in elim_loc.items():
                if clave not in elim_nube:
                    cur_nube.execute(
                        "INSERT INTO videos_eliminados (clave_unica, fecha_eliminacion) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (clave, fecha)
                    )
                    cur_nube.execute("DELETE FROM videos WHERE clave_unica = %s", (clave,))
                    elim_sincro += 1
        conn_nube.commit()

        # Replicar eliminaciones de Nube -> Local
        for clave, fecha in elim_nube.items():
            if clave not in elim_loc:
                conn_local.execute(
                    "INSERT OR IGNORE INTO videos_eliminados (clave_unica, fecha_eliminacion) VALUES (?, ?)",
                    (clave, fecha)
                )
                conn_local.execute("DELETE FROM videos WHERE clave_unica = ?", (clave,))
                elim_sincro += 1
        conn_local.commit()

        todas_eliminadas = set(elim_loc.keys()) | set(elim_nube.keys())

        # --- 2. Sincronizar registros de videos ---
        cur_loc.execute("SELECT * FROM videos")
        col_names_loc = [desc[0] for desc in cur_loc.description]
        videos_loc = {row[col_names_loc.index('clave_unica')]: dict(zip(col_names_loc, row)) for row in cur_loc.fetchall()}

        with conn_nube.cursor() as cur_nube:
            cur_nube.execute("SELECT * FROM videos")
            col_names_nube = [desc[0] for desc in cur_nube.description]
            videos_nube = {row[col_names_nube.index('clave_unica')]: dict(zip(col_names_nube, row)) for row in cur_nube.fetchall()}

        # A) Subir de Local -> Nube
        subidos = 0
        for clave, v_loc in videos_loc.items():
            if clave in todas_eliminadas:
                continue
            if clave not in videos_nube:
                # Copiar video a Nube
                cols = [c for c in col_names_loc if c != 'id']
                val_placeholders = ", ".join(["%s"] * len(cols))
                col_names_str = ", ".join(cols)
                vals = [v_loc[c] for c in cols]
                with conn_nube.cursor() as cur_nube:
                    cur_nube.execute(
                        f"INSERT INTO videos ({col_names_str}) VALUES ({val_placeholders}) ON CONFLICT (clave_unica) DO NOTHING RETURNING id",
                        vals
                    )
                    row_id = cur_nube.fetchone()
                    if row_id:
                        vid_nube_id = row_id[0]
                        # Copiar frame_hashes
                        cur_loc.execute("SELECT posicion, hash_valor FROM frame_hashes WHERE video_id = ?", (v_loc['id'],))
                        fh_rows = cur_loc.fetchall()
                        if fh_rows:
                            fh_vals = [(vid_nube_id, pos, hval) for pos, hval in fh_rows]
                            cur_nube.executemany(
                                "INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                                fh_vals
                            )
                        subidos += 1
        conn_nube.commit()

        # B) Descargar de Nube -> Local
        descargados = 0
        for clave, v_nube in videos_nube.items():
            if clave in todas_eliminadas:
                continue
            if clave not in videos_loc:
                cols = [c for c in col_names_nube if c != 'id']
                val_placeholders = ", ".join(["?"] * len(cols))
                col_names_str = ", ".join(cols)
                vals = [v_nube[c] for c in cols]
                cur_ins = conn_local.execute(
                    f"INSERT OR IGNORE INTO videos ({col_names_str}) VALUES ({val_placeholders})",
                    vals
                )
                vid_loc_id = cur_ins.lastrowid
                if vid_loc_id:
                    with conn_nube.cursor() as cur_nube:
                        cur_nube.execute("SELECT posicion, hash_valor FROM frame_hashes WHERE video_id = %s", (v_nube['id'],))
                        fh_rows = cur_nube.fetchall()
                    if fh_rows:
                        fh_vals = [(vid_loc_id, pos, hval) for pos, hval in fh_rows]
                        conn_local.executemany(
                            "INSERT OR IGNORE INTO frame_hashes (video_id, posicion, hash_valor) VALUES (?, ?, ?)",
                            fh_vals
                        )
                    descargados += 1
        conn_local.commit()

        return subidos, descargados, elim_sincro

    except Exception as exc:
        imprimir(f'[yellow]⚠ Error durante la sincronización: {exc}[/]')
        return 0, 0, 0
    finally:
        cerrar(conn_local)
        cerrar(conn_nube)


def sincronizar_al_iniciar() -> None:
    """Ejecuta la sincronización automática al iniciar VIDUP si la nube está disponible."""
    if not _usa_neon():
        return
    ruta_local = obtener_ruta_db_local_dispositivo()
    imprimir(f'[cyan]🔄 Sincronizando base local ({ruta_local.name}) con la nube (Neon)...[/]')
    subidos, descargados, elim = sincronizar_local_y_nube()
    if subidos > 0 or descargados > 0 or elim > 0:
        imprimir(f'[spring_green2]✓ Sincronización completada: {subidos} subidos, {descargados} descargados, {elim} eliminados.[/]')
    else:
        imprimir('[spring_green2]✓ Bases de datos ya están sincronizadas.[/]')
