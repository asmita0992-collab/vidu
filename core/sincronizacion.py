# -*- coding: utf-8 -*-

"""Módulo para la sincronización bidireccional entre la base de datos local y la nube (Neon)."""

from __future__ import annotations

import sys
import os
import re
import time
from pathlib import Path
from typing import Tuple, Dict, Any, Set, List

from core.presentacion import imprimir, CONSOLA
from core.horario import fmt_ahora_utc6
from core.database import (
    _usa_neon, conectar, conectar_sqlite, cerrar, info_dispositivo_actual,
    _es_pg
)

try:
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn, TimeRemainingColumn
    TIENE_RICH_PROGRESS = True
except ImportError:
    TIENE_RICH_PROGRESS = False

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
    """Sincroniza bidireccionalmente la base local y Neon en la nube (incluyendo SHA256, visual, audio y metadatos).

    Retorna (subidos_o_actualizados, descargados_o_actualizados, eliminados_sincronizados).
    """
    if not _usa_neon():
        return 0, 0, 0

    ruta_local = obtener_ruta_db_local_dispositivo()
    try:
        conn_local = conectar_sqlite(str(ruta_local))
        conn_nube = conectar('neon_cloud')
    except Exception as exc:
        imprimir(f'[red]🛑 No se pudo conectar para sincronización: {exc}[/]')
        return 0, 0, 0

    subidos = 0
    descargados = 0
    elim_sincro = 0

    try:
        asegurar_tabla_eliminados(conn_local)
        asegurar_tabla_eliminados(conn_nube)

        # 1. Cargar eliminados
        cur_loc = conn_local.cursor()
        cur_loc.execute("SELECT clave_unica, fecha_eliminacion FROM videos_eliminados")
        elim_loc = dict(cur_loc.fetchall())

        with conn_nube.cursor() as cur_nube:
            cur_nube.execute("SELECT clave_unica, fecha_eliminacion FROM videos_eliminados")
            elim_nube = dict(cur_nube.fetchall())

        # Sincronizar eliminaciones
        for clave, fecha in elim_loc.items():
            if clave not in elim_nube:
                with conn_nube.cursor() as cur_nube:
                    cur_nube.execute(
                        "INSERT INTO videos_eliminados (clave_unica, fecha_eliminacion) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (clave, fecha)
                    )
                    cur_nube.execute("DELETE FROM videos WHERE clave_unica = %s", (clave,))
                elim_sincro += 1
        conn_nube.commit()

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

        # 2. Cargar lista de videos en local y nube
        cur_loc.execute("SELECT * FROM videos")
        col_names_loc = [desc[0] for desc in cur_loc.description]
        videos_loc = {row[col_names_loc.index('clave_unica')]: dict(zip(col_names_loc, row)) for row in cur_loc.fetchall()}

        cur_loc.execute("SELECT DISTINCT video_id FROM frame_hashes")
        loc_frames_ids = {row[0] for row in cur_loc.fetchall()}

        with conn_nube.cursor() as cur_nube:
            cur_nube.execute("SELECT * FROM videos")
            col_names_nube = [desc[0] for desc in cur_nube.description]
            videos_nube = {row[col_names_nube.index('clave_unica')]: dict(zip(col_names_nube, row)) for row in cur_nube.fetchall()}
            
            cur_nube.execute("SELECT DISTINCT video_id FROM frame_hashes")
            nube_frames_ids = {row[0] for row in cur_nube.fetchall()}

        for v in videos_loc.values():
            v['tiene_frames'] = v['id'] in loc_frames_ids
            v['tiene_audio'] = bool(v.get('audio_huella'))
            
        for v in videos_nube.values():
            v['tiene_frames'] = v['id'] in nube_frames_ids
            v['tiene_audio'] = bool(v.get('audio_huella'))

        # Identificar videos a subir/actualizar y a descargar/actualizar
        a_subir = []
        frames_pendientes_subir = []
        meta_pendientes_subir = []

        for k, v in videos_loc.items():
            if k in todas_eliminadas:
                continue
            if k not in videos_nube:
                a_subir.append(v)
            else:
                vn = videos_nube[k]
                if v['tiene_frames'] and not vn['tiene_frames']:
                    frames_pendientes_subir.append((v, vn['id']))
                # Si local tiene huella de audio, hash_rapido completo o metadatos que en la nube faltan
                if (v['tiene_audio'] and not vn['tiene_audio']) or (
                    v.get('hash_rapido') not in ('', 'sha256_solo') and vn.get('hash_rapido') in ('', 'sha256_solo')
                ) or (v.get('duracion', 0) > 0 and not vn.get('duracion')):
                    meta_pendientes_subir.append((v, vn['id']))

        a_descargar = []
        frames_a_descargar = []
        meta_a_descargar = []

        for k, v in videos_nube.items():
            if k in todas_eliminadas:
                continue
            if k not in videos_loc:
                a_descargar.append(v)
            else:
                vl = videos_loc[k]
                if v['tiene_frames'] and not vl['tiene_frames']:
                    frames_a_descargar.append((v, vl['id']))
                if (v['tiene_audio'] and not vl['tiene_audio']) or (
                    v.get('hash_rapido') not in ('', 'sha256_solo') and vl.get('hash_rapido') in ('', 'sha256_solo')
                ) or (v.get('duracion', 0) > 0 and not vl.get('duracion')):
                    meta_a_descargar.append((v, vl['id']))

        total_operaciones = (
            len(a_subir) + len(a_descargar) +
            len(frames_pendientes_subir) + len(frames_a_descargar) +
            len(meta_pendientes_subir) + len(meta_a_descargar)
        )
        if total_operaciones == 0:
            return subidos, descargados, elim_sincro

        hechos = 0
        lote_tamano = 100

        def mostrar_avance(proceso: str, actual: int, total: int):
            pct = (actual / total * 100) if total > 0 else 100.0
            sys.stdout.write(f"\r 🔄 {proceso}: {actual}/{total} ({pct:5.1f}%)   ")
            sys.stdout.flush()

        # A) Subir registros nuevos Local -> Nube
        if a_subir:
            cols = [c for c in col_names_loc if c not in ('id', 'tiene_frames', 'tiene_audio')]
            col_names_str = ", ".join(cols)
            val_placeholders = ", ".join(["%s"] * len(cols))
            sql_ins = f"INSERT INTO videos ({col_names_str}) VALUES ({val_placeholders}) ON CONFLICT (clave_unica) DO NOTHING RETURNING id, clave_unica"

            for i in range(0, len(a_subir), lote_tamano):
                lote = a_subir[i:i + lote_tamano]
                for v_loc in lote:
                    vals = [v_loc[c] for c in cols]
                    with conn_nube.cursor() as cur_nube:
                        cur_nube.execute(sql_ins, vals)
                        row = cur_nube.fetchone()
                        if row:
                            vid_nube_id = row[0]
                            cur_loc.execute("SELECT posicion, hash_valor FROM frame_hashes WHERE video_id = ?", (v_loc['id'],))
                            fh_rows = cur_loc.fetchall()
                            if fh_rows:
                                fh_vals = [(vid_nube_id, pos, hval) for pos, hval in fh_rows]
                                cur_nube.executemany(
                                    "INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                                    fh_vals
                                )
                            subidos += 1
                    hechos += 1
                    mostrar_avance("Subiendo a la nube", hechos, total_operaciones)
                conn_nube.commit()

        # A2) Subir frames faltantes Local -> Nube
        if frames_pendientes_subir:
            for v_loc, vid_nube_id in frames_pendientes_subir:
                cur_loc.execute("SELECT posicion, hash_valor FROM frame_hashes WHERE video_id = ?", (v_loc['id'],))
                fh_rows = cur_loc.fetchall()
                if fh_rows:
                    fh_vals = [(vid_nube_id, pos, hval) for pos, hval in fh_rows]
                    with conn_nube.cursor() as cur_nube:
                        cur_nube.executemany(
                            "INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                            fh_vals
                        )
                    subidos += 1
                hechos += 1
                mostrar_avance("Subiendo huellas visuales a la nube", hechos, total_operaciones)
            conn_nube.commit()

        # A3) Actualizar huella de audio y metadatos Local -> Nube
        if meta_pendientes_subir:
            sql_upd_nube = """
                UPDATE videos SET
                    audio_huella = COALESCE(NULLIF(%s, ''), audio_huella),
                    hash_rapido = CASE WHEN hash_rapido IN ('', 'sha256_solo') AND %s NOT IN ('', 'sha256_solo') THEN %s ELSE hash_rapido END,
                    frame_intervalo = CASE WHEN frame_intervalo <= 0 AND %s > 0 THEN %s ELSE frame_intervalo END,
                    duracion = CASE WHEN duracion <= 0 AND %s > 0 THEN %s ELSE duracion END,
                    ancho = CASE WHEN ancho <= 0 AND %s > 0 THEN %s ELSE ancho END,
                    alto = CASE WHEN alto <= 0 AND %s > 0 THEN %s ELSE alto END,
                    fps = CASE WHEN fps <= 0 AND %s > 0 THEN %s ELSE fps END,
                    codec_video = COALESCE(NULLIF(codec_video, ''), %s),
                    codec_audio = COALESCE(NULLIF(codec_audio, ''), %s),
                    bitrate_video_kbps = CASE WHEN bitrate_video_kbps <= 0 AND %s > 0 THEN %s ELSE bitrate_video_kbps END
                WHERE id = %s
            """
            for v_loc, vid_nube_id in meta_pendientes_subir:
                with conn_nube.cursor() as cur_nube:
                    cur_nube.execute(sql_upd_nube, (
                        v_loc.get('audio_huella') or '',
                        v_loc.get('hash_rapido') or '', v_loc.get('hash_rapido') or '',
                        v_loc.get('frame_intervalo') or 0.0, v_loc.get('frame_intervalo') or 0.0,
                        v_loc.get('duracion') or 0.0, v_loc.get('duracion') or 0.0,
                        v_loc.get('ancho') or 0, v_loc.get('ancho') or 0,
                        v_loc.get('alto') or 0, v_loc.get('alto') or 0,
                        v_loc.get('fps') or 0.0, v_loc.get('fps') or 0.0,
                        v_loc.get('codec_video') or '',
                        v_loc.get('codec_audio') or '',
                        v_loc.get('bitrate_video_kbps') or 0.0, v_loc.get('bitrate_video_kbps') or 0.0,
                        vid_nube_id
                    ))
                subidos += 1
                hechos += 1
                mostrar_avance("Actualizando audio/metadatos en la nube", hechos, total_operaciones)
            conn_nube.commit()

        # B) Descargar registros nuevos Nube -> Local
        if a_descargar:
            cols = [c for c in col_names_nube if c not in ('id', 'tiene_frames', 'tiene_audio')]
            col_names_str = ", ".join(cols)
            val_placeholders = ", ".join(["?"] * len(cols))
            sql_ins = f"INSERT OR IGNORE INTO videos ({col_names_str}) VALUES ({val_placeholders})"

            for i in range(0, len(a_descargar), lote_tamano):
                lote = a_descargar[i:i + lote_tamano]
                lote_ids = [v['id'] for v in lote]
                
                fh_por_video: Dict[int, List[Tuple[int, str]]] = {}
                with conn_nube.cursor() as cur_nube:
                    cur_nube.execute("SELECT video_id, posicion, hash_valor FROM frame_hashes WHERE video_id = ANY(%s)", (lote_ids,))
                    for vid_id, pos, hval in cur_nube.fetchall():
                        fh_por_video.setdefault(vid_id, []).append((pos, hval))

                for v_nube in lote:
                    vals = [v_nube[c] for c in cols]
                    cur_ins = conn_local.execute(sql_ins, vals)
                    vid_loc_id = cur_ins.lastrowid
                    if vid_loc_id:
                        fh_rows = fh_por_video.get(v_nube['id'], [])
                        if fh_rows:
                            fh_vals = [(vid_loc_id, pos, hval) for pos, hval in fh_rows]
                            conn_local.executemany(
                                "INSERT OR IGNORE INTO frame_hashes (video_id, posicion, hash_valor) VALUES (?, ?, ?)",
                                fh_vals
                            )
                        descargados += 1
                    hechos += 1
                    mostrar_avance("Descargando de la nube", hechos, total_operaciones)
                conn_local.commit()

        # B2) Descargar frames faltantes Nube -> Local
        if frames_a_descargar:
            for v_nube, vid_loc_id in frames_a_descargar:
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
                hechos += 1
                mostrar_avance("Descargando huellas visuales de la nube", hechos, total_operaciones)
            conn_local.commit()

        # B3) Actualizar huella de audio y metadatos Nube -> Local
        if meta_a_descargar:
            sql_upd_loc = """
                UPDATE videos SET
                    audio_huella = COALESCE(NULLIF(?, ''), audio_huella),
                    hash_rapido = CASE WHEN hash_rapido IN ('', 'sha256_solo') AND ? NOT IN ('', 'sha256_solo') THEN ? ELSE hash_rapido END,
                    frame_intervalo = CASE WHEN frame_intervalo <= 0 AND ? > 0 THEN ? ELSE frame_intervalo END,
                    duracion = CASE WHEN duracion <= 0 AND ? > 0 THEN ? ELSE duracion END,
                    ancho = CASE WHEN ancho <= 0 AND ? > 0 THEN ? ELSE ancho END,
                    alto = CASE WHEN alto <= 0 AND ? > 0 THEN ? ELSE alto END,
                    fps = CASE WHEN fps <= 0 AND ? > 0 THEN ? ELSE fps END,
                    codec_video = COALESCE(NULLIF(codec_video, ''), ?),
                    codec_audio = COALESCE(NULLIF(codec_audio, ''), ?),
                    bitrate_video_kbps = CASE WHEN bitrate_video_kbps <= 0 AND ? > 0 THEN ? ELSE bitrate_video_kbps END
                WHERE id = ?
            """
            for v_nube, vid_loc_id in meta_a_descargar:
                conn_local.execute(sql_upd_loc, (
                    v_nube.get('audio_huella') or '',
                    v_nube.get('hash_rapido') or '', v_nube.get('hash_rapido') or '',
                    v_nube.get('frame_intervalo') or 0.0, v_nube.get('frame_intervalo') or 0.0,
                    v_nube.get('duracion') or 0.0, v_nube.get('duracion') or 0.0,
                    v_nube.get('ancho') or 0, v_nube.get('ancho') or 0,
                    v_nube.get('alto') or 0, v_nube.get('alto') or 0,
                    v_nube.get('fps') or 0.0, v_nube.get('fps') or 0.0,
                    v_nube.get('codec_video') or '',
                    v_nube.get('codec_audio') or '',
                    v_nube.get('bitrate_video_kbps') or 0.0, v_nube.get('bitrate_video_kbps') or 0.0,
                    vid_loc_id
                ))
                descargados += 1
                hechos += 1
                mostrar_avance("Actualizando audio/metadatos desde la nube", hechos, total_operaciones)
            conn_local.commit()

        print()
        return subidos, descargados, elim_sincro

    except Exception as exc:
        print()
        imprimir(f'[red]🛑 Error durante la sincronización: {exc}[/]')
        imprimir('[yellow]⚠ Se detuvo la sincronización para evitar corrupción o bloqueos.[/]')
        return subidos, descargados, elim_sincro
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
        imprimir(f'[spring_green2]✓ Sincronización completada: {subidos} subidos/actualizados, {descargados} descargados/actualizados, {elim} eliminados.[/]')
    else:
        imprimir('[spring_green2]✓ Bases de datos ya están sincronizadas.[/]')
