"""Localiza archivos registrados que ya no están en su ruta, buscando la
misma ruta relativa en otras raíces de almacenamiento.

Ejemplo: la base registra /storage/emulated/0/Videos/14-10-2026/a.mp4 y
ese archivo ya no existe. Si /storage/9F36-61B5/Videos/14-10-2026/a.mp4
existe con el mismo tamaño y el mismo SHA256 registrado, se propone el
cambio de ruta.

La fuente es la base de datos, no el reporte de verificación: el reporte
no incluye tamaño ni SHA256 para las carpetas inaccesibles, que es
justo donde suelen estar los archivos movidos.

Este módulo NO mueve ni modifica archivos del disco. Para aplicar los
cambios reutiliza core.cambiar_rutas, que respalda cada cambio y lo hace
en una transacción por archivo.

Notas de diseño importantes:

* Las raíces adicionales deben ser RAÍCES de almacenamiento
  (/storage/9F36-61B5), no subcarpetas (/storage/9F36-61B5/Videos).
  El módulo añade la ruta relativa completa desde la raíz; si se le
  pasa una subcarpeta, se detecta, se avisa y se corrige automáticamente.

* En Android moderno /storage no siempre se puede listar (scoped
  storage, FUSE lento, volumen aún no montado). raices_almacenamiento()
  no silencia esos errores: los devuelve como avisos para que el
  reporte los muestre en lugar de fallar en silencio con lista vacía.

* La búsqueda reporta progreso por faltante, por raíz y por chunk de
  hash, para que la UI no parezca colgada en volúmenes FUSE lentos.

* El mensaje de "sin coincidencia" incluye el motivo por raíz
  (no existe, no es archivo, tamaño distinto, excluida, no legible...)
  en lugar de una frase genérica que oculta la causa real.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat as stat_module
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import cambiar_rutas, inventario

Progreso = Optional[Callable[[str, int, int], None]]
ProgresoBytes = Optional[Callable[[str, int, int], None]]

_PATRON_STORAGE = re.compile(r'^/storage/(emulated/0|[^/]+)/(.*)$')
_PATRON_STORAGE_RAIZ = re.compile(r'^/storage/(emulated/0|[^/]+)/?$')

# Aviso cuando una raíz adicional es en realidad una subcarpeta.
_UMBRAL_STAT_LENTO = 2.0  # segundos


class InterrupcionBusqueda(Exception):
    """El usuario canceló la búsqueda o la aplicación."""
    pass


def _verificar_cancelacion(cancelar: Optional[threading.Event]) -> None:
    if cancelar is not None and cancelar.is_set():
        raise InterrupcionBusqueda('Operación cancelada por el usuario')


# ============================================================================
# Raíces de almacenamiento
# ============================================================================
def raices_almacenamiento(avisos: Optional[List[str]] = None) -> List[str]:
    """Memoria interna y tarjetas en Android; unidades en Windows.

    En Android: /storage/emulated/0 (interna) y /storage/XXXX-XXXX (tarjeta).
    En Windows: unidades montadas (C:\\, D:\\, etc).
    En Linux/MacOS: puntos de montaje en /mnt o /media.
    """
    raices: List[str] = []
    candidatas: List[str] = []

    def _anotar(msg: str) -> None:
        if avisos is not None:
            avisos.append(msg)

    if os.name == 'nt':
        import string
        for letra in string.ascii_uppercase:
            unidad = f'{letra}:\\'
            if os.path.isdir(unidad):
                candidatas.append(unidad)
    else:
        # Detectar si estamos en Android
        if ('ANDROID_ROOT' in os.environ or 'ANDROID_DATA' in os.environ or 
            'com.termux' in os.environ.get('PREFIX', '') or 
            'com.termux' in os.environ.get('PATH', '')):
            
            # 1) Memoria interna explícita Android
            interna = '/storage/emulated/0'
            if os.path.isdir(interna):
                candidatas.append(interna)

            # 2) Listar /storage propagando el error
            try:
                base = Path('/storage')
                if base.is_dir():
                    for hijo in sorted(base.iterdir()):
                        if hijo.name in ('self', 'emulated'):
                            continue
                        if os.path.isdir(str(hijo)):
                            candidatas.append(str(hijo))
                else:
                    _anotar('/storage no existe o no es accesible')
            except OSError as exc:
                _anotar(f'No se pudo listar /storage: {exc}')

            # 3) Vía alternativa Android
            try:
                media = Path('/mnt/media_rw')
                if os.path.isdir(str(media)):
                    for hijo in sorted(media.iterdir()):
                        if os.path.isdir(str(hijo)):
                            candidatas.append(str(hijo))
            except OSError as exc:
                _anotar(f'No se pudo listar /mnt/media_rw: {exc}')
                
        # En Linux / MacOS: Buscar en /mnt y /media
        else:
            for directorio_base in ('/mnt', '/media'):
                try:
                    base = Path(directorio_base)
                    if base.is_dir():
                        # Primero agregar la raíz principal
                        candidatas.append(str(base))
                        # Si hay subcarpetas de montaje, también agregarlas
                        for hijo in sorted(base.iterdir()):
                            if os.path.isdir(str(hijo)):
                                candidatas.append(str(hijo))
                except OSError as exc:
                    _anotar(f'No se pudo listar {directorio_base}: {exc}')

    # Deduplicar preservando orden.
    vistos: set = set()
    for r in candidatas:
        limpia = r.rstrip('/\\')
        if os.name == 'nt' and len(limpia) == 2 and limpia[1] == ':':
            limpia += '\\'
        if limpia and limpia not in vistos:
            vistos.add(limpia)
            raices.append(limpia)
    return raices


def separar_almacenamiento(ruta: str) -> Optional[Tuple[str, str]]:
    """Devuelve (raíz, ruta relativa) adaptándose a Windows, Linux y Android."""
    ruta_str = str(ruta).replace('\\', '/')

    # 1. Windows: C:/Videos/a.mp4 -> ('C:\\', 'Videos/a.mp4')
    m_win = re.match(r'^([a-zA-Z]:/)(.*)$', ruta_str)
    if m_win:
        relativa = m_win.group(2)
        if relativa:
            return m_win.group(1).replace('/', '\\'), relativa

    # 2. Android: /storage/emulated/0/Videos/a.mp4 -> ('/storage/emulated/0', 'Videos/a.mp4')
    m_android = re.match(r'^(/storage/(?:emulated/0|[^/]+))/(.*)$', ruta_str)
    if m_android:
        relativa = m_android.group(2)
        if relativa:
            return m_android.group(1), relativa

    # 3. Linux/MacOS montajes: /mnt/disco/Videos/a.mp4 -> ('/mnt/disco', 'Videos/a.mp4')
    m_linux = re.match(r'^(/mnt/[^/]+|/media/[^/]+)/(.*)$', ruta_str)
    if m_linux:
        relativa = m_linux.group(2)
        if relativa:
            return m_linux.group(1), relativa

    # 4. Fallback de Linux/Mac (ej. /home/usuario/Videos)
    m_home = re.match(r'^(/home/[^/]+)/(.*)$', ruta_str)
    if m_home:
        relativa = m_home.group(2)
        if relativa:
            return m_home.group(1), relativa

    return None


def _normalizar_raiz(raiz: str) -> Tuple[Optional[str], Optional[str]]:
    """Valida/normaliza una raíz adicional."""
    limpia = str(raiz).strip().rstrip('/\\')

    if os.name == 'nt' and len(limpia) == 2 and limpia[1] == ':':
        limpia += '\\'

    if not limpia:
        return None, 'raíz vacía'

    separado = separar_almacenamiento(limpia)
    if separado is None:
        return limpia, None

    base, relativa = separado
    if relativa:
        return base, (f'raíz ajustada: {limpia} -> {base} '
                      f'(el módulo añade "{relativa}/..." solo)')
    return base, None


# ============================================================================
# Faltantes desde la base de datos
# ============================================================================
def faltantes_desde_base(conn: Any, dispositivo_id: str,
                         excluidas: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """Registros del dispositivo cuyo archivo ya no existe en su ruta.

    Deduplica por (ruta, sha256) para evitar procesar dos veces el mismo
    archivo si la base tiene registros repetidos.
    """
    faltantes: List[Dict[str, Any]] = []
    vistos: set = set()
    for r in inventario.registros_dispositivo(conn, dispositivo_id):
        ruta = str(r.get('ruta') or '')
        if not ruta or inventario.excluido(ruta, excluidas):
            continue
        sha = str(r.get('hash_archivo') or '').strip().lower()
        clave = (ruta, sha)
        if clave in vistos:
            continue
        vistos.add(clave)

        # os.path.lexists evita seguir symlinks rotos; isfile distingue
        # "no existe" de "es un directorio". Un directorio con el mismo
        # nombre no cuenta como archivo presente.
        if os.path.isfile(ruta):
            continue

        tamanio = r.get('tamanio_bytes')
        faltantes.append({
            'ruta': ruta,
            'nombre': str(r.get('nombre') or os.path.basename(ruta)),
            'dispositivo_id': dispositivo_id,
            'sha256': sha,
            # None = desconocido; 0 = cero real. Permite no filtrar por
            # tamaño cuando la base no lo sabe, sin confundir con vacío.
            'tamanio_bytes': None if tamanio in (None, '') else int(tamanio),
        })
    faltantes.sort(key=lambda f: f['ruta'].casefold())
    return faltantes


# ============================================================================
# Hash
# ============================================================================
def _sha256_archivo(ruta: str,
                    cancelar: Optional[threading.Event] = None,
                    chunk: int = 1024 * 1024,
                    progreso_bytes: ProgresoBytes = None) -> str:
    sha = hashlib.sha256()
    try:
        total = os.stat(ruta).st_size
    except OSError:
        total = 0
    leidos = 0
    with open(ruta, 'rb') as archivo:
        while True:
            _verificar_cancelacion(cancelar)
            datos = archivo.read(chunk)
            if not datos:
                break
            sha.update(datos)
            leidos += len(datos)
            if progreso_bytes is not None:
                try:
                    progreso_bytes(ruta, leidos, total)
                except Exception:
                    # Un callback de UI no debe romper la búsqueda.
                    pass
    return sha.hexdigest()


# ============================================================================
# Búsqueda
# ============================================================================
def buscar_reubicaciones(
    faltantes: Sequence[Dict[str, Any]],
    raices_extra: Sequence[str] = (),
    excluidas: Sequence[str] = (),
    progreso: Progreso = None,
    progreso_bytes: ProgresoBytes = None,
    cancelar: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Busca, para cada faltante, la misma ruta relativa en otras raíces.

    Un candidato se acepta como coincidencia solo si su tamaño y su SHA256
    coinciden con los registrados. Si la base no tiene SHA256, el hallazgo
    queda como probable y no se aplica automáticamente.

    `raices_extra` debe contener RAÍCES de almacenamiento, no subcarpetas.
    Si se pasa una subcarpeta (/storage/9F36-61B5/Videos), se corrige
    automáticamente y se anota en `avisos`.
    """
    avisos: List[str] = []
    raices: List[str] = []

    for raiz in raices_almacenamiento(avisos):
        if raiz and raiz not in raices:
            raices.append(raiz)

    for raiz in raices_extra:
        valida, aviso = _normalizar_raiz(str(raiz))
        if aviso:
            avisos.append(aviso)
        if valida is None:
            continue
        if valida not in raices:
            raices.append(valida)

    if not raices:
        avisos.append(
            'No se detectó ninguna raíz de almacenamiento. '
            'Añade raices_extra o revisa los permisos de almacenamiento.'
        )

    cache_sha: Dict[str, str] = {}
    coincidencias: List[Dict[str, Any]] = []
    probables: List[Dict[str, Any]] = []
    sin_coincidencia: List[Dict[str, Any]] = []
    no_legibles: List[str] = []
    total = len(faltantes)

    for indice, f in enumerate(faltantes, start=1):
        _verificar_cancelacion(cancelar)

        base = {
            'ruta_anterior': f['ruta'],
            'nombre': f['nombre'],
            'dispositivo_id': f['dispositivo_id'],
            'sha256': f['sha256'],
            'tamanio_bytes': f['tamanio_bytes'],
        }

        separado = separar_almacenamiento(f['ruta'])
        if separado is None:
            sin_coincidencia.append({
                **base, 'estado': 'sin_coincidencia',
                'motivo': ('La ruta no coincide con un patrón estándar de almacenamiento '
                           '(Windows, /storage, /mnt, /home); no se puede separar la raíz.'),
            })
            continue

        raiz_origen, relativa = separado
        hallados: List[str] = []
        motivos_raiz: List[str] = []

        for raiz in raices:
            _verificar_cancelacion(cancelar)

            if inventario.normalizar(raiz) == inventario.normalizar(raiz_origen):
                continue

            if inventario.es_windows(raiz):
                rel_win = relativa.replace('/', '\\').lstrip('\\')
                raiz_limpia = raiz.rstrip('\\') + '\\'
                candidato = os.path.join(raiz_limpia, rel_win)
            else:
                candidato = posixpath.join(raiz, relativa)

            if progreso is not None:
                try:
                    progreso(f'Buscando {indice}/{total}: {raiz}',
                             indice, total)
                except Exception:
                    pass

            if inventario.excluido(candidato, excluidas):
                motivos_raiz.append(f'{raiz}: excluida por configuración')
                continue

            # Una sola stat (evita el doble stat de isfile + stat en FUSE).
            t0 = time.monotonic()
            try:
                st = os.stat(candidato)
            except FileNotFoundError:
                motivos_raiz.append(f'{raiz}: no existe')
                continue
            except OSError as exc:
                motivos_raiz.append(f'{raiz}: no legible ({exc})')
                no_legibles.append(f'{candidato}: {exc}')
                continue
            dt = time.monotonic() - t0
            if dt > _UMBRAL_STAT_LENTO:
                no_legibles.append(
                    f'{candidato}: stat lento ({dt:.1f}s)'
                )

            if not stat_module.S_ISREG(st.st_mode):
                motivos_raiz.append(f'{raiz}: existe pero no es archivo')
                continue

            tamanio_real = st.st_size
            if f['tamanio_bytes'] is not None and tamanio_real != f['tamanio_bytes']:
                motivos_raiz.append(
                    f'{raiz}: tamaño {tamanio_real} != {f["tamanio_bytes"]}'
                )
                continue

            hallados.append(candidato)

        if not hallados:
            motivo = 'No se halló en ninguna raíz'
            if motivos_raiz:
                motivo += ': ' + '; '.join(motivos_raiz)
            elif not raices:
                motivo = ('No se detectó ninguna raíz de almacenamiento; '
                          'pasa raices_extra o revisa permisos')
            sin_coincidencia.append({
                **base, 'estado': 'sin_coincidencia', 'motivo': motivo,
            })
            continue

        if not f['sha256']:
            probables.append({
                **base, 'rutas_nuevas': hallados, 'estado': 'probable',
                'motivo': 'Mismo tamaño; la base no tiene SHA256 para confirmar',
            })
            continue

        confirmados: List[str] = []
        for candidato in hallados:
            _verificar_cancelacion(cancelar)

            sha_candidato = cache_sha.get(candidato)
            if sha_candidato is None:
                if progreso is not None:
                    try:
                        progreso(f'Hasheando {indice}/{total}: {candidato}',
                                 indice, total)
                    except Exception:
                        pass
                try:
                    sha_candidato = _sha256_archivo(
                        candidato, cancelar, progreso_bytes=progreso_bytes,
                    )
                except OSError as exc:
                    no_legibles.append(f'{candidato}: {exc}')
                    continue
                cache_sha[candidato] = sha_candidato

            if sha_candidato == f['sha256']:
                confirmados.append(candidato)

        if len(confirmados) == 1:
            coincidencias.append({**base, 'ruta_nueva': confirmados[0],
                                  'estado': 'coincidencia', 'motivo': ''})
        elif len(confirmados) > 1:
            probables.append({
                **base, 'rutas_nuevas': sorted(confirmados), 'estado': 'ambiguo',
                'motivo': 'Varias copias idénticas; elige manualmente',
            })
        else:
            sin_coincidencia.append({
                **base, 'estado': 'sin_coincidencia',
                'motivo': ('Hay archivo con el mismo tamaño, pero SHA256 '
                           'distinto'),
            })

    return {
        'fecha_utc6': inventario.fecha_texto(),
        'dispositivo_id': faltantes[0]['dispositivo_id'] if faltantes else '',
        'total_faltantes': total,
        'raices_revisadas': raices,
        'avisos': avisos,
        'coincidencias': coincidencias,
        'probables': probables,
        'sin_coincidencia': sin_coincidencia,
        'no_legibles': no_legibles,
    }


# ============================================================================
# Reporte
# ============================================================================
def _csv_escribir_local(destino: Path, encabezado: Sequence[str],
                        filas: Sequence[Dict[str, Any]]) -> None:
    """Fallback si inventario._csv_escribir no está disponible."""
    import csv
    with destino.open('w', encoding='utf-8', newline='') as f:
        escritor = csv.DictWriter(f, fieldnames=list(encabezado))
        escritor.writeheader()
        for fila in filas:
            escritor.writerow({k: fila.get(k, '') for k in encabezado})


def _destinos_reporte(base: Path) -> Dict[str, Path]:
    """Usa inventario._destinos si existe; si no, un fallback local."""
    try:
        return inventario._destinos(base, 'ubicaciones')
    except Exception:
        base.mkdir(parents=True, exist_ok=True)
        sello = inventario.fecha_texto()
        return {
            'json': base / f'ubicaciones_{sello}.json',
            'txt': base / f'ubicaciones_{sello}.txt',
            'csv': base / f'ubicaciones_{sello}.csv',
        }


def reportar_ubicaciones(base: Path, resultado: Dict[str, Any]) -> Dict[str, Path]:
    """Escribe JSON, TXT y CSV del resultado de la búsqueda."""
    destinos = _destinos_reporte(base)

    with destinos['json'].open('w', encoding='utf-8') as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)

    with destinos['txt'].open('w', encoding='utf-8') as f:
        f.write(f'BÚSQUEDA DE UBICACIÓN — {resultado["fecha_utc6"]}\n')
        f.write(f'Dispositivo: {resultado["dispositivo_id"]}\n')
        f.write('Criterio: misma ruta relativa en otra raíz, con mismo '
                'tamaño y mismo SHA256.\n')
        f.write(f'Registros con archivo ausente: {resultado["total_faltantes"]}\n')
        f.write(f'Coincidencias confirmadas: {len(resultado["coincidencias"])}\n')
        f.write(f'Probables o ambiguas (no se aplican solas): '
                f'{len(resultado["probables"])}\n')
        f.write(f'Sin coincidencia: {len(resultado["sin_coincidencia"])}\n')

        f.write('\nRAÍCES REVISADAS\n')
        for raiz in resultado['raices_revisadas']:
            f.write(f'- {raiz}\n')
        if not resultado['raices_revisadas']:
            f.write('- (ninguna)\n')

        if resultado.get('avisos'):
            f.write(f'\nAVISOS ({len(resultado["avisos"])})\n')
            for aviso in resultado['avisos']:
                f.write(f'- {aviso}\n')

        f.write(f'\nCOINCIDENCIAS CONFIRMADAS '
                f'({len(resultado["coincidencias"])})\n')
        for c in resultado['coincidencias']:
            f.write(f'- {c["ruta_anterior"]}\n    -> {c["ruta_nueva"]}\n')

        f.write(f'\nPROBABLES O AMBIGUAS ({len(resultado["probables"])})\n')
        for p in resultado['probables']:
            f.write(f'- {p["ruta_anterior"]} | {p["motivo"]}\n')
            for nueva in p['rutas_nuevas']:
                f.write(f'    -> {nueva}\n')

        f.write(f'\nSIN COINCIDENCIA '
                f'({len(resultado["sin_coincidencia"])})\n')
        for s in resultado['sin_coincidencia']:
            f.write(f'- {s["ruta_anterior"]} | {s["motivo"]}\n')

        if resultado['no_legibles']:
            f.write(f'\nNO LEGIBLES ({len(resultado["no_legibles"])})\n')
            for texto in resultado['no_legibles']:
                f.write(f'- {texto}\n')

    encabezado = ('estado', 'ruta_anterior', 'ruta_nueva', 'nombre',
                  'dispositivo_id', 'sha256', 'motivo')
    filas: List[Dict[str, Any]] = []
    for c in resultado['coincidencias']:
        filas.append({'estado': 'coincidencia',
                      'ruta_anterior': c['ruta_anterior'],
                      'ruta_nueva': c['ruta_nueva'],
                      'nombre': c['nombre'],
                      'dispositivo_id': c['dispositivo_id'],
                      'sha256': c['sha256'], 'motivo': ''})
    for p in resultado['probables']:
        for nueva in p['rutas_nuevas']:
            filas.append({'estado': p['estado'],
                          'ruta_anterior': p['ruta_anterior'],
                          'ruta_nueva': nueva,
                          'nombre': p['nombre'],
                          'dispositivo_id': p['dispositivo_id'],
                          'sha256': p['sha256'], 'motivo': p['motivo']})
    for s in resultado['sin_coincidencia']:
        filas.append({'estado': 'sin_coincidencia',
                      'ruta_anterior': s['ruta_anterior'],
                      'ruta_nueva': '', 'nombre': s['nombre'],
                      'dispositivo_id': s['dispositivo_id'],
                      'sha256': s['sha256'], 'motivo': s['motivo']})

    try:
        inventario._csv_escribir(destinos['csv'], encabezado, filas)
    except Exception:
        _csv_escribir_local(destinos['csv'], encabezado, filas)
    return destinos


# ============================================================================
# Aplicación
# ============================================================================
def _validar_coincidencia(c: Dict[str, Any]) -> Optional[str]:
    """Devuelve un motivo de rechazo, o None si la coincidencia es aplicable.

    Evita aplicar reportes editados a mano con rutas inexistentes o con
    hash que ya no coincide.
    """
    anterior = str(c.get('ruta_anterior') or '')
    nueva = str(c.get('ruta_nueva') or '')
    if not anterior or not nueva:
        return 'falta ruta_anterior o ruta_nueva'
    if not os.path.isfile(nueva):
        return f'la ruta nueva no existe o no es archivo: {nueva}'
    sha = str(c.get('sha256') or '').strip().lower()
    if sha:
        try:
            real = _sha256_archivo(nueva)
        except OSError as exc:
            return f'no se pudo leer la ruta nueva: {exc}'
        if real != sha:
            return 'el SHA256 de la ruta nueva no coincide con el registrado'
    return None


def cargar_coincidencias(ruta_reporte: str,
                         validar: bool = True) -> List[Dict[str, Any]]:
    """Devuelve solo las coincidencias confirmadas de un reporte de búsqueda.

    Si `validar` es True (por defecto), comprueba que cada ruta nueva exista,
    sea archivo y (si hay SHA256) coincida antes de devolverla. Las que no
    pasan la validación se descartan silenciosamente aquí; para ver el
    motivo usa cargar_coincidencias_con_errores.
    """
    validas, _ = cargar_coincidencias_con_errores(ruta_reporte,
                                                  validar=validar)
    return validas


def cargar_coincidencias_con_errores(
    ruta_reporte: str, validar: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Como cargar_coincidencias, pero también devuelve las rechazadas.

    Devuelve (validas, rechazadas), donde cada rechazada es
    {**coincidencia, 'error': motivo}.
    """
    with Path(ruta_reporte).open(encoding='utf-8-sig') as f:
        datos = json.load(f)
    if not isinstance(datos.get('coincidencias'), list):
        raise ValueError('El archivo no contiene la lista "coincidencias".')

    validas: List[Dict[str, Any]] = []
    rechazadas: List[Dict[str, Any]] = []
    for c in datos['coincidencias']:
        if not c.get('ruta_anterior') or not c.get('ruta_nueva'):
            rechazadas.append({**c, 'error': 'faltan rutas'})
            continue
        if validar:
            motivo = _validar_coincidencia(c)
            if motivo is not None:
                rechazadas.append({**c, 'error': motivo})
                continue
        validas.append(c)
    return validas, rechazadas


def aplicar_reubicaciones(
    conn: Any,
    dispositivo_id: str,
    dispositivo: str,
    coincidencias: Sequence[Dict[str, Any]],
    reportes: Path,
    progreso: Progreso = None,
    cancelar: Optional[threading.Event] = None,
    validar: bool = False,
) -> Dict[str, List[Dict[str, Any]]]:
    """Aplica cada coincidencia con cambiar_rutas.ejecutar (archivo exacto).

    Cada archivo es una transacción independiente con su propio respaldo.
    Si uno falla, por ejemplo porque su ruta nueva ya está registrada, se
    anota en 'fallidas' y el proceso sigue con el siguiente.

    Si `validar` es True, se comprueba antes de cada cambio que la ruta
    nueva exista y (si hay SHA256) coincida. Útil si el reporte pudo
    editarse a mano. Por defecto False para no rehashear archivos grandes
    que ya pasaron por la fase de búsqueda.
    """
    aplicadas: List[Dict[str, Any]] = []
    fallidas: List[Dict[str, Any]] = []
    total = len(coincidencias)

    for indice, c in enumerate(coincidencias, start=1):
        _verificar_cancelacion(cancelar)
        if progreso is not None:
            try:
                progreso('Aplicando cambios', indice, total)
            except Exception:
                pass

        if validar:
            motivo = _validar_coincidencia(c)
            if motivo is not None:
                fallidas.append({**c, 'error': motivo})
                continue

        try:
            cambiar_rutas.ejecutar(
                conn, dispositivo_id, dispositivo,
                c['ruta_anterior'], c['ruta_nueva'],
                carpeta=False, reportes=reportes,
            )
            aplicadas.append(c)
        except InterrupcionBusqueda:
            raise
        except Exception as exc:
            fallidas.append({**c, 'error': str(exc)})

    return {'aplicadas': aplicadas, 'fallidas': fallidas}
