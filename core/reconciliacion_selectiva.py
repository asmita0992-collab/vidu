# -*- coding: utf-8 -*-
"""Reconciliación selectiva segura de Neon hacia una SQLite heredada.

No convierte la existencia en Neon en prueba de que un vídeo local esté obsoleto.
Solo completa valores faltantes de registros que tienen exactamente la misma
clave, propietario, ruta, tamaño y SHA256. Nunca borra, sustituye ni publica
registros de otro dispositivo, y no modifica archivos multimedia físicos.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core.horario import fmt_ahora_utc6, id_utc6

CAMPOS_COMPLETABLES = (
    'hash_rapido','duracion','ancho','alto','fps','codec_video','codec_audio',
    'bitrate_video_kbps','dispositivo_origen','dispositivo_origen_id',
)
NUMERICOS = {'duracion','ancho','alto','fps','bitrate_video_kbps'}
IDENTIDAD = ('clave_unica','ruta','dispositivo_id','hash_archivo','tamanio_bytes')


def _vacio(valor, campo: str) -> bool:
    if valor is None or valor == '':
        return True
    return campo in NUMERICOS and float(valor or 0) == 0


def construir_plan(local: dict, neon: dict, hashes_local: dict,
                   hashes_neon: dict, marcas_local: dict, marcas_neon: dict) -> dict:
    """Prepara acciones SIN escritura; incluye las razones de cada bloqueo."""
    cambios, bloqueados = [], []
    for clave in sorted(set(local)|set(neon)):
        a, b = local.get(clave), neon.get(clave)
        if not a or not b:
            bloqueados.append({'clave':clave, 'motivo':(
                'Registro exclusivo de SQLite: no se borra sin marca verificada.'
                if a else 'Registro exclusivo de Neon: no se reactiva sin comprobar su estado.')})
            continue
        if any(str(a.get(c) or '') != str(b.get(c) or '') for c in IDENTIDAD):
            bloqueados.append({'clave':clave,'motivo':'Identidad, ruta, SHA256 o tamaño diferentes.'})
            continue
        if clave in marcas_local or clave in marcas_neon:
            if marcas_local.get(clave) != marcas_neon.get(clave):
                bloqueados.append({'clave':clave,'motivo':'Marcas de eliminación distintas; requiere plan propio.'})
                continue
        nuevos={}
        incompatibles=[]
        for campo in CAMPOS_COMPLETABLES:
            lv, nv=a.get(campo),b.get(campo)
            if _vacio(lv,campo) and not _vacio(nv,campo):
                nuevos[campo]=nv
            elif not _vacio(lv,campo) and not _vacio(nv,campo) and lv!=nv:
                # FPS 29.9700299700 y 29.97 son representaciones equivalentes.
                if campo=='fps' and abs(float(lv)-float(nv))<0.001:
                    continue
                incompatibles.append(campo)
        h_a=hashes_local.get(int(a['id']), (0,''))
        h_b=hashes_neon.get(int(b['id']), (0,''))
        n_a,n_b=int(h_a[0]),int(h_b[0])
        frames=(n_a==0 and n_b>0)
        audio=(_vacio(a.get('audio_huella'),'audio_huella') and
               not _vacio(b.get('audio_huella'),'audio_huella'))
        if n_a and n_b and h_a!=h_b:
            incompatibles.append('frame_hashes')
        if a.get('audio_huella') and b.get('audio_huella') and a['audio_huella']!=b['audio_huella']:
            incompatibles.append('audio_huella')
        # Los campos en disputa no se sobrescriben, pero otros campos faltantes
        # sí pueden completarse, siempre que la identidad sea idéntica.
        if nuevos or frames or audio:
            cambios.append({'clave':clave,'campos':nuevos,'frames':frames,
                            'audio':audio,'frames_neon':n_b,
                            'neon_id':int(b['id']),'sqlite_id':int(a['id']),
                            'intervalo_neon':b.get('frame_intervalo'),
                            'audio_esperado':b.get('audio_huella'),
                            'revision_neon':b.get('fecha_escaneo'),
                            'revision_local':a.get('fecha_escaneo'),
                            'incompatibles':incompatibles})
        elif incompatibles:
            bloqueados.append({'clave':clave,'motivo':'Valores incompatibles: '+', '.join(incompatibles)})
    marcas_diferentes=sorted(k for k in set(marcas_local)|set(marcas_neon)
                               if marcas_local.get(k)!=marcas_neon.get(k))
    return {'tipo':'VIDUP_PLAN_SELECTIVO_2.6','generado':fmt_ahora_utc6(),
            'local_solo':len(set(local)-set(neon)),
            'neon_solo':len(set(neon)-set(local)),
            'marcas_diferentes':marcas_diferentes,
            'actualizaciones':cambios,'bloqueados':bloqueados,
            'aviso':'Solo completará datos faltantes de vídeos idénticos. '
                    'No sustituye catálogos, no elimina ni publica registros.'}


def guardar_plan(plan: dict, directorio: Path) -> tuple[Path,Path]:
    destino=Path(directorio)
    json_dir=destino/'json'; txt_dir=destino/'txt'
    json_dir.mkdir(parents=True,exist_ok=True); txt_dir.mkdir(parents=True,exist_ok=True)
    nombre='conciliacion_selectiva_'+id_utc6()
    ruta_json=json_dir/(nombre+'.json'); ruta_txt=txt_dir/(nombre+'.txt')
    ruta_json.write_text(json.dumps(plan,ensure_ascii=False,indent=2,default=str),encoding='utf8')
    lineas=['VIDUP 2.6 — PROPUESTA DE RECONCILIACIÓN SELECTIVA', '='*66,
            'Generado: '+plan['generado'], '',
            f'Vídeos que solo están en SQLite: {plan["local_solo"]}',
            f'Vídeos que solo están en Neon: {plan["neon_solo"]}',
            f'Marcas de eliminación distintas: {len(plan["marcas_diferentes"])}',
            f'Registros con datos completables: {len(plan["actualizaciones"])}',
            f'Casos bloqueados para revisión: {len(plan["bloqueados"])}','',
            plan['aviso'],'', 'DATOS QUE PUEDEN COMPLETARSE', '-'*50]
    for x in plan['actualizaciones']:
        piezas=[*x['campos'].keys()]
        if x['frames']:
            piezas.append(f'{x["frames_neon"]} huellas visuales')
        if x['audio']:
            piezas.append('huella de audio')
        lineas.append(x['clave']+'\n  Se completará: '+', '.join(piezas))
        if x['incompatibles']:
            lineas.append('  Sin modificar (conflictos): '+', '.join(x['incompatibles']))
    lineas.extend(['','REGISTROS BLOQUEADOS', '-'*50])
    for x in plan['bloqueados']:
        lineas.append(x['clave']+'\n  Motivo: '+x['motivo'])
    lineas.extend(['','DIFERENCIAS QUE PERMANECERÁN', '-'*50,
                   'Las marcas de eliminación y los registros exclusivos no se modifican.',
                   'Una generación anterior seguirá bloqueada para publicar hasta una conciliación completa.'])
    ruta_txt.write_text('\n'.join(lineas)+'\n',encoding='utf8')
    return ruta_json,ruta_txt


def aplicar_plan(conn_sqlite, conn_neon, plan: dict, ruta_sqlite: str,
                reportes: Path) -> dict:
    """Hace backup antes de completar campos/frames sin alterar claves.

    Verifica cada identidad en ambas bases dentro de la transacción. Si falla
    cualquier paso, ejecuta rollback completo en SQLite.
    """
    pendientes=plan['actualizaciones']
    if not pendientes:
        return {'aplicados':0,'respaldo':None}
    ruta=Path(ruta_sqlite)
    if not ruta.is_file():
        raise FileNotFoundError('La SQLite original no existe; no se crea una vacía.')
    destino=Path(reportes)/'respaldos'/'db'
    destino.mkdir(parents=True,exist_ok=True)
    backup=destino/('sqlite_antes_conciliacion_'+id_utc6()+'.db')
    # Snapshot con sqlite3.backup; no basta copiar un fichero con WAL activo.
    with sqlite3.connect(str(backup)) as copia:
        conn_sqlite.backup(copia)
    with sqlite3.connect(str(backup)) as prueba:
        if prueba.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Falló la verificación del respaldo; cancelado.')
    from core.control_sincronizacion import suprimir_eventos
    conn_sqlite.execute('BEGIN IMMEDIATE')
    try:
        suprimir_eventos(conn_sqlite, True)
        for item in pendientes:
            k=item['clave']
            fila=conn_sqlite.execute('SELECT * FROM videos WHERE clave_unica=?',(k,)).fetchone()
            if not fila or int(fila['id'])!=item['sqlite_id']:
                raise RuntimeError('Cambió el registro SQLite durante el plan: '+k)
            with conn_neon.cursor() as cur:
                cur.execute('''SELECT dispositivo_id,ruta,hash_archivo,tamanio_bytes,
                               audio_huella FROM videos WHERE clave_unica=%s''',(k,))
                rem=cur.fetchone()
                if not rem or any(str(fila[c] or '') != str(v or '') for c,v in zip(
                    ('dispositivo_id','ruta','hash_archivo','tamanio_bytes'),rem[:4])):
                    raise RuntimeError('Cambió el registro Neon durante el plan: '+k)
                n_frames=0
                if item['frames']:
                    cur.execute('SELECT posicion,hash_valor FROM frame_hashes WHERE video_id=%s '
                                'ORDER BY posicion',(item['neon_id'],))
                    frames=cur.fetchall()
                    n_frames=len(frames)
                    if n_frames!=item['frames_neon']:
                        raise RuntimeError('Cambió el conjunto de huellas de Neon: '+k)
                if item['audio'] and rem[4] != item['audio_esperado']:
                    raise RuntimeError('Cambió la huella de audio de Neon: '+k)
                if item['campos']:
                    cur.execute('SELECT '+', '.join(item['campos'])+' FROM videos '
                                'WHERE clave_unica=%s',(k,))
                    valores=cur.fetchone()
                    if not valores or any(v!=item['campos'][campo] for campo,v in zip(item['campos'],valores)):
                        raise RuntimeError('Cambiaron los metadatos de Neon: '+k)
            if item['frames']:
                n_local=conn_sqlite.execute('SELECT COUNT(*) FROM frame_hashes WHERE video_id=?',
                                            (fila['id'],)).fetchone()[0]
                if n_local:
                    raise RuntimeError('Ya hay nuevas huellas en SQLite: '+k)
                conn_sqlite.executemany('INSERT INTO frame_hashes(video_id,posicion,hash_valor) VALUES(?,?,?)',
                                        ((fila['id'],pos,h) for pos,h in frames))
                conn_sqlite.execute('UPDATE videos SET frame_intervalo=? WHERE id=?',
                                    (float(item.get('intervalo_neon') or fila['frame_intervalo']),fila['id']))
            asignaciones={}
            for campo,valor in item['campos'].items():
                if not _vacio(fila[campo],campo):
                    raise RuntimeError('Campo completado concurrentemente en SQLite: '+k+'/'+campo)
                asignaciones[campo]=valor
            if item['audio']:
                if fila['audio_huella']:
                    raise RuntimeError('Audio registrado concurrentemente: '+k)
                asignaciones['audio_huella']=rem[4]
            if asignaciones:
                sql='UPDATE videos SET '+', '.join(c+'=?' for c in asignaciones)+' WHERE id=?'
                conn_sqlite.execute(sql,(*asignaciones.values(), fila['id']))
        suprimir_eventos(conn_sqlite,False)
        conn_sqlite.commit()
        return {'aplicados':len(pendientes),'respaldo':str(backup)}
    except BaseException:
        conn_sqlite.rollback()
        raise
