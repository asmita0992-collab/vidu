"""Localiza vídeos movidos en la misma unidad o entre unidades distintas.

Ejemplo: la base registra /storage/9F36-61B5/Videos/7-10-2026/a.mp4,
pero el archivo fue movido a /storage/9F36-61B5/Videos/a.mp4.
La búsqueda recorre subcarpetas de esa misma SD y de las otras raíces,
filtra por nombre y tamaño, y confirma con SHA256 antes de proponer cambios.

La fuente es la base de datos, no el reporte de verificación: el reporte
no incluye tamaño ni SHA256 para las carpetas inaccesibles, que es
justo donde suelen estar los archivos movidos.

Este módulo NO mueve ni modifica archivos del disco. La opción de aplicar
rutas utiliza el reporte SHA256 producido por la búsqueda y cambia únicamente
las rutas del inventario SQLite, en una sola transacción por lote. No repite
el cálculo SHA256 ni altera huellas o vídeos físicos.

Notas de diseño importantes:

* Las raíces adicionales deben ser RAÍCES de almacenamiento
  (/storage/9F36-61B5), no subcarpetas (/storage/9F36-61B5/Videos).
  Se comprueba la ruta relativa original y después se buscan coincidencias
  recursivas dentro de todas las raíces, incluida la de origen.

* En Android moderno /storage no siempre se puede listar (scoped
  storage, FUSE lento, volumen aún no montado). raices_almacenamiento()
  no silencia esos errores: los devuelve como avisos para que el
  reporte los muestre en lugar de fallar en silencio con lista vacía.

* La búsqueda reporta progreso por faltante, por raíz y por chunk de
  hash, para que la UI no parezca colgada en volúmenes FUSE lentos.

* Si faltan permisos, el llamador puede solicitar explícitamente root.
  Las consultas privilegiadas son de solo lectura; los archivos que no
  puedan leerse normalmente se informan como probables no aplicables.
"""
from __future__ import annotations

import hashlib
import json
import os
import ntpath
import shlex
import subprocess
import posixpath
import re
import stat as stat_module
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import cambiar_rutas, inventario, plataforma

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
def raices_almacenamiento(avisos: Optional[List[str]] = None,
                         sugeridas: Sequence[str] = ()) -> List[str]:
    """Detecta raíces de discos y memorias, incluidas SD de Android/Termux.

    Las raíces explícitas tienen prioridad y no se descartan por un fallo
    de permisos. En Android delega en plataforma.py, que lee mountinfo,
    variables de entorno y accesos de Termux además de /storage.
    """
    candidatas: List[str] = list(sugeridas)
    if os.name == 'nt':
        import string
        for letra in string.ascii_uppercase:
            volumen = f'{letra}:\\'
            if os.path.isdir(volumen):
                candidatas.append(volumen)
    elif plataforma.es_android():
        candidatas.extend(plataforma.raices_android_almacenamiento(
            avisos, sugeridas=sugeridas,
        ))
    else:
        for base in ('/mnt', '/media'):
            try:
                if os.path.isdir(base):
                    for nombre in sorted(os.listdir(base)):
                        ruta = os.path.join(base, nombre)
                        if os.path.isdir(ruta):
                            candidatas.append(ruta)
                            # /media/usuario/disco es habitual en Xubuntu.
                            if base == '/media':
                                try:
                                    for sub in sorted(os.listdir(ruta)):
                                        subruta = os.path.join(ruta, sub)
                                        if os.path.ismount(subruta):
                                            candidatas.append(subruta)
                                except OSError:
                                    pass
            except OSError as exc:
                if avisos is not None:
                    avisos.append(f'No se pudo listar {base}: {exc}')
    resultado = []
    vistos = set()
    for candidata in candidatas:
        candidata = os.path.normpath(str(candidata))
        clave = os.path.normcase(candidata)
        if candidata not in ('', '.') and clave not in vistos:
            vistos.add(clave)
            resultado.append(candidata)
    return resultado


def clave_raiz(raiz: str) -> str:
    """Normaliza raíces para exclusiones, incluso rutas Windows desde pruebas Linux."""
    valor = str(raiz).strip()
    if re.match(r'^[A-Za-z]:', valor):
        return ntpath.normcase(ntpath.normpath(valor))
    return os.path.normcase(os.path.normpath(valor))


def filtrar_raices(raices: Sequence[str], excluidas: Sequence[str]) -> List[str]:
    """Excluye solo los montajes escogidos; nunca los reintroduce al buscar."""
    bloqueadas = {clave_raiz(raiz) for raiz in excluidas}
    return [raiz for raiz in raices if clave_raiz(raiz) not in bloqueadas]


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
def _indexar_archivos(
    raices: Sequence[str], objetivos: Sequence[Dict[str, Any]],
    avisos: List[str], cancelar: Optional[threading.Event],
    progreso: Progreso,
) -> Dict[str, List[str]]:
    """Explora TODAS las subcarpetas, incluida la raíz del vídeo desaparecido.

    Indexa exclusivamente nombres solicitados; no lee ni calcula huellas de
    otros vídeos. Evita el coste de repetir os.walk por cada registro.
    """
    buscados = {str(f['nombre'] or os.path.basename(f['ruta'])).casefold()
                for f in objetivos}
    encontrados: Dict[str, List[str]] = {nombre: [] for nombre in buscados}
    for numero, raiz in enumerate(raices, 1):
        _verificar_cancelacion(cancelar)
        # El porcentaje representa raíces terminadas, no vídeos hallados:
        # hallar un vídeo no equivale a terminar una unidad.
        if progreso:
            progreso(f'Raíz {numero}/{len(raices)}: {raiz} || Iniciando recorrido',
                     numero - 1, len(raices))
        if not os.path.isdir(raiz):
            avisos.append(f'Raíz inexistente o inaccesible: {raiz}')
            continue
        errores_esta_raiz = [0]
        def error_ruta(error: OSError) -> None:
            errores_esta_raiz[0] += 1
            if errores_esta_raiz[0] <= 5:
                avisos.append(f'Carpeta no legible: {error.filename}: {error.strerror}')
        carpetas_visitadas = archivos_visitados = candidatos_vistos = 0
        ultimo_aviso = 0.0
        try:
            for carpeta, directorios, archivos in os.walk(raiz, onerror=error_ruta,
                                                          followlinks=False):
                _verificar_cancelacion(cancelar)
                carpetas_visitadas += 1
                archivos_visitados += len(archivos)
                # No seguir enlaces de directorios, que pueden producir ciclos.
                directorios[:] = [d for d in directorios
                                  if not os.path.islink(os.path.join(carpeta, d))]
                for nombre in archivos:
                    clave = nombre.casefold()
                    if clave in buscados:
                        candidatos_vistos += 1
                        encontrados[clave].append(os.path.join(carpeta, nombre))
                # Informar durante el recorrido, no únicamente al terminar el disco.
                # Limitar a 4 actualizaciones por segundo para no ralentizar os.walk.
                ahora = time.monotonic()
                if progreso and (ahora - ultimo_aviso >= 0.25):
                    progreso(f'{carpeta} || Carpetas: {carpetas_visitadas:,} · '
                             f'Archivos: {archivos_visitados:,} · Nombres: {candidatos_vistos:,}',
                             numero - 1, len(raices))
                    ultimo_aviso = ahora
        except (OSError, PermissionError) as exc:
            avisos.append(f'No se pudo recorrer {raiz}: {exc}')
        if progreso:
            progreso(f'{raiz} || Raíz terminada · Carpetas: {carpetas_visitadas:,} · '
                     f'Archivos: {archivos_visitados:,} · Nombres: {candidatos_vistos:,}',
                     numero, len(raices))
        if errores_esta_raiz[0] > 5:
            avisos.append(f'{raiz}: {errores_esta_raiz[0] - 5} carpetas inaccesibles más')
    return encontrados


def _buscar_root_solo_lectura(
    raices: Sequence[str], objetivos: Sequence[Dict[str, Any]],
    comando_root: Sequence[str], avisos: List[str],
    cancelar: Optional[threading.Event], progreso: Progreso,
) -> Dict[str, List[str]]:
    """Enumera nombres usando find mediante su/sudo, sin escribir en disco.

    Solo se invoca tras autorización del usuario y verificación de privilegios.
    No obtiene contraseñas, no modifica permisos ni ejecuta comandos de borrado.
    """
    buscados = {str(f['nombre'] or os.path.basename(f['ruta'])).casefold()
                for f in objetivos}
    encontrados: Dict[str, List[str]] = {nombre: [] for nombre in buscados}
    for numero, raiz in enumerate(raices, 1):
        _verificar_cancelacion(cancelar)
        if progreso:
            progreso(f'Lectura elevada ({numero}/{len(raices)}): {raiz}', 0,
                     len(objetivos))
        # No permitir que un error convierta una raíz vacía en una búsqueda /.
        if not os.path.isabs(raiz) or raiz in ('/', '/storage', '/mnt', '/media'):
            avisos.append(f'Se omitió raíz demasiado amplia para root: {raiz}')
            continue
        comando = f'find {shlex.quote(raiz)} -type f -print0'
        try:
            r = subprocess.run([*comando_root, comando], capture_output=True,
                               timeout=180, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            avisos.append(f'Lectura elevada fallida en {raiz}: {exc}')
            continue
        if r.returncode != 0:
            mensaje = r.stderr.decode('utf-8', 'replace').strip()[:250]
            avisos.append(f'Lectura elevada no disponible en {raiz}: {mensaje}')
        for linea in r.stdout.split(b'\0'):
            if linea:
                ruta = os.fsdecode(linea)
                nombre = os.path.basename(ruta).casefold()
                if nombre in buscados:
                    encontrados[nombre].append(ruta)
    return encontrados


def _stat_candidato(ruta: str, comando_root: Optional[Sequence[str]]) -> Tuple[Optional[int], bool, str]:
    """Devuelve (tamaño, requiere_root, error), nunca altera archivos."""
    try:
        dato = os.stat(ruta)
        if not stat_module.S_ISREG(dato.st_mode):
            return None, False, 'no es un archivo regular'
        return int(dato.st_size), False, ''
    except OSError as exc:
        if comando_root is None:
            return None, False, str(exc)
    comando = f'stat -c %s {shlex.quote(ruta)}'
    try:
        r = subprocess.run([*comando_root, comando], capture_output=True,
                           text=True, timeout=20, check=False)
        if r.returncode == 0:
            return int(r.stdout.strip()), True, ''
        return None, True, r.stderr.strip()[:200] or 'falló stat con root'
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return None, True, str(exc)


def _hash_con_root(ruta: str, comando_root: Sequence[str]) -> str:
    """Lee SHA256 del archivo con privilegios, sin modificar su contenido."""
    r = subprocess.run([*comando_root, f'sha256sum {shlex.quote(ruta)}'],
                       capture_output=True, text=True, timeout=240, check=False)
    if r.returncode != 0:
        raise OSError(r.stderr.strip() or 'Falló sha256sum con root')
    sha = r.stdout.split(maxsplit=1)[0].lower() if r.stdout.strip() else ''
    if not re.fullmatch(r'[a-f0-9]{64}', sha):
        raise OSError('Salida de sha256sum con formato no válido')
    return sha


def buscar_reubicaciones(
    faltantes: Sequence[Dict[str, Any]],
    raices_extra: Sequence[str] = (),
    excluidas: Sequence[str] = (),
    progreso: Progreso = None,
    progreso_bytes: ProgresoBytes = None,
    cancelar: Optional[threading.Event] = None,
    comando_root: Optional[Sequence[str]] = None,
    raices_excluidas: Sequence[str] = (),
) -> Dict[str, Any]:
    """Busca rutas cambiadas dentro de la MISMA unidad y en otras unidades.

    1) Prueba las rutas relativas exactas en las raíces seleccionadas.
    2) Recorre subcarpetas de todas las raíces, incluida la de origen.
    3) Filtra por nombre y tamaño y confirma por SHA256 si existe.

    Si se autoriza root, se usa solo para lectura; los hallazgos inaccesibles
    a VIDUP se informan como probables y NO se aplican automáticamente.
    """
    avisos: List[str] = []
    extras: List[str] = []
    for raiz in raices_extra:
        valida, aviso = _normalizar_raiz(str(raiz))
        if aviso:
            avisos.append(aviso)
        if valida is not None:
            extras.append(valida)
    # Añadir siempre la raíz original: antes se omitía y no buscaba
    # archivos movidos entre carpetas dentro de la misma tarjeta SD.
    for f in faltantes:
        separado = separar_almacenamiento(f['ruta'])
        if separado:
            extras.append(separado[0])
    raices = filtrar_raices(raices_almacenamiento(avisos, sugeridas=extras),
                            raices_excluidas)
    if raices_excluidas:
        avisos.append(f'Raíces excluidas por el usuario: {", ".join(raices_excluidas)}')
    if not raices:
        avisos.append('No quedan raíces para buscar; revisa exclusiones y permisos.')

    indice = _indexar_archivos(raices, faltantes, avisos, cancelar, progreso)
    if comando_root is not None:
        con_root = _buscar_root_solo_lectura(raices, faltantes, comando_root,
                                           avisos, cancelar, progreso)
        for nombre, rutas in con_root.items():
            indice.setdefault(nombre, []).extend(rutas)

    coincidencias: List[Dict[str, Any]] = []
    probables: List[Dict[str, Any]] = []
    sin_coincidencia: List[Dict[str, Any]] = []
    no_legibles: List[str] = []
    cache_hash: Dict[str, str] = {}
    total = len(faltantes)
    for i, f in enumerate(faltantes, 1):
        _verificar_cancelacion(cancelar)
        anterior = str(f['ruta'])
        nombre = str(f['nombre'] or os.path.basename(anterior))
        base = {
            'ruta_anterior': anterior, 'nombre': nombre,
            'dispositivo_id': f['dispositivo_id'], 'sha256': f['sha256'],
            'tamanio_bytes': f['tamanio_bytes'],
        }
        separado = separar_almacenamiento(anterior)
        # La comprobación directa es muy barata y conserva la búsqueda
        # histórica de rutas relativas entre distintos volúmenes.
        candidatos: List[str] = list(indice.get(nombre.casefold(), []))
        if separado:
            _, relativa = separado
            for raiz in raices:
                if inventario.es_windows(raiz):
                    destino = os.path.join(raiz, relativa.replace('/', '\\'))
                else:
                    destino = posixpath.join(raiz, relativa)
                candidatos.append(destino)
        candidatos_unicos: List[str] = []
        vistos = set()
        for ruta in candidatos:
            clave = os.path.normcase(os.path.normpath(ruta))
            if clave in vistos or ruta == anterior or inventario.excluido(ruta, excluidas):
                continue
            vistos.add(clave)
            candidatos_unicos.append(ruta)

        hallados: List[Tuple[str, bool]] = []
        errores = []
        for ruta in candidatos_unicos:
            _verificar_cancelacion(cancelar)
            tam, requiere_root, error = _stat_candidato(ruta, comando_root)
            if tam is None:
                if error and ('Permission' in error or 'denied' in error.lower()
                              or 'Permiso' in error):
                    no_legibles.append(f'{ruta}: {error}')
                continue
            if f['tamanio_bytes'] is not None and int(f['tamanio_bytes']) != tam:
                errores.append(f'{ruta}: tamaño {tam} distinto del registrado')
                continue
            hallados.append((ruta, requiere_root))
        if progreso:
            progreso(f'Comprobando {i}/{total}: {nombre}', i, total)
        if not hallados:
            motivo = ('No se encontró en las subcarpetas de las raíces exploradas. '
                      'Comprueba permisos y que la SD esté montada.')
            if errores:
                motivo += ' ' + '; '.join(errores[:3])
            sin_coincidencia.append({**base, 'estado': 'sin_coincidencia',
                                     'motivo': motivo})
            continue
        if not f['sha256']:
            probables.append({**base, 'rutas_nuevas': [r for r, _ in hallados],
                              'estado': 'probable',
                              'motivo': 'Tamaño y nombre coinciden; la base no guarda SHA256'})
            continue

        confirmados: List[Tuple[str, bool]] = []
        for ruta, requiere_root in hallados:
            _verificar_cancelacion(cancelar)
            sha = cache_hash.get(ruta)
            if sha is None:
                if progreso:
                    progreso(f'Calculando SHA256 {i}/{total}: {ruta}', i, total)
                try:
                    if requiere_root:
                        sha = _hash_con_root(ruta, comando_root or [])
                    else:
                        sha = _sha256_archivo(ruta, cancelar,
                                             progreso_bytes=progreso_bytes)
                    cache_hash[ruta] = sha
                except (OSError, subprocess.TimeoutExpired) as exc:
                    no_legibles.append(f'{ruta}: {exc}')
                    continue
            if sha.casefold() == str(f['sha256']).casefold():
                confirmados.append((ruta, requiere_root))

        # Eliminar alias del mismo archivo para no generar ambigüedades falsas.
        unicos: List[Tuple[str, bool]] = []
        identidades = set()
        for ruta, requiere_root in confirmados:
            try:
                dato = os.stat(ruta)
                identidad = ('inode', dato.st_dev, dato.st_ino)
            except OSError:
                identidad = ('ruta', os.path.realpath(ruta))
            if identidad not in identidades:
                identidades.add(identidad)
                unicos.append((ruta, requiere_root))
        accesibles = [ruta for ruta, requiere_root in unicos if not requiere_root]
        if len(unicos) == 1 and len(accesibles) == 1:
            coincidencias.append({**base, 'ruta_nueva': accesibles[0],
                                  'estado': 'coincidencia', 'motivo': ''})
        elif unicos:
            necesita_root = any(requiere_root for _, requiere_root in unicos)
            probables.append({**base,
                              'rutas_nuevas': [ruta for ruta, _ in unicos],
                              'estado': 'acceso_root' if necesita_root else 'ambiguo',
                              'motivo': ('El archivo se encontró con root, pero VIDUP no puede '
                                         'leerlo sin privilegios; no se aplicará automáticamente'
                                         if necesita_root else
                                         'Varias copias idénticas; elige manualmente')})
        else:
            sin_coincidencia.append({**base, 'estado': 'sin_coincidencia',
                                     'motivo': 'Hay candidatos de igual nombre/tamaño pero SHA256 distinto'})
    return {
        'fecha_utc6': inventario.fecha_texto(),
        'dispositivo_id': faltantes[0]['dispositivo_id'] if faltantes else '',
        'total_faltantes': total, 'raices_revisadas': raices,
        'avisos': avisos, 'coincidencias': coincidencias,
        'probables': probables, 'sin_coincidencia': sin_coincidencia,
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
    """Valida el formato del reporte, sin volver a leer ni hashear el vídeo.

    El SHA256 fue verificado en 8.2. En 8.4 únicamente se preparan las rutas
    para el cambio de inventario; la coincidencia del registro y su SHA256
    guardado en SQLite se comprueba durante la prevalidación del lote.
    """
    anterior = str(c.get('ruta_anterior') or '').strip()
    nueva = str(c.get('ruta_nueva') or '').strip()
    if not anterior or not nueva:
        return 'falta ruta_anterior o ruta_nueva'
    if anterior == nueva:
        return 'la ruta anterior y la nueva son iguales'
    if not str(c.get('sha256') or '').strip():
        return 'el reporte no contiene el SHA256 confirmado por la búsqueda'
    return None


def cargar_coincidencias(ruta_reporte: str,
                         validar: bool = True) -> List[Dict[str, Any]]:
    """Devuelve solo las coincidencias confirmadas de un reporte de búsqueda.

    Si `validar` es True (por defecto), valida solo el formato y la presencia
    del SHA256 en el reporte; no recalcula la huella de los archivos.
    Las rechazadas pueden consultarse con cargar_coincidencias_con_errores.
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
) -> Dict[str, Any]:
    """Cambia rutas del reporte de 8.2 sin volver a calcular SHA256.

    8.2 certificó los archivos físicos. 8.4 únicamente comprueba que las
    claves/huellas registradas en SQLite siguen siendo las esperadas, prepara
    un lote y cambia todas las rutas admisibles dentro de una transacción.
    La interrupción revierte todas las escrituras no confirmadas.
    """
    _verificar_cancelacion(cancelar)
    validas, fallidas = [], []
    for c in coincidencias:
        motivo = _validar_coincidencia(c) if validar else None
        if motivo:
            fallidas.append({**c, 'error': motivo})
        else:
            validas.append(c)

    if not validas:
        return {'aplicadas': [], 'fallidas': fallidas, 'reportes': {}}

    result = cambiar_rutas.ejecutar_lote(
        conn, dispositivo_id, dispositivo, validas, reportes,
        progreso=progreso, cancelar=cancelar,
    )
    return {
        'aplicadas': result['aplicadas'],
        'fallidas': fallidas + result['fallidas'],
        'reportes': result.get('reportes', {}),
    }
