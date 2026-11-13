# -*- coding: utf-8 -*-
"""Auditoría legible de catálogos VIDUP, sin modificar datos ni leer vídeos.

Genera informes JSON (estructurados) y TXT (explicativos) a partir de las
filas y resúmenes de huellas ya leídos de SQLite y Neon. En particular,
identifica análisis visuales/audio ausentes y conflictos reales de valores.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

# Datos masivos o de valor sensible: no copiar huellas enteras a informes TXT.
CAMPOS_HUELLA = {'hash_archivo', 'hash_rapido', 'audio_huella'}
ETIQUETAS = {
    'hash_archivo': 'SHA256 del archivo',
    'hash_rapido': 'Huella rápida',
    'audio_huella': 'Huella de audio',
    'fecha_escaneo': 'Fecha de escaneo',
    'ruta': 'Ruta',
    'tamanio_bytes': 'Tamaño en bytes',
    'duracion': 'Duración',
    'codec_video': 'Códec de vídeo',
    'codec_audio': 'Códec de audio',
    'dispositivo_id': 'ID de dispositivo',
    'dispositivo': 'Dispositivo',
    'error': 'Error de escaneo',
}


def _ausente(valor: Any) -> bool:
    return valor is None or valor == ''


def _valor_legible(campo: str, valor: Any) -> str:
    """Un resumen evita exponer huellas enteras en los reportes."""
    if _ausente(valor):
        return 'AUSENTE'
    if campo in CAMPOS_HUELLA:
        texto = str(valor)
        return f'presente ({len(texto)} caracteres, inicio {texto[:12]})'
    return str(valor)


def _resumen_registros(videos: Mapping[str, dict], frames: Mapping[int, tuple]) -> dict:
    cuentas = [frames.get(int(v['id']), (0, ''))[0] for v in videos.values()]
    return {
        'videos': len(videos),
        'con_sha256': sum(not _ausente(v.get('hash_archivo')) for v in videos.values()),
        'con_audio': sum(not _ausente(v.get('audio_huella')) for v in videos.values()),
        'con_visual': sum(c > 0 for c in cuentas),
        'huellas_visuales_total': sum(cuentas),
    }


def construir_auditoria(*, local: Mapping[str, dict], neon: Mapping[str, dict],
                        frames_local: Mapping[int, tuple],
                        frames_neon: Mapping[int, tuple],
                        marcas_local: Mapping[str, str],
                        marcas_neon: Mapping[str, str],
                        columnas: tuple[str, ...],
                        generacion: str, ruta_sqlite: str) -> dict:
    """Compara la identidad completa, incluyendo huellas visuales por vídeo."""
    todos = sorted(set(local) | set(neon))
    detalles = []
    categorias = {'solo_sqlite': 0, 'solo_neon': 0, 'datos_diferentes': 0,
                  'datos_faltantes_en_sqlite': 0, 'datos_faltantes_en_neon': 0,
                  'conflictos_de_valores': 0}
    for clave in todos:
        a, b = local.get(clave), neon.get(clave)
        if a is None or b is None:
            estado = 'solo_neon' if a is None else 'solo_sqlite'
            categorias[estado] += 1
            detalles.append({'clave': clave, 'tipo': estado, 'diferencias': [],
                             'explicacion': ('Solo está en Neon. Adoptar Neon lo añadirá a SQLite.' if a is None
                                             else 'Solo está en SQLite. Adoptar Neon retirará este registro del catálogo local; NO borrará el archivo físico.')})
            continue
        diferencias = []
        for campo in columnas:
            av, bv = a.get(campo), b.get(campo)
            if av == bv:
                continue
            if _ausente(av) and not _ausente(bv):
                tipo = 'faltante_sqlite'
            elif _ausente(bv) and not _ausente(av):
                tipo = 'faltante_neon'
            else:
                tipo = 'valores_distintos'
            diferencias.append({'campo': campo, 'etiqueta': ETIQUETAS.get(campo, campo),
                                'tipo': tipo, 'sqlite': _valor_legible(campo, av),
                                'neon': _valor_legible(campo, bv)})
        fl = frames_local.get(int(a['id']), (0, ''))
        fn = frames_neon.get(int(b['id']), (0, ''))
        if fl != fn:
            nl, nn = fl[0], fn[0]
            tipo = ('faltante_sqlite' if nl == 0 and nn > 0 else
                    'faltante_neon' if nn == 0 and nl > 0 else 'valores_distintos')
            diferencias.append({'campo': 'frame_hashes', 'etiqueta': 'Huellas visuales',
                                'tipo': tipo, 'sqlite': f'{nl} fotogramas',
                                'neon': f'{nn} fotogramas',
                                'detalle': ('Difieren las huellas, aunque el total sea igual.'
                                            if nl == nn else 'Cantidad diferente de huellas.')})
        if not diferencias:
            continue
        categorias['datos_diferentes'] += 1
        tipos = {x['tipo'] for x in diferencias}
        for nombre, tipo in [('datos_faltantes_en_sqlite', 'faltante_sqlite'),
                             ('datos_faltantes_en_neon', 'faltante_neon'),
                             ('conflictos_de_valores', 'valores_distintos')]:
            if tipo in tipos:
                categorias[nombre] += 1
        if tipos == {'faltante_sqlite'}:
            explicacion = ('SQLite tiene datos incompletos. Neon contiene estos análisis; '
                           'adoptar Neon completaría los campos indicados.')
        elif tipos == {'faltante_neon'}:
            explicacion = ('Neon tiene datos incompletos. Adoptar Neon sustituiría '
                           'información disponible en SQLite; revisar antes.')
        else:
            explicacion = ('Hay valores distintos o diferencias de ambas direcciones. '
                           'Revisar antes de adoptar Neon: la adopción sustituye el catálogo local.')
        detalles.append({'clave': clave, 'tipo': 'datos_diferentes',
                         'diferencias': diferencias, 'explicacion': explicacion})
    diferencias_marcas = []
    for clave in sorted(set(marcas_local) | set(marcas_neon)):
        a, b = marcas_local.get(clave), marcas_neon.get(clave)
        if a != b:
            diferencias_marcas.append({
                'clave': clave, 'fecha_sqlite': a, 'fecha_neon': b,
                'tipo': 'solo_sqlite' if b is None else
                        'solo_neon' if a is None else 'fechas_diferentes',
            })
    resultado = {
        'formato': 'vidup-auditoria-catalogo-v2',
        'fecha': datetime.now().isoformat(timespec='seconds'),
        'generacion_neon': generacion,
        'sqlite': ruta_sqlite,
        'resumen_sqlite': _resumen_registros(local, frames_local),
        'resumen_neon': _resumen_registros(neon, frames_neon),
        'categorias': categorias,
        'diferencias': detalles,
        'marcas_diferentes': diferencias_marcas,
        # Compatibilidad con el formato de auditoría anterior.
        'solo_sqlite': [x['clave'] for x in detalles if x['tipo'] == 'solo_sqlite'],
        'solo_neon': [x['clave'] for x in detalles if x['tipo'] == 'solo_neon'],
        'versiones_distintas': [x['clave'] for x in detalles if x['tipo'] == 'datos_diferentes'],
        'marcas_solo_sqlite': [x['clave'] for x in diferencias_marcas if x['tipo'] == 'solo_sqlite'],
        'marcas_solo_neon': [x['clave'] for x in diferencias_marcas if x['tipo'] == 'solo_neon'],
        'advertencia': ('Este reporte no modifica las bases ni verifica archivos físicos. '
                        'Adoptar Neon sustituye el catálogo SQLite completo, no los vídeos.'),
    }
    return resultado


def auditoria_a_txt(datos: dict) -> str:
    """Traduce el reporte técnico a decisiones comprensibles para el usuario."""
    a, b, cont = datos['resumen_sqlite'], datos['resumen_neon'], datos['categorias']
    lineas = [
        'VIDUP — DIAGNÓSTICO DE SINCRONIZACIÓN Y ADOPCIÓN DE NEON',
        '=' * 68,
        f"Generado: {datos['fecha']}",
        f"SQLite: {datos['sqlite']}",
        f"Generación de Neon: {datos['generacion_neon']}",
        '', '1. ESTADÍSTICAS COMPARADAS',
        'Métrica                         SQLite         Neon',
        '-' * 58,
    ]
    for clave, nombre in [('videos', 'Vídeos activos'), ('con_sha256', 'Vídeos con SHA256'),
                          ('con_visual', 'Vídeos con huella visual'),
                          ('con_audio', 'Vídeos con huella de audio'),
                          ('huellas_visuales_total', 'Fotogramas con huella')]:
        lineas.append(f'{nombre:<30} {a[clave]:>9}    {b[clave]:>9}')
    lineas += [
        '', '2. RESUMEN DE DIFERENCIAS',
        f"Vídeos exclusivos de SQLite: {cont['solo_sqlite']}",
        f"Vídeos exclusivos de Neon: {cont['solo_neon']}",
        f"Registros con información diferente: {cont['datos_diferentes']}",
        f"Registros con datos faltantes en SQLite: {cont['datos_faltantes_en_sqlite']}",
        f"Registros con datos faltantes en Neon: {cont['datos_faltantes_en_neon']}",
        f"Registros con valores distintos: {cont['conflictos_de_valores']}",
        f"Marcas de eliminación distintas: {len(datos['marcas_diferentes'])}",
        '', '3. INTERPRETACIÓN',
    ]
    if not datos['diferencias'] and not datos['marcas_diferentes']:
        lineas.append('Los catálogos comparados coinciden en los campos y huellas evaluados.')
    elif (cont['solo_sqlite'] == cont['solo_neon'] ==
          cont['datos_faltantes_en_neon'] == cont['conflictos_de_valores'] == 0 and
          not datos['marcas_diferentes']):
        lineas.append('Las diferencias consisten en datos disponibles en Neon que faltan en SQLite.')
        lineas.append('Al adoptar Neon se recuperarían esos análisis en el catálogo local.')
    else:
        lineas.append('Existen diferencias que requieren revisar el detalle antes de adoptar Neon.')
    lineas.append('El informe no vuelve a calcular SHA256 ni lee archivos de vídeo.')
    lineas += ['', '4. DETALLE POR VÍDEO', '-' * 68]
    for idx, fila in enumerate(datos['diferencias'], 1):
        lineas += [f'{idx}. {fila["clave"]}',
                   f'   Diagnóstico: {fila["explicacion"]}']
        for d in fila['diferencias']:
            lineas += [f'   - {d["etiqueta"]}:',
                       f'       SQLite: {d["sqlite"]}',
                       f'       Neon:   {d["neon"]}']
            if d.get('detalle'):
                lineas.append(f'       Nota: {d["detalle"]}')
        lineas.append('')
    if not datos['diferencias']:
        lineas.append('No existen vídeos con diferencias.')
    lineas += ['', '5. MARCAS DE ELIMINACIÓN']
    if datos['marcas_diferentes']:
        for d in datos['marcas_diferentes']:
            lineas.append(f"- {d['clave']}: SQLite={d['fecha_sqlite'] or 'AUSENTE'}; "
                          f"Neon={d['fecha_neon'] or 'AUSENTE'}")
    else:
        lineas.append('Las marcas de eliminación coinciden.')
    marc_solo_neon = sum(m['tipo'] == 'solo_neon' for m in datos['marcas_diferentes'])
    marc_solo_sqlite = sum(m['tipo'] == 'solo_sqlite' for m in datos['marcas_diferentes'])
    marc_fechas = sum(m['tipo'] == 'fechas_diferentes' for m in datos['marcas_diferentes'])
    lineas += ['', '6. QUÉ PUEDES HACER PARA RESOLVER LAS DIFERENCIAS', '-' * 68,
               'VIDUP ofrece estas alternativas durante la sincronización interactiva:',
               '1) Aplicar el plan seguro: revisa primero qué operaciones propone.',
               '2) Copiar solo marcas históricas de Neon a SQLite: no modifica vídeos',
               '   ni huellas; las marcas relacionadas con vídeos activos se excluyen.',
               '3) Adoptar todo el catálogo Neon: exige confirmación y respaldo íntegro',
               '   de SQLite. No modifica ni elimina los archivos multimedia físicos.',
               '4) Conservar ambas bases intactas y revisar este TXT y el JSON.',
               '0) Cancelar sin efectuar modificaciones.',
               '',
               f'Marcas que están solo en Neon: {marc_solo_neon}',
               f'Marcas que están solo en SQLite: {marc_solo_sqlite}',
               f'Marcas con fechas diferentes: {marc_fechas}',
               'Las marcas locales históricas o ajenas NO se publican sin una',
               'operación propia registrada: podrían contener borrados obsoletos.',
               'Tampoco se toma la ausencia de un archivo en otro dispositivo',
               'como autorización para eliminar su registro.',
               '', '7. ACLARACIÓN SOBRE LA ADOPCIÓN', datos['advertencia'],
               'El respaldo debe completarse antes de sustituir el catálogo local.', '']
    return '\n'.join(lineas)


def guardar_auditoria(datos: dict, carpeta_base: Path, prefijo: str) -> tuple[Path, Path]:
    """Guarda TXT y JSON con el mismo identificador en carpetas separadas."""
    sello = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    ruta_json = carpeta_base / 'json' / f'{prefijo}_{sello}.json'
    ruta_txt = carpeta_base / 'txt' / f'{prefijo}_{sello}.txt'
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_txt.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ruta_json.open('x', encoding='utf-8') as fp:
            json.dump(datos, fp, ensure_ascii=False, indent=2)
        with ruta_txt.open('x', encoding='utf-8') as fp:
            fp.write(auditoria_a_txt(datos))
    except BaseException:
        ruta_json.unlink(missing_ok=True)
        ruta_txt.unlink(missing_ok=True)
        raise
    return ruta_json, ruta_txt


def guardar_plan_txt(archivo_json: Path, datos: dict) -> Path:
    """Copia humanamente legible del plan, manteniendo el JSON interoperable."""
    destino = archivo_json.parent.parent / 'txt' / (archivo_json.stem + '.txt')
    destino.parent.mkdir(parents=True, exist_ok=True)
    grupos = [('subir', 'Registros nuevos para Neon'),
              ('bajar', 'Registros nuevos para SQLite'),
              ('completar_metadatos_neon', 'Metadatos faltantes en Neon'),
              ('completar_metadatos_local', 'Metadatos faltantes en SQLite'),
              ('completar_huellas_neon', 'Huellas visuales faltantes en Neon'),
              ('completar_huellas_local', 'Huellas visuales faltantes en SQLite'),
              ('conflictos_no_resueltos', 'Conflictos no resueltos'),
              ('publicaciones_bloqueadas_o_protegidas', 'Cambios bloqueados'),
              ('videos_borrar_en_neon', 'Bajas propuestas en Neon'),
              ('videos_borrar_en_sqlite', 'Bajas propuestas en SQLite'),
              ('marcas_para_neon', 'Marcas de eliminación para Neon'),
              ('marcas_para_sqlite', 'Marcas de eliminación para SQLite'),
              ('reactivar_marcas_neon', 'Reactivar marcas antiguas en Neon'),
              ('reactivar_marcas_sqlite', 'Reactivar marcas antiguas en SQLite'),
              ('actualizar_huellas_neon', 'Actualizar huellas visuales/de audio en Neon'),
              ('actualizar_huellas_sqlite', 'Actualizar huellas visuales/de audio en SQLite'),
              ('actualizar_fecha_neon', 'Actualizar fechas de escaneo en Neon'),
              ('actualizar_fecha_sqlite', 'Actualizar fechas de escaneo en SQLite'),
              ('reemplazar_version_antigua_neon', 'Sustituciones reales de registro en Neon'),
              ('reemplazar_version_antigua_sqlite', 'Sustituciones reales de registro en SQLite'),
              ('conflictos_de_eliminacion', 'Conflictos de eliminaciones')]
    lineas = ['VIDUP — PLAN DE SINCRONIZACIÓN', '=' * 60,
              f"Fecha: {datos['fecha']}", f"SQLite: {datos['sqlite']}",
              f"Vídeos: SQLite={datos['videos_locales']} / Neon={datos['videos_neon']}",
              '', 'Un plan no aplica cambios hasta que se autoricen.',
              'Las eliminaciones propuestas afectan al catálogo, NO a archivos físicos.', '']
    for clave, titulo in grupos:
        valores = datos.get(clave, [])
        lineas += [f'{titulo} ({len(valores)})', '-' * 54]
        for valor in valores:
            if isinstance(valor, (tuple, list)) and len(valor) == 2:
                lineas.append(f'  - {valor[0]} (fecha: {valor[1]})')
            else:
                lineas.append(f'  - {valor}')
        if not valores:
            lineas.append('  Ninguno')
        lineas.append('')
    lineas.append('Revise el JSON de este mismo nombre para el detalle técnico.')
    with destino.open('x', encoding='utf-8') as fp:
        fp.write('\n'.join(lineas) + '\n')
    return destino
