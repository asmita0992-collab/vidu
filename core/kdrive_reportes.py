# -*- coding: utf-8 -*-
"""Restituye informes detallados del escaneo remoto en la estructura de VIDUP.

Los TXT de recursos son compatibles con el módulo habitual ``core.recursos``;
el informe de lote detalla tiempos individuales, errores y tiempo total.
"""
from __future__ import annotations
import csv
import json
from pathlib import Path
from typing import Any
from .horario import fmt_ahora_utc6, id_utc6


def guardar_reportes_escaneo_kdrive(resultado: dict, mediciones: list, *,
                                    inicio_fecha: str, carpeta: Path) -> dict:
    carpeta = Path(carpeta)
    identificador=id_utc6()
    for sub in ('recursos/txt','recursos/json','recursos/csv','errores/txt','errores/json',
                'escaneos/txt','escaneos/json'):
        (carpeta / sub).mkdir(parents=True, exist_ok=True)
    tiempos=[]
    for m in sorted(mediciones, key=lambda m: m.inicio_epoch):
        tiempos.append(dict(ruta=m.ruta,nombre=m.nombre, inicio=m.inicio_texto,
                            fin=m.fin_texto, segundos=round(float(m.duracion_seg),3),
                            completado=bool(m.completado),error=m.error,
                            codec_video=m.codec_video,codec_audio=m.codec_audio,
                            frames=m.frames_muestreados,metodo_acel=m.metodo_acel,
                            tamanio_bytes=m.tamanio_bytes))
    error_data={'fecha':fmt_ahora_utc6(),'origen':'kdrive (nube)',
                'cantidad':len(resultado.get('errores') or []),'errores':resultado.get('errores') or []}
    resumen={'version_informe':2,'tipo':'escaneo_kdrive','inicio':inicio_fecha,
             'fin':fmt_ahora_utc6(),'total_segundos':resultado.get('segundos'),
             'procesados':resultado.get('procesados',0),
             'errores':len(resultado.get('errores') or []),
             'completados_anteriores':resultado.get('ya_completos',0),
             'total_candidatos':resultado.get('coincidencias_unicas',0),
             'tiempos_por_video':tiempos,'errores_detallados':error_data['errores'],
             'destino_sqlite':resultado.get('destino'),'catalogo_sha':resultado.get('origen'),
             'montaje':resultado.get('montaje'),'neon':'NO SE HA MODIFICADO'}
    recursos_txt=carpeta/'recursos'/'txt'/f'recursos_kdrive_{identificador}.txt'
    if mediciones:
        from .recursos import guardar_reporte_txt
        guardar_reporte_txt(mediciones,str(recursos_txt))
        # Extender sin sustituir el formato de recursos tradicional.
        with recursos_txt.open('a',encoding='utf-8') as fh:
            fh.write(f'\nRESUMEN DEL LOTE KDRIVE\nTiempo total del lote: {resultado.get("segundos",0)} s\n')
    else:
        recursos_txt.write_text('VIDUP · RECURSOS ESCANEO KDRIVE\n'
                                 f'Inicio: {inicio_fecha}\nFin: {resumen["fin"]}\n'
                                 f'Tiempo total lote: {resultado.get("segundos",0)} s\n'
                                 'No hubo mediciones individuales disponibles.\n',encoding='utf-8')
    recursos_json=carpeta/'recursos'/'json'/f'recursos_kdrive_{identificador}.json'
    recursos_json.write_text(json.dumps(resumen,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    recursos_csv=carpeta/'recursos'/'csv'/f'recursos_kdrive_{identificador}.csv'
    with recursos_csv.open('w',encoding='utf-8-sig',newline='') as f:
        keys=('ruta','nombre','inicio','fin','segundos','completado','error','codec_video',
              'codec_audio','frames','metodo_acel','tamanio_bytes')
        escritor=csv.DictWriter(f,fieldnames=keys)
        escritor.writeheader();escritor.writerows(tiempos)
    errores_txt=carpeta/'errores'/'txt'/f'errores_kdrive_{identificador}.txt'
    errores_txt.write_text('VIDUP · ERRORES ESCANEO KDRIVE\n'
                            f'Total errores: {error_data["cantidad"]}\n\n' +
                            '\n'.join(f'{e.get("ruta")}: {e.get("motivo")}\n{e.get("detalle","")}'
                                      for e in error_data['errores']) + '\n',encoding='utf-8')
    errores_json=carpeta/'errores'/'json'/f'errores_kdrive_{identificador}.json'
    errores_json.write_text(json.dumps(error_data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    resumen_txt=carpeta/'escaneos'/'txt'/f'escaneo_kdrive_{identificador}.txt'
    resumen_txt.write_text('VIDUP · ESCANEO DE KDRIVE\n'
        f'Inicio: {inicio_fecha}\nFin: {resumen["fin"]}\n'
        f'Duración total: {resultado.get("segundos",0)} s\n'
        f'Procesados: {resumen["procesados"]}\nErrores: {resumen["errores"]}\n'
        f'Ya completos: {resumen["completados_anteriores"]}\n'
        'TIEMPOS POR VIDEO\n' +
        '\n'.join(f'{m["nombre"]} | {m["segundos"]} s | {m["inicio"]} - {m["fin"]} | '
                  f'{"OK" if m["completado"] else "ERROR"}' for m in tiempos) +
        f'\nInforme de recursos: {recursos_txt}\nInforme de errores: {errores_txt}\n', encoding='utf-8')
    resumen_json=carpeta/'escaneos'/'json'/f'escaneo_kdrive_{identificador}.json'
    resumen_json.write_text(json.dumps(resumen,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return dict(recursos_txt=str(recursos_txt),recursos_json=str(recursos_json),
                recursos_csv=str(recursos_csv),errores_txt=str(errores_txt),
                errores_json=str(errores_json),escaneo_txt=str(resumen_txt),
                escaneo_json=str(resumen_json))
