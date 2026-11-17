# -*- coding: utf-8 -*-
"""Publicación EXPLÍCITA y aislada de la SQLite de kDrive en Neon.

No se invoca desde el sincronizador general: este conserva la prohibición de
publicar accidentalmente la base aislada. Solo inserta registros kDrive nuevos,
no sustituye información de los dispositivos físicos.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from contextlib import closing
from typing import Any

from .kdrive_remoto import _sqlite_aislada, _json_canonico
from .kdrive_identidad import ID_DISPOSITIVO, NOMBRE_DISPOSITIVO
from .horario import fmt_ahora_utc6, id_utc6

RAIZ = Path(__file__).resolve().parent.parent
INFORMES = RAIZ / 'reportes' / 'kdrive' / 'neon'
FRASE = 'PUBLICAR KDRIVE EN NEON'
VIDEO_FIELDS = ('clave_unica','ruta','nombre','duracion','ancho','alto','fps',
                'codec_video','codec_audio','hash_archivo','hash_rapido',
                'frame_intervalo','audio_huella','tamanio_bytes','bitrate_video_kbps',
                'fecha_escaneo','dispositivo','dispositivo_id','dispositivo_origen',
                'dispositivo_origen_id','error')


def _conexion_neon(*, lectura: bool):
    try:
        from dotenv import load_dotenv
        load_dotenv(RAIZ / '.env', override=False)
    except ImportError:
        pass
    from .sincronizacion import _conectar_nube_directa
    conn = _conectar_nube_directa()
    if lectura:
        conn.rollback()
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ', autocommit=False)
    return conn


def _datos_locales(ruta: Path) -> tuple[dict[str,dict], dict[str,dict]]:
    videos = {}
    eventos = {}
    with closing(_sqlite_aislada(ruta)) as con:
        con.row_factory = sqlite3.Row
        columnas_v = {r[1] for r in con.execute('PRAGMA table_info(videos)')}
        columnas_f = {r[1] for r in con.execute('PRAGMA table_info(frame_hashes)')}
        version_sql = ('fingerprint_version' if 'fingerprint_version' in columnas_v
                       else "'phash64_v1' AS fingerprint_version")
        for row in con.execute('SELECT ' + ','.join(VIDEO_FIELDS) + ',' + version_sql +
                               ' FROM videos WHERE error IS NULL'):
            d = dict(row)
            if d['dispositivo_id'] != ID_DISPOSITIVO or not str(d['ruta']).startswith('kdrive:/Definitivos/'):
                raise ValueError('Registro ajeno a kDrive o ruta no autorizada.')
            if d['clave_unica'] != ID_DISPOSITIVO + '|' + d['ruta']:
                raise ValueError('La clave única no coincide con propietario/ruta.')
            videos[d['clave_unica']] = d
        t0_col = 'tiempo_inicio' if 'tiempo_inicio' in columnas_f else 'NULL AS tiempo_inicio'
        t1_col = 'tiempo_fin' if 'tiempo_fin' in columnas_f else 'NULL AS tiempo_fin'
        frames = con.execute('SELECT video_id,posicion,hash_valor,' + t0_col + ',' + t1_col +
                             ' FROM frame_hashes ORDER BY video_id,posicion').fetchall()
        ids = {row['id']: row['clave_unica'] for row in con.execute('SELECT id,clave_unica FROM videos')}
        for r in frames:
            clave = ids.get(r['video_id'])
            if clave in videos:
                videos[clave].setdefault('frames', []).append((int(r['posicion']),str(r['hash_valor'])))
                videos[clave].setdefault('frames_tiempos', []).append(
                    (int(r['posicion']),str(r['hash_valor']),r['tiempo_inicio'],r['tiempo_fin']))
        # Compatibilidad con SQLite 2.6.x: reconstruir solo las marcas de tiempo
        # ausentes, sin leer de nuevo el vídeo ni alterar su huella original.
        for video in videos.values():
            sucesion = video.get('frames_tiempos', [])
            if not sucesion:
                continue
            intervalo = float(video['frame_intervalo'] or 10.0)
            duracion = float(video['duracion'] or 0.0)
            completados = []
            for indice, (pos,h,inicio,fin) in enumerate(sucesion):
                calculado_inicio = float(pos) * intervalo
                calculado_fin = (duracion if indice == len(sucesion)-1 and
                                 duracion > calculado_inicio else calculado_inicio + intervalo)
                if duracion > 0:
                    calculado_inicio = min(calculado_inicio,duracion)
                    calculado_fin = min(calculado_fin,duracion)
                completados.append((pos,h,float(inicio) if inicio is not None else calculado_inicio,
                                    float(fin) if fin is not None else max(calculado_inicio,calculado_fin)))
            video['frames_tiempos'] = completados
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='kdrive_papelera_eventos'").fetchone():
            marcas_locales = {r['clave_unica']: str(r['fecha_eliminacion']) for r in
                              con.execute('SELECT clave_unica,fecha_eliminacion FROM videos_eliminados')}
            for r in con.execute('SELECT clave_unica,sha256,fecha,reporte FROM kdrive_papelera_eventos'):
                if not str(r['clave_unica']).startswith(ID_DISPOSITIVO+'|kdrive:/Definitivos/'):
                    raise ValueError('Evento de papelera de otro propietario.')
                if r['clave_unica'] in videos:
                    raise ValueError('Baja contradictoria: archivo activo y en papelera.')
                if marcas_locales.get(r['clave_unica']) != str(r['fecha']):
                    raise ValueError('Baja kDrive sin marca de eliminación coincidente: requiere conciliación.')
                eventos[r['clave_unica']] = dict(r)
    return videos, eventos


def _estado_neon(conn):
    """Lee SOLO el propietario kDrive, incluidos valores reales de huellas.

    Comparar únicamente cantidad de fotogramas no prueba equivalencia.
    """
    with conn.cursor() as cur:
        cur.execute('SELECT ' + ','.join(VIDEO_FIELDS) +
                    ' FROM videos WHERE dispositivo_id=%s', (ID_DISPOSITIVO,))
        activos = {r[0]: dict(zip(VIDEO_FIELDS, r)) | {'frames': []}
                   for r in cur.fetchall()}
        cur.execute('SELECT v.clave_unica,f.posicion,f.hash_valor '
                    'FROM frame_hashes f JOIN videos v ON v.id=f.video_id '
                    'WHERE v.dispositivo_id=%s ORDER BY v.clave_unica,f.posicion',
                    (ID_DISPOSITIVO,))
        for clave, pos, h in cur.fetchall():
            if clave in activos:
                activos[clave]['frames'].append((int(pos), str(h)))
        cur.execute('SELECT clave_unica,fecha_eliminacion FROM videos_eliminados WHERE clave_unica LIKE %s',
                    (ID_DISPOSITIVO+'|kdrive:/Definitivos/%',))
        bajas = {r[0]:str(r[1]) for r in cur.fetchall()}
    return activos, bajas


def _equivalentes(local: dict, remoto: dict) -> bool:
    """Compara los 21 campos originales y TODAS las huellas visuales."""
    if any(local.get(c) != remoto.get(c) for c in VIDEO_FIELDS):
        return False
    return local.get('frames',[]) == remoto.get('frames',[])


def _firma_local(videos: dict, eventos: dict) -> str:
    compactos = [{k: v[k] for k in VIDEO_FIELDS} |
                {'fingerprint_version': v.get('fingerprint_version','phash64_v1'),
                 'frames': v.get('frames',[]), 'frames_tiempos': v.get('frames_tiempos',[])}
                for v in videos.values()]
    compactos.sort(key=lambda x: x['clave_unica'])
    return hashlib.sha256(_json_canonico({'videos':compactos,'eventos':eventos})).hexdigest()


def planificar_publicacion(ruta_db: Path, destino: Path | None = None) -> dict:
    """Consulta Neon en modo SOLO LECTURA. Nunca publica al planificar."""
    ruta_db = Path(ruta_db).expanduser().resolve()
    videos, eventos = _datos_locales(ruta_db)
    conn = _conexion_neon(lectura=True)
    try:
        remotos, marcas = _estado_neon(conn)
    finally:
        conn.close()
    nuevos, iguales, conflictos = [], [], []
    for clave, v in videos.items():
        if clave in marcas:
            conflictos.append(dict(clave=clave, motivo='Neon conserva marca de eliminación: revisión manual'))
        elif clave not in remotos:
            nuevos.append(clave)
        else:
            r = remotos[clave]
            if _equivalentes(v, r):
                iguales.append(clave)
            else:
                conflictos.append(dict(clave=clave, motivo='Metadatos, SHA256 o contenido de huellas difieren de Neon'))
    bajas = []
    for clave, ev in eventos.items():
        if clave in remotos and str(remotos[clave]['hash_archivo']).lower() != str(ev['sha256']).lower():
            conflictos.append(dict(clave=clave, motivo='El vídeo de Neon cambió tras la eliminación remota'))
        elif clave not in marcas or clave in remotos:
            bajas.append(clave)
    plan = dict(version=1,tipo='publicacion_kdrive_neon',fecha=fmt_ahora_utc6(),
                db=str(ruta_db), dispositivo_id=ID_DISPOSITIVO,
                firma_sqlite=_firma_local(videos,eventos),
                nuevos=sorted(nuevos), iguales=len(iguales),
                bajas_verificadas=sorted(bajas), conflictos=conflictos,
                aviso='Solo se publicarán vídeos kDrive nuevos y bajas verificadas; otros dispositivos no se modifican.')
    plan['firma'] = hashlib.sha256(_json_canonico(plan)).hexdigest()
    if destino is None:
        destino=INFORMES/'planes'/f'plan_publicacion_kdrive_{id_utc6()}.json'
    destino=Path(destino).expanduser().resolve()
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    txt=destino.parent.parent/'txt'/(destino.stem+'.txt')
    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text(f'VIDUP · PUBLICACIÓN AISLADA kDrive → Neon\nSQLite: {ruta_db}\n'
                   f'Nuevos: {len(nuevos)} | Iguales: {len(iguales)} | Bajas verificadas: {len(bajas)} '
                   f'| Conflictos: {len(conflictos)}\n'
                   + '\n'.join(str(c) for c in conflictos) + '\n', encoding='utf-8')
    plan['ruta_plan']=str(destino)
    plan['ruta_txt']=str(txt)
    return plan


def aplicar_publicacion(plan_json: Path, *, confirmacion: str) -> dict:
    from .presentacion import confirma_frase
    if not confirma_frase(confirmacion, FRASE):
        raise ValueError('Operación no autorizada. Se requiere confirmación literal.')
    plan_json=Path(plan_json).expanduser().resolve()
    plan=json.loads(plan_json.read_text(encoding='utf-8'))
    firma=plan.pop('firma',None)
    if not firma or hashlib.sha256(_json_canonico(plan)).hexdigest()!=firma:
        raise ValueError('El archivo de plan fue modificado.')
    if plan.get('tipo')!='publicacion_kdrive_neon' or plan.get('dispositivo_id')!=ID_DISPOSITIVO:
        raise ValueError('Plan ajeno a kDrive.')
    if plan['conflictos']:
        raise ValueError('Hay conflictos de Neon sin resolver; se cancela TODO el lote.')
    videos,eventos=_datos_locales(Path(plan['db']))
    if _firma_local(videos,eventos)!=plan['firma_sqlite']:
        raise ValueError('La SQLite cambió después del plan; debe generarse uno nuevo.')
    conn=_conexion_neon(lectura=False)
    agregados=bajas=0
    try:
        with conn.cursor() as cur:
            # Serializa esta publicación, sin manipular tablas ni dispositivos ajenos.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('vidup:kdrive:publicar:v1'))")
            actuales,marcas=_estado_neon(conn)
            for clave in plan['nuevos']:
                if clave not in videos:
                    raise ValueError('No existe ya el vídeo local del plan.')
                if clave in actuales or clave in marcas:
                    raise ValueError('Neon cambió desde el plan (nuevos); se cancela todo.')
            for clave in plan['bajas_verificadas']:
                ev=eventos.get(clave)
                if ev is None:
                    raise ValueError('Falta prueba local de papelera.')
                if clave in actuales:
                    anterior = actuales[clave]
                    if str(anterior['hash_archivo']).lower() != str(ev['sha256']).lower():
                        raise ValueError('Neon contiene un SHA256 diferente al archivo enviado a papelera.')
                    if str(anterior['fecha_escaneo']) >= str(ev['fecha']):
                        raise ValueError('Neon contiene un escaneo posterior o simultáneo a la baja.')
            # Migración aditiva y transaccional para los tiempos del índice de
            # huellas. Se ejecuta SOLO tras confirmación explícita de publicación,
            # nunca al leer/planificar ni en la sincronización ordinaria.
            cur.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS fingerprint_version TEXT NOT NULL DEFAULT 'phash64_v1'")
            cur.execute('ALTER TABLE frame_hashes ADD COLUMN IF NOT EXISTS tiempo_inicio DOUBLE PRECISION')
            cur.execute('ALTER TABLE frame_hashes ADD COLUMN IF NOT EXISTS tiempo_fin DOUBLE PRECISION')
            for clave in plan['nuevos']:
                v=videos[clave]
                campos = VIDEO_FIELDS + ('fingerprint_version',)
                valores=tuple(v[k] for k in VIDEO_FIELDS) + (v.get('fingerprint_version','phash64_v1'),)
                cur.execute('INSERT INTO videos ('+','.join(campos)+') VALUES ('+','.join(['%s']*len(campos))+') '
                            'ON CONFLICT (clave_unica) DO NOTHING RETURNING id', valores)
                fila=cur.fetchone()
                if fila is None:
                    raise ValueError('Neon cambió durante la inserción; abortando transacción.')
                vid=int(fila[0])
                frames=v.get('frames',[])
                if frames:
                    from psycopg2.extras import execute_values
                    execute_values(cur,
                                   'INSERT INTO frame_hashes(video_id,posicion,hash_valor,tiempo_inicio,tiempo_fin) VALUES %s',
                                   [(vid,p,h,t0,t1) for p,h,t0,t1 in v.get('frames_tiempos',[])],
                                   page_size=500)
                agregados+=1
            for clave in plan['bajas_verificadas']:
                ev=eventos[clave]
                cur.execute('DELETE FROM videos WHERE clave_unica=%s AND dispositivo_id=%s AND hash_archivo=%s',
                            (clave, ID_DISPOSITIVO, ev['sha256']))
                cur.execute('''INSERT INTO videos_eliminados(clave_unica,fecha_eliminacion)
                            VALUES (%s,%s) ON CONFLICT(clave_unica) DO UPDATE
                            SET fecha_eliminacion=EXCLUDED.fecha_eliminacion''',
                            (clave,ev['fecha']))
                bajas+=1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    resumen=dict(fecha=fmt_ahora_utc6(),plan=str(plan_json),videos_nuevos=agregados,
                 bajas_verificadas=bajas,conflictos=0,
                 nota='No se modificaron registros con propietario distinto de kDrive.')
    INFORMES.joinpath('json').mkdir(parents=True,exist_ok=True)
    salida=INFORMES/'json'/f'publicacion_kdrive_{id_utc6()}.json'
    salida.write_text(json.dumps(resumen,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    txt=INFORMES/'txt'/(salida.stem+'.txt')
    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text('VIDUP · RESULTADO PUBLICACIÓN A NEON\n'+
                   f'Vídeos nuevos: {agregados}\nBajas verificadas: {bajas}\n'+
                   f'No se modificó ningún otro propietario.\n',encoding='utf-8')
    resumen['reporte']=str(salida)
    resumen['reporte_txt']=str(txt)
    return resumen


def publicar_interactivo():
    from .presentacion import imprimir, pedir_ruta, pedir_texto, rich_escape
    db=Path(pedir_ruta('SQLite kDrive a publicar', existente=True))
    plan=planificar_publicacion(db)
    imprimir('[turquoise2]Plan: '+rich_escape(plan['ruta_plan'])+'[/]')
    imprimir(f'[gold1]Nuevos: {len(plan["nuevos"])}; conflictos: {len(plan["conflictos"])}; '
            f'bajas: {len(plan["bajas_verificadas"])}[/]')
    if plan['conflictos']:
        imprimir('[red]No se puede publicar: revisar plan y TXT.[/]')
        return
    frase=pedir_texto(f'Para publicar escribe {FRASE}', '', permitir_escape=False)
    from .presentacion import confirma_frase
    if not confirma_frase(frase, FRASE):
        imprimir('[yellow]Publicación cancelada.[/]')
        return
    res=aplicar_publicacion(plan['ruta_plan'],confirmacion=frase)
    imprimir('[green]Publicación completada. '+rich_escape(str(res))+'[/]')
