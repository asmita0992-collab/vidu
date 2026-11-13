# -*- coding: utf-8 -*-
"""Concilia reubicaciones locales con Neon sin tocar vídeos ni recalcular SHA256.

Los cambios de rutas 8.4 utilizan identificadores temporales ``__vidup_reub_*``.
Las versiones anteriores generaban erróneamente lápidas de esos IDs; nunca
representaron un archivo eliminado. Sólo se retiran si una auditoría APLICADA
acredita el cambio y ambas bases contienen ya la ruta final y su SHA256.

Las marcas reales de la ruta anterior pueden publicarse si la auditoría del
mismo dispositivo avala el cambio, las rutas finales coinciden y no queda un
vídeo activo con la clave antigua. No se realiza ningún DELETE en ``videos``
ni ``frame_hashes`` y nunca se leen archivos multimedia.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping


TEMPORAL = re.compile(r'^__vidup_reub_(\d{8}_\d{6}_\d{6})_(\d+)$')


@dataclass
class ConciliacionReubicaciones:
    tecnicas_a_quitar: list[tuple[str, str]] = field(default_factory=list)
    historicas_a_neon: list[tuple[str, str]] = field(default_factory=list)
    no_resueltas: list[str] = field(default_factory=list)
    comprobadas: list[dict] = field(default_factory=list)

    @property
    def cambios(self) -> int:
        return len(self.tecnicas_a_quitar) + len(self.historicas_a_neon)


def _leer_reporte(base: Path, sello: str, cache: dict) -> dict | None:
    """Localiza únicamente auditorías de lote 8.4, sin ejecutar su contenido."""
    if sello not in cache:
        archivo = base / 'cambios_rutas' / 'json' / f'reubicaciones_lote_{sello}.json'
        try:
            datos = json.loads(archivo.read_text(encoding='utf-8'))
            cache[sello] = datos if isinstance(datos, dict) else None
        except (OSError, ValueError, UnicodeError):
            cache[sello] = None
    return cache[sello]


def construir_plan_reubicaciones(*, dispositivo_id: str, local: Mapping[str, dict],
                                  neon: Mapping[str, dict], marcas_local: Mapping[str, str],
                                  marcas_neon: Mapping[str, str], reportes: Path) -> ConciliacionReubicaciones:
    """Sólo autoriza marcas vinculadas a auditorías APLICADAS y catálogo coincidente."""
    plan = ConciliacionReubicaciones()
    base = Path(reportes)
    cache: dict[str, dict | None] = {}
    inventarios: dict[str, dict] = {}
    # Primero se revisan las lápidas técnicas; el sello y el ID vienen de 8.4.
    for clave in sorted(set(marcas_local) - set(marcas_neon)):
        m = TEMPORAL.fullmatch(clave)
        if not m:
            continue
        sello, id_registro = m.group(1), int(m.group(2))
        reporte = _leer_reporte(base, sello, cache)
        if not reporte or reporte.get('estado') != 'APLICADO' or reporte.get('dispositivo_id') != dispositivo_id:
            plan.no_resueltas.append(f'{clave}: falta auditoría aplicada por este dispositivo; no se modifica')
            continue
        entrada = next((p for p in reporte.get('reubicaciones', [])
                        if isinstance(p, dict) and p.get('id') == id_registro), None)
        if entrada is None:
            plan.no_resueltas.append(f'{clave}: la auditoría no contiene ese ID; no se modifica')
            continue
        nueva, antigua = str(entrada.get('clave_nueva', '')), str(entrada.get('clave_anterior', ''))
        if not nueva.startswith(dispositivo_id + '|') or not antigua.startswith(dispositivo_id + '|'):
            plan.no_resueltas.append(f'{clave}: clave de otro dispositivo; no se modifica')
            continue
        registro_local, registro_neon = local.get(nueva), neon.get(nueva)
        esperado = str(entrada.get('sha256') or '').strip().lower()
        if (not registro_local or not registro_neon or
            int(registro_local['id']) != id_registro or
            not esperado or
            any(str(registro.get('hash_archivo') or '').strip().lower() != esperado
                for registro in (registro_local, registro_neon)) or
            antigua in local or antigua in neon or clave in local or clave in neon):
            plan.no_resueltas.append(f'{clave}: la ruta final o SHA256 ya no coincide; no se modifica')
            continue
        if not marcas_local.get(clave):
            plan.no_resueltas.append(f'{clave}: fecha de marca ilegible; no se modifica')
            continue
        plan.tecnicas_a_quitar.append((clave, marcas_local[clave]))
        inventarios[antigua] = entrada
        plan.comprobadas.append({'clave_temporal': clave, 'ruta_antes': entrada.get('anterior'),
                                 'ruta_despues': entrada.get('nueva'), 'sha256': esperado[:12] + '…'})
    # Una reubicación hecha por la versión corregida ya no crea la marca
    # temporal. Por ello, examinar también las auditorías del dispositivo: una
    # antigua ruta sólo es publicable si el reporte APLICADO la vincula a una
    # clave vigente que coincide en ambas bases, incluyendo el SHA256.
    carpeta = base / 'cambios_rutas' / 'json'
    for archivo in carpeta.glob('reubicaciones_lote_*.json') if carpeta.is_dir() else ():
        try:
            reporte = json.loads(archivo.read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError):
            continue
        if not isinstance(reporte, dict) or reporte.get('estado') != 'APLICADO' or reporte.get('dispositivo_id') != dispositivo_id:
            continue
        for entrada in reporte.get('reubicaciones', []):
            if not isinstance(entrada, dict):
                continue
            antigua, nueva = str(entrada.get('clave_anterior', '')), str(entrada.get('clave_nueva', ''))
            if not antigua.startswith(dispositivo_id + '|') or not nueva.startswith(dispositivo_id + '|'):
                continue
            fecha = marcas_local.get(antigua)
            if not fecha or antigua in marcas_neon or antigua in local or antigua in neon:
                continue
            a, b = local.get(nueva), neon.get(nueva)
            esperado = str(entrada.get('sha256') or '').strip().lower()
            if (a and b and esperado and
                str(a.get('hash_archivo') or '').strip().lower() == esperado and
                str(b.get('hash_archivo') or '').strip().lower() == esperado and
                int(a['id']) == int(entrada.get('id', -1))):
                inventarios[antigua] = entrada
    for antigua, entrada in sorted(inventarios.items()):
        fecha = marcas_local.get(antigua)
        if fecha and antigua not in marcas_neon and antigua not in local and antigua not in neon:
            plan.historicas_a_neon.append((antigua, fecha))
    return plan


def _escribir_informe(base: Path, datos: dict, rutas: tuple[Path, Path] | None = None) -> tuple[Path, Path]:
    """Auditoría atómica y ligera: no copia bases ni fotogramas."""
    carpeta = Path(base) / 'sincronizacion'
    sello = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    json_path, txt_path = rutas or (
        carpeta / 'json' / f'conciliar_reubicaciones_{sello}.json',
        carpeta / 'txt' / f'conciliar_reubicaciones_{sello}.txt',
    )
    for p in (json_path, txt_path):
        p.parent.mkdir(parents=True, exist_ok=True)
    lineas = [
        'VIDUP - CONCILIACION DE REUBICACIONES', '=' * 60,
        f"Estado: {datos['estado']}", f"Dispositivo: {datos['dispositivo_id']}",
        f"Identificación de Neon: {datos['generacion']}",
        f"Marcas técnicas para retirar de SQLite: {len(datos['retirar_tecnicas'])}",
        f"Marcas de antiguas rutas para publicar en Neon: {len(datos['publicar_antiguas'])}",
        f"Incidencias que NO se modifican: {len(datos['no_resueltas'])}", '',
        '¿QUÉ SIGNIFICA?',
        'Las claves __vidup_reub_* son identificadores temporales usados mientras',
        'se cambiaba una ruta. NO son vídeos ni archivos eliminados y no deben',
        'publicarse como eliminaciones en Neon.',
        'Las claves reales de la ruta anterior sí se conservan como historial',
        'cuando existe una auditoría aplicada del dispositivo y la nueva ruta',
        'ya coincide en ambos catálogos.',
        'No se borran vídeos físicos, registros activos ni huellas visuales.',
        '', ('MARCAS TÉCNICAS RETIRADAS DE SQLITE' if datos['estado'] == 'APLICADO'
             else 'MARCAS TÉCNICAS PROPUESTAS PARA RETIRAR DE SQLITE'),
    ]
    lineas += [f'  - {clave}' for clave, _ in datos['retirar_tecnicas']] or ['  Ninguna']
    lineas += ['', ('MARCAS HISTÓRICAS PUBLICADAS EN NEON' if datos['estado'] == 'APLICADO'
                    else 'MARCAS HISTÓRICAS PROPUESTAS PARA NEON')]
    lineas += [f'  - {clave}' for clave, _ in datos['publicar_antiguas']] or ['  Ninguna']
    lineas += ['', 'INCIDENCIAS QUE NECESITAN REVISIÓN']
    lineas += [f'  - {detalle}' for detalle in datos['no_resueltas']] or ['  Ninguna']
    lineas += ['', 'Verifica los cambios en el nuevo diagnóstico de sincronización.',
               'No se calculó nuevamente ningún SHA256 ni se accedió a los vídeos.', '']
    texto = '\n'.join(lineas)
    for archivo, contenido in ((json_path, json.dumps(datos, ensure_ascii=False, indent=2)),
                               (txt_path, texto)):
        temporal = archivo.with_suffix(archivo.suffix + '.tmp')
        with temporal.open('w', encoding='utf-8') as out:
            out.write(contenido)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporal, archivo)
    return json_path, txt_path


def aplicar_conciliacion(*, conn_loc: Any, conn_neon: Any, plan: ConciliacionReubicaciones,
                          reportes: Path, dispositivo_id: str, generacion: str) -> tuple[Path, Path]:
    """Concilia exclusivamente marcas certificadas; confirma Neon antes de SQLite."""
    datos = {
        'estado': 'PREPARADO - SIN APLICAR', 'dispositivo_id': dispositivo_id,
        'generacion': generacion,
        'retirar_tecnicas': plan.tecnicas_a_quitar,
        'publicar_antiguas': plan.historicas_a_neon,
        'no_resueltas': plan.no_resueltas,
        'evidencias': plan.comprobadas,
    }
    paths = _escribir_informe(reportes, datos)
    if not plan.cambios:
        return paths
    # Se trabaja bajo el bloqueo de transacción de Neon del motor principal.
    try:
        conn_loc.execute('BEGIN IMMEDIATE')
        # Comprueba que los registros de marca no cambiaron tras calcular el plan.
        for clave, fecha in plan.tecnicas_a_quitar:
            fila = conn_loc.execute('SELECT fecha_eliminacion FROM videos_eliminados '
                                    'WHERE clave_unica = ?', (clave,)).fetchone()
            if fila is None or str(fila[0]) != fecha:
                raise RuntimeError('Una marca local cambió desde el diagnóstico: ' + clave)
        for clave, fecha in plan.historicas_a_neon:
            fila = conn_loc.execute('SELECT fecha_eliminacion FROM videos_eliminados '
                                    'WHERE clave_unica = ?', (clave,)).fetchone()
            if fila is None or str(fila[0]) != fecha:
                raise RuntimeError('Una marca histórica cambió desde el diagnóstico: ' + clave)
            with conn_neon.cursor() as cur:
                cur.execute('SELECT clave_unica FROM videos WHERE clave_unica = %s', (clave,))
                if cur.fetchone():
                    raise RuntimeError('La ruta antigua reapareció en Neon; no se publica: ' + clave)
                cur.execute('INSERT INTO videos_eliminados (clave_unica, fecha_eliminacion) '
                            'VALUES (%s, %s) ON CONFLICT (clave_unica) DO NOTHING',
                            (clave, fecha))
                if cur.rowcount != 1:
                    raise RuntimeError('La marca cambió en Neon; vuelve a revisar: ' + clave)
        for clave, fecha in plan.tecnicas_a_quitar:
            cursor = conn_loc.execute('DELETE FROM videos_eliminados '
                                      'WHERE clave_unica = ? AND fecha_eliminacion = ?', (clave, fecha))
            if cursor.rowcount != 1:
                raise RuntimeError('No pudo retirarse una marca temporal: ' + clave)
            conn_loc.execute('DELETE FROM vidup_cambios_pendientes WHERE clave_unica = ?', (clave,))
        # La transacción remota se confirma primero: cualquier fallo local deja
        # solo marcas históricas adicionales, nunca vídeos eliminados.
        conn_neon.commit()
        conn_loc.commit()
    except BaseException:
        conn_loc.rollback()
        conn_neon.rollback()
        raise
    datos['estado'] = 'APLICADO'
    return _escribir_informe(reportes, datos, rutas=paths)
