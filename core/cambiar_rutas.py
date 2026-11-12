"""Reasignación transaccional de rutas de inventario, sin tocar los vídeos.

Permite un archivo exacto o una carpeta (recursiva) de UN dispositivo.
Siempre exporta copia completa de los registros y sus huellas antes de editar.
"""
from __future__ import annotations
import csv
import json
import ntpath
import posixpath
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from . import database, inventario

ZONA = timezone(timedelta(hours=-6))

def _destino(ruta: str, origen: str, destino: str, carpeta: bool) -> str:
    modulo = ntpath if inventario.es_windows(origen) else posixpath
    if carpeta:
        relativo = modulo.relpath(ruta, origen)
        if relativo == '..' or relativo.startswith('..' + modulo.sep):
            raise ValueError('Ruta fuera de la carpeta de origen')
        return modulo.normpath(modulo.join(destino, relativo))
    return destino

def preparar(conn, dispositivo_id: str, origen: str, destino: str, carpeta: bool = True) -> list[dict]:
    origen, destino = origen.strip(), destino.strip()
    if not origen or not destino or inventario.es_windows(origen) != inventario.es_windows(destino):
        raise ValueError('Se requieren rutas absolutas del mismo sistema de rutas.')
    if inventario.es_windows(origen):
        if not ntpath.isabs(origen) or not ntpath.isabs(destino):
            raise ValueError('La ruta Windows debe ser absoluta.')
    elif not posixpath.isabs(origen) or not posixpath.isabs(destino):
        raise ValueError('La ruta debe ser absoluta.')
    if inventario.normalizar(origen) == inventario.normalizar(destino):
        raise ValueError('Origen y destino son equivalentes.')
    registros = inventario.registros_dispositivo(conn, dispositivo_id)
    seleccion = [r for r in registros if
                 (inventario.dentro_de(r['ruta'], origen) if carpeta else
                  inventario.normalizar(r['ruta']) == inventario.normalizar(origen))]
    if not seleccion:
        raise ValueError('No existen registros con esa ruta para el dispositivo seleccionado.')
    ids_seleccion = {r['id'] for r in seleccion}
    existentes = {inventario.normalizar(r['ruta']) for r in registros if r['id'] not in ids_seleccion}
    usados = set()
    plan = []
    for r in seleccion:
        nueva = _destino(r['ruta'], origen, destino, carpeta)
        clave = inventario.normalizar(nueva)
        if clave in existentes or clave in usados:
            raise ValueError(f'La ruta destino colisiona con otro registro: {nueva}')
        usados.add(clave)
        plan.append({'id': r['id'], 'anterior': r['ruta'], 'nueva': nueva,
                     'clave_anterior': r['clave_unica'],
                     'clave_nueva': database._calcular_clave_unica(dispositivo_id, nueva),
                     'registro': r})
    return plan

def ejecutar(conn, dispositivo_id: str, dispositivo: str, origen: str, destino: str,
             carpeta: bool, reportes: Path, progreso: Callable | None = None) -> dict:
    plan = preparar(conn, dispositivo_id, origen, destino, carpeta)
    reportes = Path(reportes)
    for ext in ('json','txt','csv'):
        (reportes / ext).mkdir(parents=True, exist_ok=True)
    sello = datetime.now(ZONA).strftime('%Y%m%d_%H%M%S_%f')
    archivos = {ext: reportes / ext / f'cambio_rutas_{sello}.{ext}' for ext in ('json','txt','csv')}
    respaldo = inventario.agregar_huellas_frames(conn, [p['registro'] for p in plan])
    contenido = {'fecha_utc6': datetime.now(ZONA).isoformat(), 'dispositivo': dispositivo,
                 'dispositivo_id': dispositivo_id, 'origen': origen, 'destino': destino,
                 'tipo': 'carpeta' if carpeta else 'archivo', 'estado': 'RESPALDO PREVIO',
                 'registros_originales': respaldo,
                 'cambios': [{'id': p['id'], 'ruta_anterior': p['anterior'], 'ruta_nueva': p['nueva'],
                              'sha256': p['registro'].get('hash_archivo','')} for p in plan]}
    # Respaldo durable antes de ejecutar la primera modificación.
    def escribir(estado):
        contenido['estado'] = estado
        with archivos['json'].open('w',encoding='utf-8') as f:
            json.dump(contenido,f,ensure_ascii=False,indent=2,default=str)
            f.flush()
            import os
            os.fsync(f.fileno())
        with archivos['txt'].open('w',encoding='utf-8') as f:
            f.write(f'CAMBIO DE RUTAS — {estado}\nDispositivo: {dispositivo} ({dispositivo_id})\n')
            f.write(f'Origen: {origen}\nDestino: {destino}\nRegistros: {len(plan)}\n\n')
            for p in plan:
                f.write(f'ID {p["id"]}\n  ANTES: {p["anterior"]}\n  DESPUÉS: {p["nueva"]}\n'
                        f'  SHA256: {p["registro"].get("hash_archivo", "")}\n')
        with archivos['csv'].open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.writer(f); w.writerow(['id','dispositivo_id','ruta_anterior','ruta_nueva','sha256','estado'])
            for p in plan:
                w.writerow([p['id'],dispositivo_id,p['anterior'],p['nueva'],p['registro'].get('hash_archivo',''),estado])
    escribir('RESPALDO PREVIO - PENDIENTE')
    pg = database._es_pg(conn)
    marcador = '%s' if pg else '?'
    try:
        # Fase de prevalidación bloquea cambios concurrentes en los registros originales.
        cur = conn.cursor()
        try:
            if pg:
                cur.execute('SELECT id, ruta, clave_unica FROM videos WHERE dispositivo_id = %s FOR UPDATE', (dispositivo_id,))
            else:
                cur.execute('SELECT id, ruta, clave_unica FROM videos WHERE dispositivo_id = ?', (dispositivo_id,))
            actuales = {int(r[0]): (r[1], r[2]) for r in cur.fetchall()}
            for p in plan:
                if actuales.get(p['id']) != (p['anterior'],p['clave_anterior']):
                    raise RuntimeError('La base cambió desde el respaldo; operación cancelada.')
            # Las claves antiguas no deben resucitar al descargar de Neon.
            # La marca queda en la misma transacción que el cambio de ruta.
            from core.sincronizacion import registrar_eliminacion
            for p in plan:
                if p['clave_anterior'] != p['clave_nueva']:
                    registrar_eliminacion(conn, p['clave_anterior'], confirmar=False)
            # Utiliza claves temporales en fase 1 para tolerar intercambios de rutas.
            # No se altera ni nombre ni identidad, huellas o referencias a frames.
            for i,p in enumerate(plan,1):
                cur.execute(f'UPDATE videos SET clave_unica = {marcador} WHERE id = {marcador} AND dispositivo_id = {marcador}',
                            (f'__vidup_move_{sello}_{p["id"]}',p['id'],dispositivo_id))
                if cur.rowcount != 1: raise RuntimeError('Error al reservar registro')
            for i,p in enumerate(plan,1):
                cur.execute(f'UPDATE videos SET ruta = {marcador}, nombre = {marcador}, clave_unica = {marcador} WHERE id = {marcador} AND dispositivo_id = {marcador}',
                            (p['nueva'], ntpath.basename(p['nueva']) if inventario.es_windows(p['nueva']) else posixpath.basename(p['nueva']),
                             p['clave_nueva'],p['id'],dispositivo_id))
                if cur.rowcount != 1: raise RuntimeError('Error al modificar registro')
                if progreso: progreso(i,len(plan),p['nueva'])
            conn.commit()
        finally:
            cur.close()
    except BaseException:
        conn.rollback()
        raise
    try:
        escribir('ACTUALIZADO')
    except OSError:
        pass  # El respaldo previo permanece disponible.
    return {'actualizados':len(plan),'reportes':{k:str(v) for k,v in archivos.items()}}
