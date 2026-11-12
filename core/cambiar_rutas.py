"""Reasignación transaccional de rutas de inventario, sin tocar los vídeos.

Permite un archivo exacto, una carpeta o un lote de rutas ya verificadas.
Los cambios ordinarios conservan su respaldo histórico; el lote del menú 8.4
crea una auditoría reversible de rutas, sin releer vídeos ni huellas de frames,
y confirma todo de manera atómica en SQLite.
"""
from __future__ import annotations
import csv
import json
import ntpath
import posixpath
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
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


# ============================================================================
# Aplicación de rutas confirmadas por 8.2: transacción única y sin SHA256 extra
# ============================================================================
def ejecutar_lote(conn: Any, dispositivo_id: str, dispositivo: str,
                 coincidencias: Sequence[dict], reportes: Path,
                 progreso: Optional[Callable[[str, int, int], None]] = None,
                 cancelar: Any = None) -> dict:
    """Actualiza exclusivamente rutas y claves de vídeos identificados por 8.2.

    No accede a los archivos del disco, no recalcula huellas y no exporta los
    frames (que no cambian). Se auditan rutas anteriores para poder revertirlas.
    Todos los cambios admitidos se confirman con un único commit. Ctrl+C o un
    error durante la transacción hace rollback, incluidas las lápidas locales.
    """
    from . import ubicacion
    from .sincronizacion import registrar_eliminacion
    import os

    def comprobar_cancelacion():
        if cancelar is not None and cancelar.is_set():
            raise ubicacion.InterrupcionBusqueda('Actualización cancelada')

    comprobar_cancelacion()
    if not coincidencias:
        return {'aplicadas': [], 'fallidas': [], 'reportes': {}}
    if database._es_pg(conn):
        raise ValueError('La opción 8.4 solo admite la base SQLite local.')

    # Una única lectura del catálogo: no consultar toda la base 85 veces.
    todos = inventario.registros_dispositivo(conn, dispositivo_id)
    por_ruta = {inventario.normalizar(r['ruta']): r for r in todos}
    fallidas = []
    plan = []
    usadas = set()
    for c in coincidencias:
        comprobar_cancelacion()
        origen = str(c.get('ruta_anterior') or '')
        destino = str(c.get('ruta_nueva') or '')
        clave_origen = inventario.normalizar(origen)
        clave_destino = inventario.normalizar(destino)
        registrado = por_ruta.get(clave_origen)
        motivo = None
        if not origen or not destino or clave_origen == clave_destino:
            motivo = 'Rutas vacías o equivalentes'
        elif c.get('dispositivo_id') and c['dispositivo_id'] != dispositivo_id:
            motivo = 'El reporte corresponde a otro dispositivo'
        elif registrado is None:
            motivo = 'Ruta original no registrada en este dispositivo'
        elif not c.get('sha256'):
            motivo = 'Falta la confirmación SHA256 en el reporte'
        elif (str(registrado.get('hash_archivo') or '').strip().lower() !=
              str(c.get('sha256') or '').strip().lower()):
            motivo = 'La huella del registro local cambió desde 8.2'
        elif clave_origen in usadas:
            motivo = 'Origen duplicado en el reporte'
        if motivo:
            fallidas.append({**c, 'error': motivo})
            continue
        usadas.add(clave_origen)
        plan.append({
            'id': registrado['id'], 'anterior': registrado['ruta'],
            'nueva': destino, 'dispositivo_id': dispositivo_id,
            'clave_anterior': registrado['clave_unica'],
            'clave_nueva': database._calcular_clave_unica(dispositivo_id, destino),
            'sha256': registrado['hash_archivo'],
            'reporte': c,
        })

    # Los destinos ocupados solo son válidos si el ocupante se mueve en el lote.
    seleccionadas = {p['id'] for p in plan}
    destinos = set()
    admisibles = []
    for p in plan:
        destino = inventario.normalizar(p['nueva'])
        ocupante = por_ruta.get(destino)
        if destino in destinos or (ocupante and ocupante['id'] not in seleccionadas):
            fallidas.append({**p['reporte'], 'error': 'Colisión con otra ruta en el inventario'})
        else:
            destinos.add(destino)
            admisibles.append(p)
    # En caso de excluir un cambio, reconsiderar colisiones dependientes.
    while True:
        ids = {p['id'] for p in admisibles}
        nuevas = []
        cambio = False
        for p in admisibles:
            ocupante = por_ruta.get(inventario.normalizar(p['nueva']))
            if ocupante and ocupante['id'] not in ids:
                fallidas.append({**p['reporte'], 'error': 'El destino sigue ocupado'})
                cambio = True
            else:
                nuevas.append(p)
        admisibles = nuevas
        if not cambio:
            break
    plan = admisibles
    if not plan:
        return {'aplicadas': [], 'fallidas': fallidas, 'reportes': {}}

    # La auditoría contiene las rutas originales, no dumps voluminosos de
    # huellas/frame_hashes, porque esos datos no se tocan.
    reportes = Path(reportes)
    sello = datetime.now(ZONA).strftime('%Y%m%d_%H%M%S_%f')
    archivos = {ext: reportes / ext / f'reubicaciones_lote_{sello}.{ext}'
                for ext in ('json', 'txt', 'csv')}
    for archivo in archivos.values():
        archivo.parent.mkdir(parents=True, exist_ok=True)
    datos = {
        'fecha_utc6': datetime.now(ZONA).isoformat(),
        'dispositivo': dispositivo, 'dispositivo_id': dispositivo_id,
        'estado': 'PREPARADO - SIN APLICAR',
        'total': len(plan), 'reubicaciones': [
            {k:p[k] for k in ('id', 'anterior', 'nueva', 'sha256', 'clave_anterior', 'clave_nueva')}
            for p in plan],
    }
    def guardar_auditoria(estado):
        datos['estado'] = estado
        archivo_temp = archivos['json'].with_suffix('.json.tmp')
        with archivo_temp.open('w', encoding='utf-8') as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(archivo_temp, archivos['json'])
        with archivos['txt'].open('w', encoding='utf-8') as f:
            f.write(f'VIDUP - REUBICACIÓN DE RUTAS - {estado}\n')
            f.write(f'Dispositivo: {dispositivo} ({dispositivo_id})\n')
            f.write(f'Cambios: {len(plan)}\n\n')
            for p in plan:
                f.write(f'ID {p["id"]}\n  ANTES: {p["anterior"]}\n  DESPUÉS: {p["nueva"]}\n')
        with archivos['csv'].open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'dispositivo_id', 'ruta_anterior', 'ruta_nueva', 'sha256', 'estado'])
            for p in plan:
                writer.writerow([p['id'], dispositivo_id, p['anterior'], p['nueva'], p['sha256'], estado])

    guardar_auditoria('PREPARADO - SIN APLICAR')
    if progreso:
        progreso('Preparación completada; iniciando actualización', 0, len(plan))

    # Los lotes no hacen I/O multimedia; cada UPDATE es rápido y reversible.
    # BEGIN IMMEDIATE adquiere bloqueo escritor para no intercalar cambios.
    cur = conn.cursor()
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            for p in plan:
                comprobar_cancelacion()
                actual = conn.execute(
                    'SELECT ruta, clave_unica, hash_archivo FROM videos '
                    'WHERE id = ? AND dispositivo_id = ?',
                    (p['id'], dispositivo_id),
                ).fetchone()
                if (actual is None or actual['ruta'] != p['anterior'] or
                    actual['clave_unica'] != p['clave_anterior'] or
                    str(actual['hash_archivo'] or '').lower() != str(p['sha256'] or '').lower()):
                    raise RuntimeError('La base cambió durante la preparación; no se aplicó el lote')
            # Las lápidas locales evitan recuperar las claves antiguas desde Neon.
            for p in plan:
                comprobar_cancelacion()
                if p['clave_anterior'] != p['clave_nueva']:
                    registrar_eliminacion(conn, p['clave_anterior'], confirmar=False)
            # Dos pasadas permiten intercambios de rutas sin choques de UNIQUE.
            for p in plan:
                comprobar_cancelacion()
                cur.execute('UPDATE videos SET clave_unica = ? WHERE id = ? AND dispositivo_id = ?',
                            (f'__vidup_reub_{sello}_{p["id"]}', p['id'], dispositivo_id))
                if cur.rowcount != 1:
                    raise RuntimeError('No pudo reservarse un registro para actualizar')
            for i, p in enumerate(plan, 1):
                comprobar_cancelacion()
                nuevo_nombre = (ntpath.basename(p['nueva']) if inventario.es_windows(p['nueva'])
                                else posixpath.basename(p['nueva']))
                cur.execute('UPDATE videos SET ruta = ?, nombre = ?, clave_unica = ? '
                            'WHERE id = ? AND dispositivo_id = ?',
                            (p['nueva'], nuevo_nombre, p['clave_nueva'], p['id'], dispositivo_id))
                if cur.rowcount != 1:
                    raise RuntimeError('No pudo actualizarse la ruta de un vídeo')
                if progreso:
                    progreso(p['nueva'], i, len(plan))
            comprobar_cancelacion()
            conn.commit()
        except BaseException:
            conn.rollback()
            try:
                guardar_auditoria('CANCELADO O FALLIDO - SIN APLICAR')
            except OSError:
                pass
            raise
    finally:
        cur.close()
    try:
        guardar_auditoria('APLICADO')
    except OSError:
        pass
    return {
        'aplicadas': [p['reporte'] for p in plan],
        'fallidas': fallidas,
        'reportes': {k:str(v) for k,v in archivos.items()},
    }
