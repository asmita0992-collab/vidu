"""Inventario VIDUP: cotejo bidireccional por ruta, exclusiones y reportes.

La verificación NO lee contenido de los vídeos ni calcula hashes. Solo confirma
la presencia de rutas en el dispositivo local. No altera la base de datos.
"""
from __future__ import annotations

from core.dependencias import EXTENSIONES_VIDEO
from core.presentacion import imprimir, rich_escape
from core.almacenamiento import DIR_SCRIPT

import csv
from contextlib import contextmanager
import json
import ntpath
import os
import posixpath
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

EXTENSIONES_VIDEO = frozenset({
    '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v',
    '.mpg', '.mpeg', '.ts', '.3gp', '.ogv', '.rm', '.rmvb', '.vob',
    '.divx', '.m2ts',
})
ZONA_MEXICO = timezone(timedelta(hours=-6))


def fecha_id() -> str:
    return datetime.now(ZONA_MEXICO).strftime('%Y%m%d_%H%M%S')


def fecha_texto() -> str:
    return datetime.now(ZONA_MEXICO).strftime('%Y-%m-%d %H:%M:%S UTC-6')


def es_windows(ruta: str) -> bool:
    ruta = str(ruta or '')
    return bool(re.match(r'^[A-Za-z]:[\\/]', ruta) or ruta.startswith('\\\\') or '\\' in ruta)


def normalizar(ruta: str) -> str:
    ruta = str(ruta or '').strip().strip('"')
    if not ruta:
        return ''
    if es_windows(ruta):
        return 'win:' + ntpath.normcase(ntpath.normpath(ruta))
    return 'posix:' + posixpath.normpath(ruta)


def carpeta_de(ruta: str) -> str:
    modulo = ntpath if es_windows(ruta) else posixpath
    return modulo.dirname(modulo.normpath(str(ruta)))


def dentro_de(ruta: str, carpeta: str, recursivo: bool = True) -> bool:
    """Verifica límites reales de carpeta (sin coincidencias de prefijos parciales)."""
    a, b = normalizar(ruta), normalizar(carpeta)
    if not a or not b or a.split(':', 1)[0] != b.split(':', 1)[0]:
        return False
    modulo = ntpath if a.startswith('win:') else posixpath
    ar, br = a.split(':', 1)[1], b.split(':', 1)[1]
    if not recursivo:
        return modulo.dirname(ar) == br
    try:
        return modulo.commonpath([ar, br]) == br
    except ValueError:
        return False


def excluido(ruta: str, carpetas_excluidas: Sequence[str]) -> bool:
    # Exclusión automática de la carpeta de la herramienta
    if dentro_de(ruta, str(DIR_SCRIPT), recursivo=True):
        return True
    return any(dentro_de(ruta, c, recursivo=True) for c in carpetas_excluidas)


def cargar_exclusiones(raiz: Path, dispositivo_id: str) -> List[str]:
    config = Path(raiz) / '.vidup_exclusiones.json'
    try:
        with config.open('r', encoding='utf-8') as f:
            datos = json.load(f)
        carpetas = datos.get('dispositivos', {}).get(str(dispositivo_id), [])
        if not isinstance(carpetas, list):
            return []
        return [str(r) for r in carpetas if isinstance(r, str) and r.strip()]
    except FileNotFoundError:
        return []
    except (ValueError, OSError, TypeError) as exc:
        raise ValueError(f'No se pudo leer la configuración de exclusiones: {exc}') from exc


def guardar_exclusiones(raiz: Path, dispositivo_id: str, rutas: Sequence[str]) -> Path:
    raiz = Path(raiz)
    archivo = raiz / '.vidup_exclusiones.json'
    if archivo.exists():
        with archivo.open('r', encoding='utf-8') as f:
            datos = json.load(f)
        if not isinstance(datos, dict):
            raise ValueError('Configuración de exclusiones inválida.')
    else:
        datos = {'version': 1, 'dispositivos': {}}
    dispositivos = datos.setdefault('dispositivos', {})
    if not isinstance(dispositivos, dict):
        raise ValueError('Configuración de exclusiones inválida.')
    unicas = {}
    for ruta in rutas:
        ruta = str(ruta).strip().strip('"')
        if ruta:
            unicas[normalizar(ruta)] = ruta
    dispositivos[str(dispositivo_id)] = list(unicas.values())
    temporal = archivo.with_suffix('.json.tmp')
    with temporal.open('w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(temporal, archivo)
    return archivo


def registros_dispositivo(conn: Any, dispositivo_id: str) -> List[Dict[str, Any]]:
    from . import database
    if database._es_pg(conn):
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute('SELECT * FROM videos WHERE dispositivo_id = %s ORDER BY id', (dispositivo_id,))
            return [dict(r) for r in cur.fetchall()]
    return [dict(r) for r in conn.execute(
        'SELECT * FROM videos WHERE dispositivo_id = ? ORDER BY id', (dispositivo_id,)).fetchall()]


def _leer_carpeta(carpeta: str) -> List[str]:
    """Solo archivos de vídeo directamente contenidos; propaga los errores."""
    resultado = []
    with os.scandir(carpeta) as items:
        for ent in items:
            if ent.name.lower().endswith(tuple(EXTENSIONES_VIDEO)) and ent.is_file(follow_symlinks=False):
                resultado.append(ent.path)
    return resultado


def verificar_bidireccional(registros: Sequence[Dict[str, Any]],
                             dispositivo_id: str,
                             excluidas: Sequence[str] = (),
                             raices_adicionales: Sequence[str] = (),
                             recursivo_adicional: bool = True) -> Dict[str, Any]:
    """Compara rutas registradas del dispositivo local contra contenido real.

    `faltantes`: la carpeta inmediata pudo leerse, pero el archivo no figura.
    `sin_registro`: archivo presente dentro del alcance, sin ruta en BD.
    `no_verificables`: carpetas que no se pudieron abrir; sus archivos NUNCA
    se declaran faltantes. Un disco desmontado no se considera vacío.
    """
    registros = [dict(r) for r in registros if str(r.get('dispositivo_id') or '') == dispositivo_id]
    por_ruta: Dict[str, List[Dict[str, Any]]] = {}
    carpetas: Dict[str, str] = {}
    ignorados: List[Dict[str, Any]] = []
    for r in registros:
        ruta = str(r.get('ruta') or '')
        if not ruta or excluido(ruta, excluidas):
            ignorados.append({'ruta': ruta, 'motivo': 'Ruta vacía o excluida'})
            continue
        clave = normalizar(ruta)
        por_ruta.setdefault(clave, []).append(r)
        c = carpeta_de(ruta)
        carpetas[normalizar(c)] = c

    encontrados: Dict[str, str] = {}
    verificadas: Dict[str, str] = {}
    no_verificables: Dict[str, str] = {}
    carpetas_excluidas = set()

    def leer(carpeta: str) -> None:
        clave = normalizar(carpeta)
        if clave in verificadas or clave in no_verificables or clave in carpetas_excluidas:
            return
        if excluido(carpeta, excluidas):
            carpetas_excluidas.add(clave)
            return
        try:
            archivos = _leer_carpeta(carpeta)
            verificadas[clave] = carpeta
            for archivo in archivos:
                if not excluido(archivo, excluidas):
                    encontrados[normalizar(archivo)] = archivo
        except OSError as exc:
            no_verificables[clave] = f'{carpeta}: {exc}'

    for carpeta in carpetas.values():
        leer(carpeta)

    for raiz in raices_adicionales:
        raiz = str(raiz).strip()
        if not raiz or excluido(raiz, excluidas):
            continue
        if not recursivo_adicional:
            leer(raiz)
            continue
        # os.walk no sigue directorios enlazados; registra fallos de acceso.
        if not os.path.isdir(raiz):
            leer(raiz)
            continue
        def error_camino(exc):
            no_verificables[normalizar(exc.filename or raiz)] = str(exc)
        for actual, subdirs, _ in os.walk(raiz, onerror=error_camino, followlinks=False):
            subdirs[:] = [d for d in subdirs if not excluido(os.path.join(actual, d), excluidas)]
            leer(actual)

    faltantes = []
    presentes = []
    pendientes = []
    for r in registros:
        ruta = str(r.get('ruta') or '')
        if not ruta or excluido(ruta, excluidas):
            continue
        clave = normalizar(ruta)
        carpeta = normalizar(carpeta_de(ruta))
        if carpeta not in verificadas:
            pendientes.append({'ruta': ruta, 'nombre': r.get('nombre', ''),
                                'dispositivo_id': dispositivo_id,
                                'motivo': no_verificables.get(carpeta, 'Carpeta no verificada')})
        elif clave not in encontrados:
            faltantes.append({'ruta': ruta, 'nombre': r.get('nombre', ''),
                              'dispositivo_id': dispositivo_id,
                              'sha256': r.get('hash_archivo', ''),
                              'tamanio_bytes': int(r.get('tamanio_bytes') or 0)})
        else:
            presentes.append(ruta)

    sin_registro = [{'ruta': ruta, 'nombre': os.path.basename(ruta),
                     'dispositivo_id': dispositivo_id}
                    for clave, ruta in encontrados.items() if clave not in por_ruta]
    sin_registro.sort(key=lambda r: r['ruta'].casefold())
    faltantes.sort(key=lambda r: r['ruta'].casefold())
    resumen_carpetas = []
    for clave, carpeta in sorted(carpetas.items()):
        registros_carpeta = [r for r in registros
                             if normalizar(carpeta_de(r.get('ruta') or '')) == clave
                             and not excluido(str(r.get('ruta') or ''), excluidas)]
        resumen_carpetas.append({
            'carpeta': carpeta, 'registrados': len(registros_carpeta),
            'encontrados': (sum(normalizar(carpeta_de(r)) == clave for r in encontrados.values())
                            if clave in verificadas else None),
            'estado': 'verificada' if clave in verificadas else 'no_verificable',
        })

    return {
        'fecha_utc6': fecha_texto(), 'dispositivo_id': dispositivo_id,
        'metodo': 'rutas y nombres, sin recalcular SHA256',
        'registrados': len(registros), 'presentes': len(presentes),
        'faltantes': faltantes, 'sin_registro': sin_registro,
        'no_verificados': pendientes, 'carpetas_no_verificables': sorted(no_verificables.values()),
        'ignorados_por_exclusion': ignorados,
        'carpetas_verificadas': len(verificadas),
        'carpetas': resumen_carpetas, 'raices_adicionales': list(raices_adicionales),
    }


def _csv_escribir(destino: Path, encabezado: Sequence[str], filas: Iterable[Dict[str, Any]]) -> None:
    with destino.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(encabezado), extrasaction='ignore')
        writer.writeheader()
        for fila in filas:
            writer.writerow(fila)


def _destinos(base: Path, prefijo: str) -> Dict[str, Path]:
    identificador = fecha_id()
    resultado = {}
    for ext in ('json', 'txt', 'csv'):
        carpeta = Path(base) / ext
        carpeta.mkdir(parents=True, exist_ok=True)
        destino = carpeta / f'{prefijo}_{identificador}.{ext}'
        contador = 1
        while destino.exists():
            destino = carpeta / f'{prefijo}_{identificador}_{contador}.{ext}'
            contador += 1
        resultado[ext] = destino
    return resultado


def reportar_estadisticas(base: Path, generales: dict, dispositivos: Sequence[dict],
                           origen: str) -> Dict[str, Path]:
    destinos = _destinos(base, 'estadisticas')
    paquete = {'fecha_utc6': fecha_texto(), 'base': origen,
               'generales': generales, 'dispositivos': list(dispositivos)}
    with destinos['json'].open('w', encoding='utf-8') as f:
        json.dump(paquete, f, ensure_ascii=False, indent=2)
    with destinos['txt'].open('w', encoding='utf-8') as f:
        f.write(f'ESTADÍSTICAS VIDUP — {paquete["fecha_utc6"]}\nOrigen: {origen}\n')
        f.write(f'Vídeos totales: {generales["total_videos"]}\n')
        f.write(f'Frames totales: {generales["total_frames"]}\n')
        f.write(f'Marcas de eliminación: {generales["total_eliminaciones"]}\n')
        f.write(f'Procesados sin error: {generales["con_huella"]}\n')
        f.write(f'Con error: {generales["con_error"]}\n')
        f.write(f'Duración total: {generales["horas_totales"]} h\n')
        f.write(f'Tamaño total: {generales["gb_totales"]} GB\n')
        f.write(f'Dispositivos/grupos: {len(dispositivos)}\n')
        f.write('Etiquetas de origen registradas (sin volver a contar vídeos):\n')
        for nombre in generales.get('dispositivos_origen', []):
            f.write(f'  {nombre}\n')
        for disp in dispositivos:
            f.write(f'\nDISPOSITIVO: {disp["dispositivo"]} (ID {disp["dispositivo_id"] or "SIN ID"})\n')
            f.write(f'  Vídeos: {disp["total_videos"]}; bytes: {disp["tamanio_bytes"]}; SHA256: '
                    f'{disp["con_sha256"]}; visual: {disp["con_huella_visual"]}; '
                    f'audio: {disp["con_huella_audio"]}; errores: {disp["con_error"]}\n')
            for carpeta in disp['carpetas']:
                f.write(f'  {carpeta["ruta"]} | {carpeta["total_videos"]} vídeos | '
                        f'{carpeta["tamanio_bytes"]} bytes | SHA256 {carpeta["con_sha256"]} | '
                        f'visual {carpeta["con_huella_visual"]} | '
                        f'audio {carpeta.get("con_huella_audio", 0)}\n')
    filas = [dict(tipo='general', dispositivo='', dispositivo_id='', carpeta=origen,
                  videos=generales['total_videos'], frames_totales=generales['total_frames'],
                  marcas_eliminacion=generales['total_eliminaciones'],
                  tamanio_bytes='', sha256='', visual='',
                  audio='', errores=generales['con_error'],
                  procesados_sin_error=generales['con_huella'],
                  duracion_horas=generales['horas_totales'],
                  gb_totales=generales['gb_totales'],
                  grupos_dispositivo=len(dispositivos),
                  etiquetas_origen=' | '.join(generales.get('dispositivos_origen', [])))]
    for disp in dispositivos:
        principal: dict[str, str | int] = dict(
            tipo='dispositivo', dispositivo=disp['dispositivo'],
            dispositivo_id=disp['dispositivo_id'], carpeta='',
            videos=disp['total_videos'], tamanio_bytes=disp['tamanio_bytes'],
            sha256=disp['con_sha256'], visual=disp['con_huella_visual'],
            audio=disp['con_huella_audio'], errores=disp['con_error'])
        filas.append(principal)
        for carpeta in disp['carpetas']:
            filas.append(dict(tipo='carpeta', dispositivo=disp['dispositivo'],
                              dispositivo_id=disp['dispositivo_id'], carpeta=carpeta['ruta'],
                              videos=carpeta['total_videos'], tamanio_bytes=carpeta['tamanio_bytes'],
                              sha256=carpeta['con_sha256'], visual=carpeta['con_huella_visual'],
                              audio=carpeta.get('con_huella_audio', 0), errores=''))
    _csv_escribir(destinos['csv'], ('tipo','dispositivo','dispositivo_id','carpeta','videos',
                                   'tamanio_bytes','sha256','visual','audio','errores',
                                   'procesados_sin_error','duracion_horas','gb_totales',
                                   'grupos_dispositivo','etiquetas_origen',
                                   'frames_totales','marcas_eliminacion'), filas)
    return destinos


def reportar_verificacion(base: Path, resultado: dict) -> Dict[str, Path]:
    destinos = _destinos(base, 'verificacion_bidireccional')
    with destinos['json'].open('w', encoding='utf-8') as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)
    with destinos['txt'].open('w', encoding='utf-8') as f:
        f.write(f'COMPROBACIÓN BIDIRECCIONAL — {resultado["fecha_utc6"]}\n')
        f.write(f'Dispositivo: {resultado["dispositivo_id"]}\n')
        f.write('Comprobación de rutas, NO verifica contenido ni recalcula SHA256.\n')
        f.write(f'Registrados: {resultado["registrados"]}; presentes: {resultado["presentes"]}; '
                f'ausentes: {len(resultado["faltantes"])}; sin registro: '
                f'{len(resultado["sin_registro"])}; no verificados: '
                f'{len(resultado["no_verificados"])}\n')
        for carpeta in resultado['carpetas']:
            f.write(f'\nCARPETA {carpeta["carpeta"]} | BD {carpeta["registrados"]} | '
                    f'disco {carpeta["encontrados"] if carpeta["encontrados"] is not None else "N/D"} '
                    f'| {carpeta["estado"]}\n')
        for nombre, clave in (('REGISTRADOS QUE YA NO EXISTEN', 'faltantes'),
                              ('EN DISCO SIN REGISTRO EN BASE', 'sin_registro'),
                              ('REGISTRADOS NO VERIFICABLES', 'no_verificados'),
                              ('EXCLUIDOS', 'ignorados_por_exclusion')):
            f.write(f'\n{nombre} ({len(resultado[clave])})\n')
            for item in resultado[clave]:
                f.write(f'- {item["ruta"]}')
                if item.get('motivo'):
                    f.write(f' | {item["motivo"]}')
                f.write('\n')
        f.write('\nCARPETAS NO VERIFICABLES\n')
        for texto in resultado['carpetas_no_verificables']:
            f.write(f'- {texto}\n')
    filas = []
    for estado, key in (('faltante','faltantes'), ('sin_registro','sin_registro'),
                        ('no_verificado','no_verificados'), ('excluido','ignorados_por_exclusion')):
        for r in resultado[key]:
            filas.append({'estado': estado, 'ruta': r.get('ruta'), 'nombre': r.get('nombre'),
                          'dispositivo_id': resultado['dispositivo_id'],
                          'sha256_registrado': r.get('sha256',''), 'motivo': r.get('motivo','')})
    _csv_escribir(destinos['csv'], ('estado','ruta','nombre','dispositivo_id',
                                   'sha256_registrado','motivo'), filas)
    return destinos


def registros_carpeta(conn: Any, dispositivo_id: str, carpeta: str,
                     recursivo: bool = True) -> List[Dict[str, Any]]:
    return [r for r in registros_dispositivo(conn, dispositivo_id)
            if dentro_de(str(r.get('ruta') or ''), carpeta, recursivo)]


def agregar_huellas_frames(conn: Any, registros: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from . import database
    pg = database._es_pg(conn)
    por_id = {int(r['id']): dict(r, frame_hashes=[]) for r in registros}
    ids = list(por_id)
    if not ids:
        return []
    for comienzo in range(0, len(ids), 500):
        lote = ids[comienzo:comienzo + 500]
        if pg:
            import psycopg2.extras
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                cursor.execute('SELECT video_id, posicion, hash_valor FROM frame_hashes '
                               'WHERE video_id = ANY(%s) ORDER BY video_id, posicion', (lote,))
                filas = cursor.fetchall()
        else:
            marcas = ','.join('?' for _ in lote)
            filas = conn.execute('SELECT video_id, posicion, hash_valor FROM frame_hashes '
                                 f'WHERE video_id IN ({marcas}) ORDER BY video_id, posicion', lote).fetchall()
        for fila in filas:
            por_id[int(fila['video_id'])]['frame_hashes'].append(str(fila['hash_valor']))
    return list(por_id.values())


@contextmanager
def _abrir_reporte_atomico(destino: Path, *, encoding: str = 'utf-8',
                          newline: Optional[str] = None):
    """No destruye el respaldo existente si falla la reescritura del reporte."""
    temporal = destino.with_name(destino.name + '.tmp')
    try:
        with temporal.open('w', encoding=encoding, newline=newline) as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporal, destino)
    finally:
        if temporal.exists():
            temporal.unlink()


def reportar_depuracion(base: Path, dispositivo: str, dispositivo_id: str,
                       carpeta: str, registros: Sequence[dict],
                       estado: str, destinos: Optional[Dict[str, Path]] = None) -> Dict[str, Path]:
    """Respaldo completo de las filas afectadas ANTES del DELETE, en tres formatos."""
    if destinos is None:
        destinos = _destinos(base, 'depuracion_carpeta')
    paquete = {'fecha_utc6': fecha_texto(), 'estado': estado,
               'dispositivo': dispositivo, 'dispositivo_id': dispositivo_id,
               'carpeta': carpeta, 'incluye_subcarpetas': True,
               'total_eliminados_o_previstos': len(registros), 'videos': list(registros)}
    with _abrir_reporte_atomico(destinos['json']) as f:
        json.dump(paquete, f, ensure_ascii=False, indent=2, default=str)
    with _abrir_reporte_atomico(destinos['txt']) as f:
        f.write(f'DEPURACIÓN DE REGISTROS — {paquete["fecha_utc6"]}\n')
        f.write(f'Estado: {estado}\nDispositivo: {dispositivo} (ID {dispositivo_id})\n')
        f.write(f'Carpeta y subcarpetas: {carpeta}\nRegistros: {len(registros)}\n')
        f.write('SOLO DATOS EN BASE; NO SE BORRAN VÍDEOS DEL DISCO.\n')
        for r in registros:
            f.write(f'\n{r.get("ruta", "")}\n  nombre: {r.get("nombre", "")}\n')
            f.write(f'  SHA256: {r.get("hash_archivo", "")}\n')
            f.write(f'  hash rápido: {r.get("hash_rapido", "")}\n')
            f.write(f'  audio: {r.get("audio_huella", "")}\n')
            f.write(f'  frame hashes: {json.dumps(r.get("frame_hashes", []), ensure_ascii=False)}\n')
    with _abrir_reporte_atomico(destinos['csv'], encoding='utf-8-sig', newline='') as f:
        columnas = ['dispositivo','dispositivo_id','ruta','nombre','hash_archivo',
                    'hash_rapido','audio_huella','frame_hashes','tamanio_bytes',
                    'fecha_escaneo','duracion','ancho','alto','codec_video','codec_audio']
        writer = csv.DictWriter(f, fieldnames=columnas)
        writer.writeheader()
        for r in registros:
            fila = {k: r.get(k, '') for k in columnas}
            fila['frame_hashes'] = json.dumps(r.get('frame_hashes', []), ensure_ascii=False)
            writer.writerow(fila)
    return destinos


def eliminar_registros_exactos(conn: Any, dispositivo_id: str,
                              registros: Sequence[Dict[str, Any]]) -> int:
    """Elimina solo filas respaldadas tras bloquear y verificar su estado.

    Una modificación concurrente (por otra sesión/proceso) aborta el borrado.
    """
    from . import database
    from core.sincronizacion import registrar_eliminacion
    pg = database._es_pg(conn)
    ids = [int(r['id']) for r in registros]
    if not ids:
        return 0
    if len(ids) != len(set(ids)):
        raise ValueError('El lote contiene IDs repetidos; depuración cancelada.')
    originales = {int(r['id']): r for r in registros}
    total = 0
    try:
        if not pg:
            conn.execute('BEGIN IMMEDIATE')
        for inicio in range(0, len(ids), 400):
            lote = ids[inicio:inicio + 400]
            if pg:
                import psycopg2.extras
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute('SELECT id, ruta, hash_archivo, tamanio_bytes FROM videos '
                                'WHERE dispositivo_id = %s AND id = ANY(%s) FOR UPDATE',
                                (dispositivo_id, lote))
                    existentes = [dict(r) for r in cur.fetchall()]
            else:
                marcas = ','.join('?' for _ in lote)
                existentes = [dict(r) for r in conn.execute(
                    'SELECT id, ruta, hash_archivo, tamanio_bytes FROM videos '
                    f'WHERE dispositivo_id = ? AND id IN ({marcas})',
                    [dispositivo_id, *lote]).fetchall()]
            if len(existentes) != len(lote):
                raise RuntimeError('Los registros seleccionados cambiaron o desaparecieron; '
                                   'la transacción fue cancelada.')
            for r in existentes:
                anterior = originales[int(r['id'])]
                if (r['ruta'] != anterior['ruta'] or
                    str(r['hash_archivo'] or '') != str(anterior.get('hash_archivo') or '') or
                    int(r['tamanio_bytes'] or 0) != int(anterior.get('tamanio_bytes') or 0)):
                    raise RuntimeError('Un registro cambió después del respaldo; '
                                       'la transacción fue cancelada.')
            # Registrar eliminaciones y borrar en la MISMA transacción.
            for id_borrar in lote:
                anterior = originales[id_borrar]
                clave_u = anterior.get('clave_unica') or database._calcular_clave_unica(
                    dispositivo_id, str(anterior.get('ruta', '')))
                if clave_u:
                    registrar_eliminacion(conn, clave_u, confirmar=False)
            if pg:
                with conn.cursor() as cur:
                    cur.execute('DELETE FROM videos WHERE dispositivo_id = %s AND id = ANY(%s)',
                                (dispositivo_id, lote))
                    total += cur.rowcount
            else:
                cur = conn.execute(f'DELETE FROM videos WHERE dispositivo_id = ? '
                                   f'AND id IN ({marcas})', [dispositivo_id, *lote])
                total += cur.rowcount
        if total != len(ids):
            raise RuntimeError(f'Depuración inconsistente: previstos {len(ids)}, eliminados {total}.')
        conn.commit()
        return total
    except Exception:
        conn.rollback()
        raise


def _es_video(ruta: Path) -> bool:
    return ruta.suffix.lower() in EXTENSIONES_VIDEO


def _listar_videos(directorio: str, recursivo: bool = True) -> List[Path]:
    """Lista vídeos sin entrar en carpetas excluidas para este dispositivo."""
    from core.database import info_dispositivo_actual
    base = Path(directorio)
    if not base.is_dir():
        imprimir(f'[red][ERROR] No existe el directorio: {directorio}[/]')
        return []
    excluidas = cargar_exclusiones(DIR_SCRIPT, info_dispositivo_actual()['dispositivo_id'])
    if excluido(str(base), excluidas):
        imprimir(f'[yellow]Carpeta excluida del escaneo: {base}[/]')
        return []
    videos = []
    if recursivo:
        def avisar_error(exc):
            imprimir(f'[yellow]No se pudo abrir {rich_escape(str(exc))}[/]')
        for carpeta, subdirectorios, nombres in os.walk(base, followlinks=False, onerror=avisar_error):
            subdirectorios[:] = [n for n in subdirectorios
                                 if not excluido(os.path.join(carpeta, n), excluidas)]
            for nombre in nombres:
                ruta = Path(carpeta) / nombre
                if _es_video(ruta) and not excluido(str(ruta), excluidas):
                    videos.append(ruta)
    else:
        videos = [ruta for ruta in base.iterdir() if ruta.is_file() and _es_video(ruta)
                  and not excluido(str(ruta), excluidas)]
    return sorted(videos)