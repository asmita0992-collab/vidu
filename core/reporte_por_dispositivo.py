"""Informe de duplicados por dispositivo a partir de comparacion_*.json.

Solo duplicado_exacto/duplicado_visual se consideran duplicados confirmados.
Los hallazgos parciales o posibles se muestran aparte, sin promoverlos a duplicados.
"""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ZONA = timezone(timedelta(hours=-6))
CONFIRMADOS = {'duplicado_exacto', 'duplicado_visual'}

def _identidad(datos):
    return (str(datos.get('dispositivo_id') or 'SIN_ID').strip(), str(datos.get('dispositivo') or 'Dispositivo desconocido').strip())

def _video(info):
    return {'ruta': str(info.get('ruta') or ''), 'nombre': str(info.get('nombre') or Path(str(info.get('ruta') or '')).name),
            'dispositivo': _identidad(info)[1], 'dispositivo_id': _identidad(info)[0],
            'sha256': str(info.get('hash_archivo') or '')}

def generar(ruta_entrada, directorio):
    origen = Path(ruta_entrada)
    datos = json.loads(origen.read_text(encoding='utf-8-sig'))
    if not isinstance(datos, dict) or not isinstance(datos.get('resultados'), list):
        raise ValueError('El archivo no tiene el formato comparacion_*.json de VIDUP.')
    cobertura = (datos.get('metadatos') or {}).get('comparativas') or {}
    inventario = cobertura.get('por_video') or []
    inventario_completo = bool(inventario) and all(isinstance(v, dict) and v.get('ruta') and v.get('dispositivo_id') and v.get('dispositivo') for v in inventario)
    # No inferir que un archivo no tiene duplicado si no consta en el inventario.
    nodos = {}
    enlaces = defaultdict(dict)
    avisos = defaultdict(list)
    for entrada in inventario:
        if isinstance(entrada, dict) and entrada.get('ruta') and entrada.get('dispositivo_id'):
            v = _video(entrada)
            nodos[(v['dispositivo_id'], v['ruta'])] = v
    for hallazgo in datos['resultados']:
        if not isinstance(hallazgo, dict):
            continue
        a, b = hallazgo.get('info_a') or {}, hallazgo.get('info_b') or {}
        # Los JSON modernos guardan datos de ambos vídeos en info_a/info_b.
        a = {**a, 'ruta': a.get('ruta') or hallazgo.get('ruta_a'), 'nombre': a.get('nombre') or hallazgo.get('nombre_a')}
        b = {**b, 'ruta': b.get('ruta') or hallazgo.get('ruta_b'), 'nombre': b.get('nombre') or hallazgo.get('nombre_b')}
        if not a.get('dispositivo_id') or not b.get('dispositivo_id'):
            continue
        va, vb = _video(a), _video(b)
        ka, kb = (va['dispositivo_id'], va['ruta']), (vb['dispositivo_id'], vb['ruta'])
        nodos.setdefault(ka, va)
        nodos.setdefault(kb, vb)
        tipo = str(hallazgo.get('tipo') or '')
        if tipo in CONFIRMADOS:
            enlaces[ka][kb] = {'video': vb, 'tipo': tipo, 'confianza': hallazgo.get('confianza')}
            enlaces[kb][ka] = {'video': va, 'tipo': tipo, 'confianza': hallazgo.get('confianza')}
        else:
            avisos[ka].append({'video': vb, 'tipo': tipo})
            avisos[kb].append({'video': va, 'tipo': tipo})
    agrupados = defaultdict(lambda: {'nombre': '', 'id': '', 'duplicados': [], 'sin_duplicado': [], 'otros_hallazgos': []})
    for k, v in sorted(nodos.items(), key=lambda kv: (kv[1]['dispositivo'].casefold(), kv[1]['ruta'].casefold())):
        g = agrupados[v['dispositivo_id']]
        g['nombre'], g['id'] = v['dispositivo'], v['dispositivo_id']
        if enlaces.get(k):
            g['duplicados'].append({**v, 'copias': sorted(enlaces[k].values(), key=lambda x: (x['video']['dispositivo'], x['video']['ruta']))})
        elif avisos.get(k):
            g['otros_hallazgos'].append({**v, 'hallazgos': avisos[k]})
        elif inventario_completo:
            g['sin_duplicado'].append(v)
    # Indicar cobertura y no emitir un listado de «sin duplicado» si faltan identidades o inventario.
    total_reportado = int(datos.get('total_videos') or 0)
    if total_reportado and len(nodos) < total_reportado:
        inventario_completo = False
    if not inventario_completo:
        for g in agrupados.values():
            g['sin_duplicado'] = []
    salida = {
        'generado': datetime.now(ZONA).isoformat(), 'archivo_origen': str(origen),
        'inventario_completo': inventario_completo,
        'advertencia': '' if inventario_completo else 'No es posible determinar todos los vídeos sin duplicado: el JSON no contiene un inventario completo con IDs de dispositivo.',
        'criterio': 'Solo duplicado_exacto y duplicado_visual se clasifican como duplicados; fragmentos y casos posibles se muestran por separado.',
        'dispositivos': sorted(agrupados.values(), key=lambda d: (d['nombre'].casefold(), d['id'])),
    }
    stamp = datetime.now(ZONA).strftime('%Y%m%d_%H%M%S')
    carpeta = Path(directorio)
    j = carpeta / 'json' / f'duplicados_por_dispositivo_{stamp}.json'
    t = carpeta / 'txt' / f'duplicados_por_dispositivo_{stamp}.txt'
    j.parent.mkdir(parents=True, exist_ok=True); t.parent.mkdir(parents=True, exist_ok=True)
    lineas = ['DUPLICADOS POR DISPOSITIVO', f'Fuente: {origen}', f'Fecha: {salida["generado"]}', salida['criterio'], '']
    if salida['advertencia']:
        lineas += ['ADVERTENCIA: ' + salida['advertencia'], '']
    for disp in salida['dispositivos']:
        lineas += ['=' * 75, f'Dispositivo: {disp["nombre"]}', f'ID dispositivo: {disp["id"]}',
                   f'Vídeos con duplicado: {len(disp["duplicados"])}',
                   f'Vídeos sin duplicado: {len(disp["sin_duplicado"])}' if inventario_completo else 'Vídeos sin duplicado: NO DETERMINABLE',
                   f'Vídeos con hallazgos no concluyentes: {len(disp["otros_hallazgos"])}', '', 'VÍDEOS DUPLICADOS']
        if not disp['duplicados']: lineas.append('  Ninguno identificado')
        for v in disp['duplicados']:
            lineas.append('  ' + v['ruta'])
            for copia in v['copias']:
                lineas.append(f'    Duplicado en: {copia["video"]["dispositivo"]} (ID {copia["video"]["dispositivo_id"]})')
                lineas.append('      ' + copia['video']['ruta'])
                lineas.append('      Tipo: ' + copia['tipo'])
        lineas += ['', 'VÍDEOS SIN DUPLICADO CONFIRMADO']
        if not inventario_completo: lineas.append('  No determinable con este JSON antiguo o incompleto')
        elif not disp['sin_duplicado']: lineas.append('  Ninguno')
        else: lineas.extend('  ' + v['ruta'] for v in disp['sin_duplicado'])
        lineas += ['', 'OTROS HALLAZGOS (FRAGMENTOS / POSIBLES COINCIDENCIAS)']
        if not disp['otros_hallazgos']: lineas.append('  Ninguno')
        for v in disp['otros_hallazgos']:
            lineas.append('  ' + v['ruta'])
            for h in v['hallazgos']:
                lineas.append(f'    {h["tipo"]}: {h["video"]["dispositivo"]} | {h["video"]["ruta"]}')
        lineas.append('')
    j.write_text(json.dumps(salida, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    t.write_text('\n'.join(lineas) + '\n', encoding='utf-8')
    return {'json': str(j), 'txt': str(t), 'datos': salida}
