"""Completa únicamente metadatos faltantes, nunca hashes, rutas ni identidad.

La inspección se hace exclusivamente en el dispositivo cuyo ID coincide con
el registro. Operación incremental con respaldo JSON previo y auditoría.
"""
from __future__ import annotations
import csv
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any
from . import database, metadata, inventario

CAMPOS = ('duracion', 'ancho', 'alto', 'fps', 'codec_video', 'codec_audio', 'bitrate_video_kbps')
ZONA = timezone(timedelta(hours=-6))


def incompletos(conn, dispositivo_id: str, excluidas=()):
    pg = database._es_pg(conn)
    cursor = conn.cursor(cursor_factory=database.psycopg2.extras.DictCursor) if pg else conn.cursor()
    try:
        cursor.execute('''SELECT id, ruta, nombre, dispositivo_id, duracion, ancho, alto, fps,
                          codec_video, codec_audio, bitrate_video_kbps
                          FROM videos WHERE dispositivo_id = %s ORDER BY id''' if pg else
                       '''SELECT id, ruta, nombre, dispositivo_id, duracion, ancho, alto, fps,
                          codec_video, codec_audio, bitrate_video_kbps
                          FROM videos WHERE dispositivo_id = ? ORDER BY id''', (dispositivo_id,))
        filas = [dict(f) for f in cursor.fetchall()]
    finally:
        cursor.close()
    return [f for f in filas if not inventario.excluido(f['ruta'], excluidas)
            and any(not f.get(c) for c in CAMPOS)]


def ejecutar(conn, dispositivo_id: str, reportes: Path, excluidas=(), progreso=None) -> dict:
    """Respalda antes de editar; rellena únicamente NULL/0/'' de registros locales."""
    if database.info_dispositivo_actual()['dispositivo_id'] != dispositivo_id:
        raise ValueError('No es posible inspeccionar rutas de otro dispositivo.')
    candidatos = incompletos(conn, dispositivo_id, excluidas)
    reportes = Path(reportes)
    for sub in ('json', 'txt', 'csv'):
        (reportes / sub).mkdir(parents=True, exist_ok=True)
    sello = datetime.now(ZONA).strftime('%Y%m%d_%H%M%S_%f')
    respaldo = reportes / 'json' / f'metadatos_respaldo_{sello}.json'
    # El respaldo existe antes del primer UPDATE, incluso si no hay cambios.
    respaldo.write_text(json.dumps({'dispositivo_id': dispositivo_id,
                                   'registros_originales': candidatos}, ensure_ascii=False, indent=2), encoding='utf-8')
    eventos = []
    contador = {'revisados': len(candidatos), 'actualizados': 0, 'sin_archivo': 0,
                'sin_datos_nuevos': 0, 'error': 0}
    pg = database._es_pg(conn)
    for idx, fila in enumerate(candidatos, 1):
        ruta = Path(fila['ruta'])
        evento = {'id': fila['id'], 'ruta': fila['ruta'], 'estado': '', 'campos': {}}
        try:
            if not ruta.is_file():
                evento['estado'] = 'sin_archivo'
                contador['sin_archivo'] += 1
            else:
                info = metadata.extraer_metadatos(ruta)
                cambios = {c: info[c] for c in CAMPOS if not fila.get(c) and info.get(c)}
                if not cambios:
                    evento['estado'] = 'sin_datos_nuevos'
                    contador['sin_datos_nuevos'] += 1
                else:
                    # Guardas SQL impiden sustituir datos completados concurrentemente.
                    valores = list(cambios.values())
                    asignaciones = ', '.join(f'{c} = CASE WHEN {c} IS NULL OR {c} = '+
                         ("''" if c in ('codec_video','codec_audio') else '0')+
                         f' THEN '+('%s' if pg else '?')+f' ELSE {c} END' for c in cambios)
                    sql = (f'UPDATE videos SET {asignaciones} WHERE id = '+('%s' if pg else '?')+
                           ' AND dispositivo_id = '+('%s' if pg else '?')+' AND ruta = '+('%s' if pg else '?'))
                    cur = conn.cursor()
                    try:
                        cur.execute(sql, (*valores, fila['id'], dispositivo_id, fila['ruta']))
                        if cur.rowcount != 1:
                            raise RuntimeError('Registro modificado o no encontrado')
                        conn.commit()
                    finally:
                        cur.close()
                    evento.update(estado='actualizado', campos=cambios)
                    contador['actualizados'] += 1
        except Exception as exc:
            conn.rollback()
            evento.update(estado='error', error=str(exc)[:300])
            contador['error'] += 1
        eventos.append(evento)
        if progreso:
            progreso(idx, len(candidatos), evento['estado'])
    resumen = {'fecha_utc6': datetime.now(ZONA).isoformat(), 'dispositivo_id': dispositivo_id,
               'respaldo': str(respaldo), 'conteo': contador, 'eventos': eventos}
    json_path = reportes / 'json' / f'metadatos_resultado_{sello}.json'
    txt_path = reportes / 'txt' / f'metadatos_resultado_{sello}.txt'
    csv_path = reportes / 'csv' / f'metadatos_resultado_{sello}.csv'
    json_path.write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding='utf-8')
    with txt_path.open('w', encoding='utf-8') as f:
        f.write(f'METADATOS FALTANTES — {resumen["fecha_utc6"]}\nDispositivo ID: {dispositivo_id}\n')
        for k,v in contador.items():
            f.write(f'{k}: {v}\n')
        f.write(f'Respaldo original: {respaldo}\n\n')
        for item in eventos:
            f.write(f'{item["estado"]} | ID {item["id"]} | {item["ruta"]} | {item["campos"]}\n')
    with csv_path.open('w', encoding='utf-8-sig', newline='') as f:
        wr = csv.writer(f)
        wr.writerow(['id', 'ruta', 'estado', 'campos_actualizados', 'error'])
        for item in eventos:
            wr.writerow([item['id'],item['ruta'],item['estado'],json.dumps(item['campos'],ensure_ascii=False),item.get('error','')])
    resumen['reportes'] = {'json': str(json_path), 'txt': str(txt_path), 'csv': str(csv_path)}
    return resumen
