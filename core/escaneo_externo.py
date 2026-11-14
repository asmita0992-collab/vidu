# -*- coding: utf-8 -*-
"""Procesamiento auxiliar de vídeos ajenos, sin reasignar propietarios.

El equipo ejecutor genera un paquete JSON y una bandeja temporal en SQLite;
puede remitir los resultados a una bandeja separada en Neon bajo autorización.
El dispositivo propietario puede importarlos con respaldo y transacción atómica.
Alternativamente, una publicación directa en Neon requiere autorización
reforzada, identidad y SHA256 idénticos y solo completa huellas faltantes.
La ruta transitoria jamás se guarda como ruta activa del vídeo.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.horario import fmt_ahora_utc6, id_utc6
from core.presentacion import imprimir, pedir_texto, pedir_si_no, pedir_numero, pedir_ruta, confirma_frase
from core.database import conectar_sqlite, info_dispositivo_actual
from core.reporter import Dashboard
from core.inventario import _listar_videos
from core.interfaz import menu_rich

SCHEMA_PAQUETE = 'VIDUP_ANALISIS_EXTERNO_V1'
DIR_RAIZ = Path(__file__).resolve().parent.parent


def _carpeta() -> Path:
    destino = DIR_RAIZ / 'reportes' / 'escaneos_externos'
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def _propietarios(db: str) -> list[tuple[str, str]]:
    conn = conectar_sqlite(db)
    try:
        return [(str(did), str(nombre)) for did, nombre in conn.execute(
            'SELECT dispositivo_id, MAX(dispositivo) FROM videos '
            "WHERE dispositivo_id IS NOT NULL AND TRIM(dispositivo_id)<>'' "
            'GROUP BY dispositivo_id ORDER BY MAX(dispositivo)')]
    finally:
        conn.close()


def seleccionar_propietario(db: str) -> Optional[tuple[str, str]]:
    disponibles = _propietarios(db)
    actual = info_dispositivo_actual()['dispositivo_id']
    disponibles = [(did, nombre) for did, nombre in disponibles if did != actual]
    if not disponibles:
        imprimir('[yellow]La SQLite no contiene registros de otros dispositivos. '
                 'Primero consulta o sincroniza el catálogo, sin publicar cambios.[/]')
        return None
    menu_rich('Dispositivo propietario de los vídeos', [
        (str(i), f'{nombre} · ID {did}') for i, (did, nombre) in enumerate(disponibles, 1)
    ] + [('0','Volver'), ('00','Menú principal')],
        subtitulo='La computadora solamente realizará el procesamiento.')
    respuesta = pedir_texto('Selecciona el propietario', '0', permitir_escape=False)
    if respuesta == '00':
        from core.presentacion import IrAlMenuPrincipal
        raise IrAlMenuPrincipal()
    if respuesta == '0':
        return None
    if not respuesta.isdigit() or not 1 <= int(respuesta) <= len(disponibles):
        imprimir('[yellow]Propietario no seleccionado.[/]')
        return None
    return disponibles[int(respuesta)-1]


def _catalogo_propietario(db: str, propietario_id: str) -> list[dict]:
    conn = conectar_sqlite(db)
    try:
        return [dict(x) for x in conn.execute(
            'SELECT clave_unica, ruta, nombre, tamanio_bytes, hash_archivo, '
            'fecha_escaneo, frame_intervalo FROM videos '
            'WHERE dispositivo_id = ? AND error IS NULL', (propietario_id,))]
    finally:
        conn.close()


def _candidatos_locales(archivos: list[Path], registros: list[dict]) -> tuple[list[Path], list[dict]]:
    """Filtro conservador por nombre/tamaño; los SHA se validan tras analizar."""
    por_nombre = {}
    for fila in registros:
        clave = (str(fila['nombre']).casefold(), int(fila['tamanio_bytes'] or 0))
        por_nombre.setdefault(clave, []).append(fila)
    posibles, excluidos = [], []
    for archivo in archivos:
        try:
            n = int(archivo.stat().st_size)
        except OSError as exc:
            excluidos.append({'archivo':str(archivo),'causa':str(exc)})
            continue
        if (archivo.name.casefold(), n) in por_nombre:
            posibles.append(archivo)
        else:
            excluidos.append({'archivo':str(archivo),
                              'causa':'Nombre y tamaño sin correspondencia en el propietario'})
    return posibles, excluidos


def preparar_paquete(propietario_id: str, propietario: str, resultados: list[dict],
                    procesador: dict) -> dict:
    return {'tipo':SCHEMA_PAQUETE, 'generado_utc6':fmt_ahora_utc6(),
            'propietario_id':propietario_id, 'propietario':propietario,
            'procesador':{'id':procesador['dispositivo_id'],
                           'nombre':procesador['dispositivo']},
            'resultados':resultados}


def _resultado_paquete(fp, registro: dict) -> dict:
    return {
        'clave_unica':registro['clave_unica'],
        'ruta_original':registro['ruta'],
        'hash_archivo':fp.hash_archivo,
        'fecha_registro_origen':registro.get('fecha_escaneo'),
        'nombre':registro['nombre'],
        'tamanio_bytes':fp.tamanio_bytes,
        'frame_intervalo':fp.frame_intervalo,
        'frame_hashes':list(fp.frame_hashes),
        'audio_huella':fp.audio_huella,
        'hash_rapido':fp.hash_rapido,
        'codec_video':fp.codec_video,
        'codec_audio':fp.codec_audio,
        'duracion':fp.duracion,
        'ancho':int(fp.resolucion[0]), 'alto':int(fp.resolucion[1]),
        'fps':fp.fps,
        'metodo_acel':fp.metodo_acel,
    }


def procesar_ajenos(db: str, propietario_id: str, propietario: str,
                    directorio: str, *, recursivo: bool = True,
                    intervalo: float = 10.0, trabajadores: int = 2,
                    aceleracion: str = 'auto', metodo: str = 'estandar') -> dict:
    """Analiza en la PC, exporta JSON y NO modifica ningún catálogo.

    Un registro ajeno tiene que coincidir en nombre, tamaño y SHA256; si varios
    registros coinciden, no se elige uno arbitrariamente.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from core.fingerprint import _trabajo_fingerprint
    from core.flujo_escaneo import resolver_aceleracion_video
    from core.escaneo_codecs import obtener_codecs_videos_pendientes, guardar_reporte_escaneo_previo_codecs

    if not Path(db).is_file():
        raise FileNotFoundError('Selecciona una SQLite existente; no se crea una base para el escaneo externo.')
    registros = _catalogo_propietario(db, propietario_id)
    archivos = _listar_videos(directorio, recursivo=recursivo)
    candidatos, descartados = _candidatos_locales(archivos, registros)
    # Comprobar SHA por vídeo tras el análisis, no publicar rutas temporales.
    indice = {}
    for registro in registros:
        if registro.get('hash_archivo'):
            k = (str(registro['nombre']).casefold(), int(registro['tamanio_bytes'] or 0),
                 str(registro['hash_archivo']).lower())
            indice.setdefault(k, []).append(registro)
    resultados, errores = [], list(descartados)
    if metodo == 'previo':
        previo = obtener_codecs_videos_pendientes(candidatos)
        ruta_json, ruta_txt = guardar_reporte_escaneo_previo_codecs(directorio, previo, len(candidatos))
        return {'previo':len(previo),'json':str(ruta_json),'txt':str(ruta_txt),
                'descartados':len(descartados)}
    if not candidatos:
        return {'analizados':0,'exportados':0,'descartados':len(descartados),'errores':errores}
    dashboard = Dashboard(num_workers=trabajadores, tty=True)
    cancelar = threading.Event()
    dashboard.iniciar(len(candidatos))
    try:
        with ThreadPoolExecutor(max_workers=max(1, trabajadores)) as ejecutor:
            futuros = {}
            for ruta in candidatos:
                acel = resolver_aceleracion_video(ruta, 'codec' if metodo == 'codec' else aceleracion)
                futuro = ejecutor.submit(_trabajo_fingerprint, ruta, intervalo, dashboard, cancelar,
                                         2, acel)
                futuros[futuro] = ruta
            for futuro in as_completed(futuros):
                ruta = futuros[futuro]
                try:
                    _, resultado = futuro.result()
                except Exception as exc:
                    errores.append({'archivo':str(ruta),'causa':str(exc)})
                    dashboard.marcar_error()
                    continue
                if resultado['estado'] != 'ok':
                    errores.append({'archivo':str(ruta),'causa':resultado.get('error','cancelado')})
                    dashboard.marcar_error()
                    continue
                fp = resultado['fingerprint']
                clave = (ruta.name.casefold(), int(fp.tamanio_bytes), str(fp.hash_archivo).lower())
                matches = indice.get(clave, [])
                if len(matches) != 1:
                    errores.append({'archivo':str(ruta),
                           'causa':'No existe un único registro con SHA256 idéntico en el catálogo propietario'})
                    dashboard.marcar_error()
                    continue
                resultados.append(_resultado_paquete(fp, matches[0]))
                dashboard.marcar_completado()
    except KeyboardInterrupt:
        cancelar.set()
        errores.append({'archivo':'(lote)','causa':'Cancelado por el usuario'})
    finally:
        dashboard.detener()
    paquete = preparar_paquete(propietario_id, propietario, resultados, info_dispositivo_actual())
    paquete['errores'] = errores
    destino = _carpeta() / f'paquete_ajeno_{id_utc6()}_{propietario_id}.json'
    destino.write_text(json.dumps(paquete, indent=2, ensure_ascii=False), encoding='utf8')
    # La bandeja es distinta del catálogo activo; no modifica vídeos/frames.
    from core.escaneos_pendientes import guardar_local
    pendientes = guardar_local(db, paquete) if resultados else {'agregados':0}
    return {'analizados':len(candidatos), 'exportados':len(resultados),
            'pendientes_sqlite':pendientes['agregados'],
            'descartados':len(errores), 'archivo':str(destino), 'errores':errores}


def plan_importacion(db: str, paquete: dict, propietario_id: str) -> dict:
    """Plan sin escritura: rechaza propietario ajeno, identidades y SHA cambiados."""
    if paquete.get('tipo') != SCHEMA_PAQUETE or paquete.get('propietario_id') != propietario_id:
        raise ValueError('El paquete no corresponde al dispositivo propietario actual.')
    if not isinstance(paquete.get('resultados'), list):
        raise ValueError('Paquete incompleto.')
    conn = conectar_sqlite(db)
    try:
        resultados = []
        for dato in paquete['resultados']:
            clave = str(dato.get('clave_unica') or '')
            fila = conn.execute('SELECT * FROM videos WHERE clave_unica = ? AND dispositivo_id = ?',
                                (clave, propietario_id)).fetchone()
            if not fila:
                resultados.append({'clave':clave,'accion':'conflicto','motivo':'El registro ya no existe o no pertenece al dispositivo'})
                continue
            if str(fila['hash_archivo'] or '').lower() != str(dato.get('hash_archivo') or '').lower() or \
               str(fila['ruta']) != str(dato.get('ruta_original')) or \
               int(fila['tamanio_bytes'] or 0) != int(dato.get('tamanio_bytes') or 0):
                resultados.append({'clave':clave,'accion':'conflicto','motivo':'Cambió SHA256, ruta o tamaño desde el análisis'})
                continue
            if conn.execute('SELECT 1 FROM videos_eliminados WHERE clave_unica=?',(clave,)).fetchone():
                resultados.append({'clave':clave,'accion':'conflicto','motivo':'Hay marca de eliminación, revisar antes de importar'})
                continue
            frames_actuales = conn.execute('SELECT COUNT(*) FROM frame_hashes WHERE video_id = ?', (fila['id'],)).fetchone()[0]
            nuevos = dato.get('frame_hashes') or []
            if not isinstance(nuevos,list) or len(nuevos)>200000 or any(not isinstance(x,str) or len(x)>1024 for x in nuevos):
                resultados.append({'clave':clave,'accion':'conflicto','motivo':'Huellas visuales inválidas'})
                continue
            if frames_actuales and nuevos:
                existentes = [h[0] for h in conn.execute(
                    'SELECT hash_valor FROM frame_hashes WHERE video_id=? ORDER BY posicion',
                    (fila['id'],))]
                if existentes != nuevos:
                    resultados.append({'clave':clave,'accion':'conflicto',
                                       'motivo':'Huellas existentes diferentes; no sustituir automáticamente'})
                    continue
            if fila['audio_huella'] and dato.get('audio_huella') and \
               fila['audio_huella'] != dato['audio_huella']:
                resultados.append({'clave':clave,'accion':'conflicto',
                                   'motivo':'Huella de audio existente diferente'})
                continue
            frames = (not frames_actuales and bool(nuevos))
            audio = (not fila['audio_huella'] and bool(dato.get('audio_huella')))
            if frames or audio:
                resultados.append({'clave':clave,'accion':'actualizar',
                                   'visual':frames,'audio':audio})
            else:
                resultados.append({'clave':clave,'accion':'sin_cambios'})
        return {'propietario_id':propietario_id, 'videos':resultados,
                'actualizar':sum(x['accion']=='actualizar' for x in resultados),
                'conflictos':sum(x['accion']=='conflicto' for x in resultados)}
    finally:
        conn.close()


def importar_paquete(db: str, ruta_paquete: str, *, confirmar: bool = False) -> dict:
    """Importación en el propietario: respaldo SQLite y cambios atómicos.

    No se aceptan archivos ajenos si el proceso se ejecuta en Windows/Linux
    mientras el propietario es Android; el paquete se lleva al Xiaomi.
    """
    actual = info_dispositivo_actual()['dispositivo_id']
    ruta = Path(ruta_paquete)
    if ruta.stat().st_size > 200*1024*1024:
        raise ValueError('El paquete supera el límite de 200 MiB.')
    paquete = json.loads(ruta.read_text(encoding='utf8'))
    plan = plan_importacion(db, paquete, actual)
    if not confirmar or not plan['actualizar']:
        return {'aplicados':0, 'plan':plan}
    source = conectar_sqlite(db)
    respaldo = _carpeta() / f'antes_importar_{id_utc6()}.db'
    try:
        with sqlite3.connect(str(respaldo)) as copia:
            source.backup(copia)
        source.execute('BEGIN IMMEDIATE')
        # Volver a validar bajo bloqueo. Si hay diferencias respecto del plan,
        # abortar en vez de aplicar un subconjunto inadvertido.
        previsto = {x['clave']:x for x in plan['videos'] if x['accion']=='actualizar'}
        aplicado = 0
        for dato in paquete['resultados']:
            clave = dato['clave_unica']
            if clave not in previsto:
                continue
            fila = source.execute('SELECT * FROM videos WHERE clave_unica=? AND dispositivo_id=?',
                                  (clave,actual)).fetchone()
            if (not fila or str(fila['ruta'])!=str(dato['ruta_original']) or
                str(fila['hash_archivo']).lower()!=str(dato['hash_archivo']).lower() or
                int(fila['tamanio_bytes'])!=int(dato['tamanio_bytes']) or
                source.execute('SELECT 1 FROM videos_eliminados WHERE clave_unica=?',(clave,)).fetchone()):
                raise RuntimeError('La base cambió durante la importación: ' + clave)
            flags = previsto[clave]
            # Revalidar las huellas bajo la transacción, además de la identidad.
            cuenta_frames = source.execute(
                'SELECT COUNT(*) FROM frame_hashes WHERE video_id=?',(fila['id'],)).fetchone()[0]
            if (flags['visual'] and cuenta_frames) or (flags['audio'] and fila['audio_huella']):
                raise RuntimeError('Huellas modificadas concurrentemente: ' + clave)
            if flags['visual']:
                source.executemany('INSERT INTO frame_hashes(video_id,posicion,hash_valor) VALUES(?,?,?)',
                    ((fila['id'],i,h) for i,h in enumerate(dato['frame_hashes'])))
                source.execute('UPDATE videos SET frame_intervalo=? WHERE id=?',
                               (float(dato.get('frame_intervalo') or 10),fila['id']))
            if flags['audio']:
                source.execute('UPDATE videos SET audio_huella=? WHERE id=?',
                               (dato['audio_huella'],fila['id']))
            # El journal de cambios propios se dispara al actualizar vídeo.
            source.execute('UPDATE videos SET fecha_escaneo=? WHERE id=?',
                           (fmt_ahora_utc6(),fila['id']))
            aplicado += 1
        source.commit()
        return {'aplicados':aplicado,'respaldo':str(respaldo),'plan':plan}
    except Exception:
        source.rollback()
        raise
    finally:
        source.close()


def menu_importar_paquete(db: str) -> None:
    ruta = pedir_ruta('Paquete JSON generado por el procesador auxiliar', existente=True)
    plan = importar_paquete(db,ruta,confirmar=False)['plan']
    imprimir(f'[turquoise2]Actualizaciones: {plan["actualizar"]} '
             f'· Conflictos: {plan["conflictos"]}[/]')
    informe = _carpeta()/f'plan_importacion_{id_utc6()}.txt'
    informe.write_text('\n'.join(f'{x["accion"]}: {x["clave"]} - {x.get("motivo", "")}'
                                   for x in plan['videos']), encoding='utf8')
    imprimir(f'[cyan]Informe: {informe}[/]')
    if not plan['actualizar']:
        return
    if not pedir_si_no('¿Aplicar exclusivamente las huellas faltantes en ESTE dispositivo?',False):
        return
    texto = pedir_texto('Escribe IMPORTAR HUELLAS para confirmar','',permitir_escape=False)
    if confirma_frase(texto,'IMPORTAR HUELLAS'):
        res = importar_paquete(db,ruta,confirmar=True)
        imprimir(f'[green]Importadas {res["aplicados"]} actualizaciones. Respaldo: {res["respaldo"]}[/]')
    else:
        imprimir('[yellow]Cancelado; no se modificó SQLite.[/]')


def menu_bandeja_externa(db: str) -> None:
    """Gestión segura de trabajos ajenos sin apropiarse de vídeos de otros equipos."""
    from core.escaneos_pendientes import (
        pendientes_locales, subir_pendientes_neon, obtener_pendientes_neon,
        marcar_neon_aplicados, aplicar_pendientes_en_neon)
    from core.presentacion import RegresarAlMenu, IrAlMenuPrincipal
    from core.interfaz import menu_rich
    actual_id = info_dispositivo_actual()['dispositivo_id']
    while True:
        menu_rich('Bandeja temporal · huellas de otro dispositivo', [
            ('1','Consultar resultados pendientes en esta SQLite'),
            ('2','Publicar pendientes en bandeja separada de Neon (sin tocar catálogo)'),
            ('3','Importar al SQLite propio los análisis recibidos en Neon'),
            ('4','Autorizar desde esta PC completar huellas en Neon (excepcional)'),
            ('5','Importar paquete JSON al dispositivo propietario'),
            ('0','Volver al método de escaneo'),('00','Menú principal')],
            subtitulo='Las huellas pendientes no cuentan como vídeos ni modifican rutas.')
        try:
            op = pedir_texto('Elige operación','0',permitir_escape=False).strip()
        except RegresarAlMenu as e:
            if isinstance(e,IrAlMenuPrincipal):
                raise
            return
        if op == '00':
            raise IrAlMenuPrincipal()
        if op == '0':
            return
        try:
            if op == '1':
                datos = pendientes_locales(db)
                imprimir(f'[turquoise2]Pendientes locales: {len(datos)}[/]')
                for d in datos[:20]:
                    imprimir(f'[gold1]{d["estado"]}[/] · '
                             f'[spring_green2]{d["propietario_id"]}[/] · '
                             f'{d["clave_unica"]}')
                if len(datos)>20:
                    imprimir(f'[grey70]Se muestran 20 de {len(datos)}; consulte el JSON de cada lote.[/]')
            elif op == '2':
                if not pedir_si_no('¿Enviar trabajos pendientes a la bandeja Neon, sin tocar vídeos?',False):
                    continue
                res=subir_pendientes_neon(db)
                imprimir(f'[spring_green2]Enviados: {res["enviados"]}[/] · '
                         f'Ya existentes: {res["ya_existentes"]} · '
                         f'Conflictos: {len(res["conflictos"])}')
                if res['conflictos']:
                    ruta=_carpeta()/f'conflictos_bandeja_{id_utc6()}.txt'
                    ruta.write_text('\n'.join(str(x) for x in res['conflictos']),encoding='utf8')
                    imprimir(f'[yellow]Informe: {ruta}[/]')
            elif op == '3':
                lote=obtener_pendientes_neon(actual_id)
                if not lote['ids']:
                    imprimir('[yellow]No hay análisis pendientes para este dispositivo en Neon.[/]')
                    continue
                paquete=preparar_paquete(actual_id,
                         info_dispositivo_actual()['dispositivo'],
                         lote['resultados'], info_dispositivo_actual())
                paquete['generacion_neon']=lote['generacion']
                destino=_carpeta()/f'descargado_neon_{id_utc6()}.json'
                destino.write_text(json.dumps(paquete,ensure_ascii=False,indent=2),encoding='utf8')
                plan=importar_paquete(db,str(destino),confirmar=False)['plan']
                imprimir(f'[turquoise2]Recibidos {len(lote["ids"])}; '
                         f'Actualizables {plan["actualizar"]}; Conflictos {plan["conflictos"]}[/]')
                ruta_txt=_carpeta()/f'plan_importacion_{id_utc6()}.txt'
                ruta_txt.write_text('\n'.join(
                    f'{x["accion"]}: {x["clave"]} — {x.get("motivo", "")}'
                    for x in plan['videos']),encoding='utf8')
                imprimir(f'[cyan]Plan TXT: {ruta_txt}[/]')
                if not plan['actualizar'] or not pedir_si_no('¿Importar las huellas faltantes en tu SQLite?',False):
                    continue
                frase=pedir_texto('Escribe IMPORTAR HUELLAS','',permitir_escape=False)
                if not confirma_frase(frase,'IMPORTAR HUELLAS'):
                    imprimir('[yellow]No se importó ningún cambio.[/]')
                    continue
                res=importar_paquete(db,str(destino),confirmar=True)
                # Solo confirmar IDs de registros que realmente se aplicaron.
                aplicados={x['clave'] for x in plan['videos'] if x['accion']=='actualizar'}
                ids=[k for k,d in zip(lote['ids'],lote['resultados']) if d['clave_unica'] in aplicados]
                try:
                    marcar_neon_aplicados(ids,actual_id)
                except Exception as e:
                    imprimir('[yellow]Los datos ya se guardaron localmente, pero Neon no '
                             f'pudo registrar el acuse: {e}. Se puede reintentar.[/]')
                imprimir(f'[spring_green2]{res["aplicados"]} análisis importados.[/] '
                         f'Respaldo: {res["respaldo"]}')
            elif op == '4':
                datos=pendientes_locales(db)
                propietarios=sorted({d['propietario_id'] for d in datos
                                     if d['propietario_id'] != actual_id})
                if not propietarios:
                    imprimir('[yellow]No hay paquetes ajenos en la bandeja local.[/]')
                    continue
                for i,p in enumerate(propietarios,1):
                    imprimir(f'[gold1]{i}[/] Propietario: [spring_green2]{p}[/]')
                n=pedir_numero('Seleccionar propietario',1,1,len(propietarios),entero=True)
                propietario=propietarios[int(n)-1]
                # No autorizar trabajos generados por otros procesadores desde esta PC.
                nube=obtener_pendientes_neon(propietario, procesador_id=actual_id)
                if not nube['ids']:
                    imprimir('[yellow]Primero publique los análisis a la bandeja de Neon.[/]')
                    continue
                imprimir('[bold orange1]Operación EXCEPCIONAL: completará únicamente huellas '
                         'faltantes de ese propietario en Neon, sin cambiar ruta, dispositivo o SHA256.[/]')
                imprimir(f'[yellow]Generación: {nube["generacion"]}; vídeos pendientes: '
                         f'{len(nube["ids"])}. Solo funciona sobre coincidencias exactas.[/]')
                if not pedir_si_no('¿Autorizar la aplicación remota con respaldo previo?',False):
                    continue
                frase=pedir_texto(f'Escribe PUBLICAR HUELLAS {propietario} para confirmar',
                                 '',permitir_escape=False)
                if not confirma_frase(frase,f'PUBLICAR HUELLAS {propietario}'):
                    imprimir('[yellow]No se publicó nada.[/]')
                    continue
                res=aplicar_pendientes_en_neon(nube['ids'],propietario,
                    procesador_id=actual_id,confirmar=True)
                imprimir(f'[spring_green2]Completados en Neon: {res["actualizados"]}[/] · '
                         f'Sin cambios: {res["sin_cambios"]} · Respaldo: {res["respaldo"]}')
            elif op == '5':
                menu_importar_paquete(db)
            else:
                imprimir('[yellow]Selecciona una opción válida.[/]')
        except (RegresarAlMenu, KeyboardInterrupt) as e:
            if isinstance(e,IrAlMenuPrincipal):
                raise
            imprimir('[yellow]↩ Operación cancelada sin modificar el catálogo.[/]')
        except Exception as e:
            imprimir(f'[bold red]Operación no completada: {e}[/]')
