# -*- coding: utf-8 -*-
"""Gestión EXPLÍCITA de la papelera remota de Infomaniak kDrive.

Solo el backend rclone nativo ``type = kdrive`` permite garantizar la semántica
papelera. Nunca ejecuta purge, cleanup, delete de directorios ni toca Neon.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from .kdrive_identidad import ID_DISPOSITIVO, MARCADOR_BD, PREFIJO_RUTA
from .horario import fmt_ahora_utc6, id_utc6

SHA_RE = re.compile(r'^[a-f0-9]{64}$')
FRASE_CONFIRMACION = 'ENVIAR A PAPELERA KDRIVE'
RUTA_REPORTE = Path(__file__).resolve().parent.parent / 'reportes' / 'kdrive' / 'papelera'


def ruta_rclone(ruta: str, remoto: str = 'kdrive:') -> str:
    """Transformación segura: solo se admiten rutas lógicas kDrive/Definitivos."""
    if not (re.fullmatch(r'[A-Za-z0-9_-]+:', remoto) and
            isinstance(ruta, str) and ruta.startswith(PREFIJO_RUTA + '/')):
        raise ValueError('Solo se permiten rutas bajo kdrive:/Definitivos/ y remoto explícito.')
    resto = ruta[len('kdrive:/'):]
    partes = resto.split('/')
    if any(not seg or seg in ('.', '..') or '\\' in seg or '\x00' in seg or '\n' in seg or '\r' in seg
           for seg in partes):
        raise ValueError('Ruta remota insegura o no válida.')
    return remoto + resto


def _json_canonico(datos: Any) -> bytes:
    return json.dumps(datos, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _sqlite_aislada(ruta: Path) -> sqlite3.Connection:
    ruta = Path(ruta).expanduser().resolve()
    if not ruta.is_file():
        raise FileNotFoundError(f'No existe SQLite de resultados: {ruta}')
    con = sqlite3.connect(ruta.as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='vidup_catalogo_aislado'").fetchone():
        con.close()
        raise ValueError('Esta SQLite no está identificada como catálogo aislado de kDrive.')
    if not con.execute('SELECT 1 FROM vidup_catalogo_aislado WHERE tipo=?', (MARCADOR_BD,)).fetchone():
        con.close()
        raise ValueError('El marcador de protección kDrive no coincide.')
    if con.execute('SELECT 1 FROM videos WHERE dispositivo_id != ? LIMIT 1', (ID_DISPOSITIVO,)).fetchone():
        con.close()
        raise ValueError('La base contiene registros ajenos a kDrive; operación bloqueada.')
    return con


def _evidencias_reporte(reporte: Path) -> list[dict]:
    datos = json.loads(Path(reporte).read_text(encoding='utf-8-sig'))
    entradas = []
    if not isinstance(datos, dict) or not isinstance(datos.get('resultados'), list):
        raise ValueError('Se requiere el reporte ORIGINAL JSON de comparación (resultados/recomendacion).')
    for r in datos['resultados']:
        if not isinstance(r, dict):
            continue
        rec = r.get('recomendacion') or {}
        if not isinstance(rec, dict) or str(rec.get('eliminar_dispositivo_id')) != ID_DISPOSITIVO:
            continue
        ruta = str(rec.get('eliminar_ruta') or '')
        try:
            ruta_rclone(ruta)
        except ValueError:
            continue
        if r.get('tipo') not in ('duplicado_exacto', 'duplicado_visual', 'posible_duplicado',
                                 'fragmento', 'posible_fragmento'):
            continue
        if not (rec.get('conservar_ruta') and rec.get('conservar_dispositivo_id') and
                (rec.get('conservar_ruta'), rec.get('conservar_dispositivo_id')) != (ruta, ID_DISPOSITIVO)):
            continue
        info = next((x for x in (r.get('info_a') or {}, r.get('info_b') or {})
                     if x.get('ruta') == ruta and x.get('dispositivo_id') == ID_DISPOSITIVO), None)
        if not info:
            continue
        sha = str(info.get('hash_archivo') or '').lower()
        tam = int(info.get('tamanio_bytes') or 0)
        if not SHA_RE.fullmatch(sha) or tam <= 0:
            continue
        entradas.append(dict(ruta=ruta, sha256=sha, tamanio_bytes=tam, tipo=r['tipo'],
                             confianza=float(r.get('confianza') or 0),
                             conservar_ruta=str(rec['conservar_ruta']),
                             conservar_dispositivo_id=str(rec['conservar_dispositivo_id']),
                             motivo=str(rec.get('motivo') or '')))
    return entradas


def generar_plan_papelera(reporte: Path, base: Path, destino: Path | None = None,
                          *, incluir_posibles: bool = False, umbral_confianza: float = 0.85) -> dict:
    """Prepara plan sin red ni escrituras; rechaza recomendaciones no verificables."""
    reporte = Path(reporte).expanduser().resolve()
    base = Path(base).expanduser().resolve()
    if not reporte.is_file():
        raise FileNotFoundError(str(reporte))
    candidatos = _evidencias_reporte(reporte)
    unicos: dict[str, dict] = {}
    descartados = []
    with closing(_sqlite_aislada(base)) as con:
        for item in candidatos:
            ruta = item['ruta']
            if ruta in unicos:
                if unicos[ruta]['sha256'] != item['sha256']:
                    raise ValueError(f'El reporte contiene SHA256 contradictorios para {ruta}.')
                continue
            if item['tipo'].startswith('posible_') and not incluir_posibles:
                descartados.append(dict(ruta=ruta, motivo='Recomendación posible: requiere --incluir-posibles'))
                continue
            if item['tipo'] != 'duplicado_exacto' and item['confianza'] < umbral_confianza:
                descartados.append(dict(ruta=ruta, motivo='Confianza inferior al mínimo configurado'))
                continue
            row = con.execute('''SELECT clave_unica,hash_archivo,tamanio_bytes,fecha_escaneo
                                 FROM videos WHERE dispositivo_id=? AND ruta=? AND error IS NULL''',
                              (ID_DISPOSITIVO, ruta)).fetchone()
            if not row or row['hash_archivo'].lower() != item['sha256'] or int(row['tamanio_bytes']) != item['tamanio_bytes']:
                descartados.append(dict(ruta=ruta, motivo='La SQLite no coincide con el reporte'))
                continue
            unicos[ruta] = dict(item, clave_unica=str(row['clave_unica']),
                               fecha_escaneo=str(row['fecha_escaneo']))
    plan = dict(version=1, tipo='kdrive_papelera', creado=fmt_ahora_utc6(),
                reporte=str(reporte), reporte_sha256=hashlib.sha256(reporte.read_bytes()).hexdigest(),
                sqlite=str(base), dispositivo_id=ID_DISPOSITIVO,
                candidatos=list(unicos.values()), descartados=descartados,
                remoto='kdrive:', modo='SOLO PAPELERA; nunca limpieza permanente',
                advertencia='Nombre y tamaño no verifican SHA256 remoto: confirmar identidad antes del borrado.')
    plan['firma'] = hashlib.sha256(_json_canonico(plan)).hexdigest()
    if destino is None:
        destino = RUTA_REPORTE / 'planes' / f'plan_papelera_{id_utc6()}.json'
    destino = Path(destino).expanduser().resolve()
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    plan['ruta_plan'] = str(destino)
    return plan


def _rclone(cmd: list[str], *, timeout: int = 120, runner: Callable = subprocess.run):
    return runner(['rclone', *cmd], capture_output=True, text=True, timeout=timeout)


def _backend_nativo(runner=subprocess.run) -> None:
    out = _rclone(['config', 'show', 'kdrive'], runner=runner)
    if out.returncode != 0 or not re.search(r'^\s*type\s*=\s*kdrive\s*$', out.stdout, re.M | re.I):
        raise RuntimeError('Rclone no tiene backend kdrive nativo. No se puede garantizar papelera; no se elimina nada.')


def _stat_remoto(remoto: str, runner=subprocess.run) -> dict | None:
    out = _rclone(['lsjson', '--stat', remoto], runner=runner)
    if out.returncode:
        # Un fallo de red, autorización o rate-limit NO significa que el
        # archivo haya sido enviado a papelera.
        error = (out.stderr or out.stdout or '').strip()
        if re.search(r'(?i)(object not found|directory not found|file not found|404 not found|no such file)', error):
            return None
        raise RuntimeError('No fue posible comprobar el archivo remoto: ' + error[-450:])
    try:
        data = json.loads(out.stdout)
    except ValueError as exc:
        raise RuntimeError('rclone lsjson devolvió datos inválidos.') from exc
    if not isinstance(data, dict) or data.get('IsDir'):
        raise RuntimeError('El objeto remoto no es un archivo verificable.')
    return data


def aplicar_plan_papelera(plan_json: Path, *, confirmar: str,
                           runner=subprocess.run) -> dict:
    """Envía archivos puntuales a papelera, con bitácora durable y respaldo local.

    No escribe en Neon. Nunca marca una eliminación como correcta solo por el
    código de retorno de rclone: exige comprobar que la ruta ya no es visible.
    """
    from .presentacion import confirma_frase
    if not confirma_frase(confirmar, FRASE_CONFIRMACION):
        raise ValueError(f'Confirmación necesaria: {FRASE_CONFIRMACION}')
    plan_json = Path(plan_json).expanduser().resolve()
    plan = json.loads(plan_json.read_text(encoding='utf-8'))
    firma = plan.pop('firma', None)
    if not isinstance(firma, str) or hashlib.sha256(_json_canonico(plan)).hexdigest() != firma:
        raise ValueError('Plan modificado o inválido.')
    if plan.get('tipo') != 'kdrive_papelera' or plan.get('dispositivo_id') != ID_DISPOSITIVO:
        raise ValueError('Este plan no corresponde a kDrive.')
    reporte = Path(plan['reporte'])
    if not reporte.is_file() or hashlib.sha256(reporte.read_bytes()).hexdigest() != plan['reporte_sha256']:
        raise ValueError('El reporte original cambió o no está disponible.')
    base = Path(plan['sqlite']).resolve()
    candidatos = plan.get('candidatos') or []
    if not candidatos:
        return {'enviados_papelera': 0, 'omitidos': 0, 'reporte': None}
    _backend_nativo(runner)
    # Respaldo integral, fuera de la transacción de ficheros remotos.
    with closing(_sqlite_aislada(base)) as lectura:
        for item in candidatos:
            ruta_rclone(item['ruta'])
            fila = lectura.execute('SELECT hash_archivo,tamanio_bytes,fecha_escaneo FROM videos WHERE dispositivo_id=? AND ruta=?',
                                  (ID_DISPOSITIVO, item['ruta'])).fetchone()
            if not fila or (str(fila['hash_archivo']).lower() != item['sha256']
                            or int(fila['tamanio_bytes']) != item['tamanio_bytes']
                            or str(fila['fecha_escaneo']) != item['fecha_escaneo']):
                raise ValueError('SQLite cambió desde el plan; hay que emitir uno nuevo.')
        # Los reportes simulados también guardan el respaldo junto a sus propias
        # carpetas, nunca contaminan la instalación real en pruebas.
        raiz = next((x.parent for x in (RUTA_REPORTE, *RUTA_REPORTE.parents)
                     if x.name == 'reportes'), Path(__file__).resolve().parent.parent)
        respaldo = raiz / 'bases' / 'respaldos' / 'db' / f'kdrive_pre_papelera_{id_utc6()}.db'
        respaldo.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(respaldo)) as copia:
            lectura.backup(copia)
    bitacora = RUTA_REPORTE / 'jsonl' / f'papelera_{id_utc6()}.jsonl'
    bitacora.parent.mkdir(parents=True, exist_ok=True)
    resultados = []
    with bitacora.open('x', encoding='utf-8') as fh:
        for item in candidatos:
            remoto = ruta_rclone(item['ruta'])
            registro = dict(fecha=fmt_ahora_utc6(), ruta=item['ruta'],
                            sha256=item['sha256'], tamano=item['tamanio_bytes'], estado='omitido')
            try:
                stat = _stat_remoto(remoto, runner=runner)
                if stat is None or int(stat.get('Size', -1)) != item['tamanio_bytes']:
                    registro['motivo'] = 'Archivo remoto ausente o tamaño diferente: no se tocó.'
                else:
                    registro['remoto_id'] = stat.get('ID', '')
                    registro['estado'] = 'enviando'
                    fh.write(json.dumps(registro, ensure_ascii=False) + '\n')
                    fh.flush(); os.fsync(fh.fileno())
                    # No usar rclone delete (directorios), purge ni cleanup.
                    out = _rclone(['deletefile', remoto], runner=runner)
                    if out.returncode:
                        registro['estado'] = 'error'; registro['motivo'] = out.stderr[-500:]
                    elif _stat_remoto(remoto, runner=runner) is not None:
                        registro['estado'] = 'incierto'
                        registro['motivo'] = 'No se pudo confirmar que desapareciera de la carpeta remota.'
                    else:
                        registro['estado'] = 'papelera'
                        with sqlite3.connect(str(base), timeout=30) as con:
                            con.execute('PRAGMA foreign_keys=ON')
                            fila = con.execute('SELECT hash_archivo,tamanio_bytes,fecha_escaneo FROM videos WHERE dispositivo_id=? AND ruta=?',
                                               (ID_DISPOSITIVO, item['ruta'])).fetchone()
                            if not fila or (fila[0].lower(), int(fila[1]), str(fila[2])) != (
                                    item['sha256'], item['tamanio_bytes'], item['fecha_escaneo']):
                                registro['estado'] = 'papelera_sin_actualizar_sqlite'
                                registro['motivo'] = 'SQLite cambió después de mover archivo; requiere conciliación.'
                            else:
                                clave = item['clave_unica']
                                marca_fecha = fmt_ahora_utc6()
                                con.execute('BEGIN IMMEDIATE')
                                con.execute('DELETE FROM videos WHERE dispositivo_id=? AND ruta=? AND hash_archivo=?',
                                            (ID_DISPOSITIVO, item['ruta'], item['sha256']))
                                con.execute('INSERT OR REPLACE INTO videos_eliminados(clave_unica,fecha_eliminacion) VALUES (?,?)',
                                            (clave, marca_fecha))
                                con.execute('''CREATE TABLE IF NOT EXISTS kdrive_papelera_eventos (
                                    clave_unica TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                                    fecha TEXT NOT NULL, reporte TEXT NOT NULL)''')
                                con.execute('INSERT OR REPLACE INTO kdrive_papelera_eventos VALUES (?,?,?,?)',
                                            (clave, item['sha256'], marca_fecha, str(plan_json)))
                                con.commit()
            except Exception as exc:
                registro['estado'] = 'error'
                registro['motivo'] = str(exc)
            fh.write(json.dumps(registro, ensure_ascii=False) + '\n')
            fh.flush(); os.fsync(fh.fileno())
            resultados.append(registro)
    resumen = {'fecha': fmt_ahora_utc6(), 'plan': str(plan_json),
               'respaldo_sqlite': str(respaldo), 'bitacora_jsonl': str(bitacora),
               'enviados_papelera': sum(r['estado'] == 'papelera' for r in resultados),
               'omitidos_o_error': sum(r['estado'] != 'papelera' for r in resultados),
               'registros': resultados,
               'aviso': 'Los archivos están en papelera kDrive, no eliminados permanentemente.'}
    salida = RUTA_REPORTE / 'json' / f'papelera_{id_utc6()}.json'
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps(resumen, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    txt = RUTA_REPORTE / 'txt' / (salida.stem + '.txt')
    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text('VIDUP · ENVÍO A PAPELERA KDRIVE\n' +
                   f'Completados: {resumen["enviados_papelera"]} | Incompletos: {resumen["omitidos_o_error"]}\n' +
                   f'Respaldo SQLite: {respaldo}\nBitácora JSONL: {bitacora}\n\n' +
                   '\n'.join(f'{r["estado"]}: {r["ruta"]} {r.get("motivo", "")}' for r in resultados) + '\n', encoding='utf-8')
    resumen['reporte_json'] = str(salida)
    resumen['reporte_txt'] = str(txt)
    return resumen


def _elegir_archivo_kdrive(etiqueta: str, *, tipo: str) -> Path:
    """Selector local sin volver a introducir rutas de informes habituales.

    Acepta número, Enter para el más reciente o M para una ruta manual.
    No crea archivos ni abre bases en modo escritura.
    """
    from .presentacion import imprimir, pedir_ruta, pedir_texto, rich_escape
    from .almacenamiento import DIR_SCRIPT, carpeta_bases, carpeta_reportes
    if tipo == 'comparacion':
        carpetas = [carpeta_reportes() / 'comparativas' / 'json',
                    DIR_SCRIPT / 'comparativas' / 'json']
        patrones = ['comparacion_*.json']
    elif tipo == 'sqlite':
        carpetas = [DIR_SCRIPT, carpeta_bases() / 'respaldos' / 'db',
                    carpeta_reportes() / 'respaldos' / 'db']
        patrones = ['*.db', '*.sqlite', '*.sqlite3']
    else:
        raise ValueError('Selector desconocido.')
    vistos = set()
    candidatos = []
    for carpeta in carpetas:
        for patron in patrones:
            for archivo in carpeta.glob(patron) if carpeta.is_dir() else ():
                if not archivo.is_file():
                    continue
                clave = str(archivo.resolve())
                if clave not in vistos:
                    vistos.add(clave)
                    candidatos.append(archivo)
    if tipo == 'sqlite':
        # El resultado de kDrive es el candidato preferido, incluso si otra
        # SQLite ordinaria fue editada más recientemente.
        nombre_preferido = DIR_SCRIPT / 'vidup_kdrive_resultados.db'
        candidatos.sort(key=lambda x: (x.resolve() == nombre_preferido.resolve(),
                                       x.stat().st_mtime_ns), reverse=True)
    else:
        candidatos.sort(key=lambda x: x.stat().st_mtime_ns, reverse=True)
    candidatos = candidatos[:15]
    if candidatos:
        imprimir(f'[turquoise2]{rich_escape(etiqueta)}[/]')
        for n, ruta in enumerate(candidatos, 1):
            imprimir(f'  [gold1]{n:>2}[/]  {rich_escape(str(ruta))}')
        imprimir('  [gold1]M[/]   Introducir ruta manual')
        while True:
            respuesta = pedir_texto('Elige un número (Enter = 1) o M', '1',
                                   permitir_escape=False).strip().casefold()
            if respuesta.isdecimal() and 1 <= int(respuesta) <= len(candidatos):
                return candidatos[int(respuesta)-1]
            if respuesta in ('m','manual'):
                break
            imprimir('[yellow]Elige una de las opciones mostradas.[/]')
    return Path(pedir_ruta(etiqueta, existente=True))


def papelera_interactiva() -> None:
    from .presentacion import pedir_texto, imprimir, rich_escape, confirma_frase
    reporte = _elegir_archivo_kdrive('JSON original de comparación', tipo='comparacion')
    db = _elegir_archivo_kdrive('SQLite de resultados kDrive', tipo='sqlite')
    plan = generar_plan_papelera(reporte, db)
    imprimir(f'[turquoise2]Plan: {rich_escape(plan["ruta_plan"])}[/]')
    imprimir(f'[gold1]Candidatos: {len(plan["candidatos"])}; omitidos: {len(plan["descartados"])}[/]')
    if not plan['candidatos']:
        return
    frase = pedir_texto(f'Para enviar a papelera escribe {FRASE_CONFIRMACION}', '', permitir_escape=False)
    if not confirma_frase(frase, FRASE_CONFIRMACION):
        imprimir('[yellow]Cancelado. No se eliminó ningún archivo remoto.[/]')
        return
    resultado = aplicar_plan_papelera(plan['ruta_plan'], confirmar=frase)
    imprimir('[spring_green2]Enviados: ' + str(resultado['enviados_papelera']) + '[/]')
    imprimir('[cyan]Informe: ' + rich_escape(resultado['reporte_json']) + '[/]')
