# -*- coding: utf-8 -*-
"""Sincronización VIDUP, revisión 2.4: SQLite primero, Neon como repositorio.

Las eliminaciones confirmadas se propagan bajo autorización con prevalencia
cronológica del último escaneo válido frente a la última marca de eliminación.
Las marcas no forman parte del catálogo de vídeos vigentes. Se conservan para
no resucitar registros borrados en otros equipos. No se borran archivos físicos.
Las reubicaciones propias verificadas disponen de una conciliación específica
que no publica los identificadores temporales del cambio de ruta.
Las diferencias de SHA256 se resuelven sólo si hay una fecha más reciente
verificable; los empates o fechas no fiables quedan como conflictos.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.database import (
    _es_pg, _usa_neon, cerrar, conectar_sqlite, info_dispositivo_actual,
)
from core.horario import fmt_ahora_utc6
from core.presentacion import imprimir

DIR_SCRIPT = Path(__file__).resolve().parent.parent
VIDEO_COLUMNS = (
    'clave_unica', 'ruta', 'nombre', 'duracion', 'ancho', 'alto', 'fps',
    'codec_video', 'codec_audio', 'hash_archivo', 'hash_rapido',
    'frame_intervalo', 'audio_huella', 'tamanio_bytes', 'bitrate_video_kbps',
    'fecha_escaneo', 'dispositivo', 'dispositivo_id', 'dispositivo_origen',
    'dispositivo_origen_id', 'error',
)
# No se sobrescriben identificadores, rutas, estado de error ni datos existentes
# usando solamente un aviso de sincronización.
MEJORABLES = (
    'hash_archivo', 'hash_rapido', 'duracion', 'ancho', 'alto', 'fps',
    'codec_video', 'codec_audio', 'audio_huella', 'tamanio_bytes',
    'bitrate_video_kbps', 'frame_intervalo',
)
NUMERICOS = {
    'duracion', 'ancho', 'alto', 'fps', 'tamanio_bytes',
    'bitrate_video_kbps', 'frame_intervalo',
}
LOTE = 100


class ErrorSincronizacion(RuntimeError):
    """Fallo verdadero: nunca equivale a cero cambios aplicados con éxito."""


class SincronizacionCancelada(ErrorSincronizacion):
    """El usuario canceló la operación después de revisar el plan."""


@dataclass
class PlanSincronizacion:
    subir: List[dict] = field(default_factory=list)
    bajar: List[dict] = field(default_factory=list)
    meta_nube: List[Tuple[str, dict]] = field(default_factory=list)
    meta_local: List[Tuple[str, dict]] = field(default_factory=list)
    frames_nube: List[str] = field(default_factory=list)
    frames_local: List[str] = field(default_factory=list)
    conflictos: List[str] = field(default_factory=list)
    protegidos: List[str] = field(default_factory=list)
    # Las marcas son eventos del catálogo; no implican borrar archivos del disco.
    marcas_nube: List[Tuple[str, str]] = field(default_factory=list)
    marcas_local: List[Tuple[str, str]] = field(default_factory=list)
    borrar_nube: List[dict] = field(default_factory=list)
    borrar_local: List[dict] = field(default_factory=list)
    reactivar_nube: List[str] = field(default_factory=list)
    reactivar_local: List[str] = field(default_factory=list)
    # Mismo vídeo/SHA256: sólo cambian las huellas visuales o de audio.
    actualizar_huellas_nube: List[Tuple[str, dict]] = field(default_factory=list)
    actualizar_huellas_local: List[Tuple[str, dict]] = field(default_factory=list)
    # Misma identidad y huellas, sólo cambia la fecha del último escaneo.
    actualizar_fecha_nube: List[Tuple[str, dict]] = field(default_factory=list)
    actualizar_fecha_local: List[Tuple[str, dict]] = field(default_factory=list)
    # Cambia realmente otra información: puede requerir sustitución completa.
    reemplazar_nube: List[Tuple[str, dict]] = field(default_factory=list)
    reemplazar_local: List[Tuple[str, dict]] = field(default_factory=list)
    conflictos_eliminacion: List[str] = field(default_factory=list)

    @property
    def cambios(self) -> int:
        return (len(self.subir) + len(self.bajar) + len(self.meta_nube) +
                len(self.meta_local) + len(self.frames_nube) + len(self.frames_local) +
                len(self.marcas_nube) + len(self.marcas_local) + len(self.borrar_nube) +
                len(self.borrar_local) + len(self.reactivar_nube) +
                len(self.reactivar_local) + len(self.actualizar_huellas_nube) +
                len(self.actualizar_huellas_local) + len(self.actualizar_fecha_nube) +
                len(self.actualizar_fecha_local) + len(self.reemplazar_nube) +
                len(self.reemplazar_local))


def obtener_ruta_db_local_dispositivo() -> Path:
    """Preserva la ruta histórica del equipo principal y las DB existentes."""
    info = info_dispositivo_actual()
    # Mantener exactamente la ruta de la instalación de escritorio existente.
    if info.get('dispositivo_id') == 'b7f7b638fce2d':
        return Path(r'C:\Users\tlahu\Desktop\Prueba_VIDUP\haggyescritorio_windows_amd64.db')
    nombre = re.sub(r'[^a-zA-Z0-9_\-]', '_',
                    info.get('dispositivo', 'dispositivo').lower()).strip('_') or 'dispositivo'
    ruta = DIR_SCRIPT / f'{nombre}.db'
    antigua = DIR_SCRIPT / 'vidup.db'
    if ruta.is_file():
        return ruta
    if antigua.is_file():
        try:
            antigua.rename(ruta)
            return ruta
        except OSError:
            return antigua
    return ruta


def asegurar_tabla_eliminados(conn: Any) -> None:
    sql = ('CREATE TABLE IF NOT EXISTS videos_eliminados '
           '(clave_unica TEXT PRIMARY KEY, fecha_eliminacion TEXT NOT NULL)')
    if _es_pg(conn):
        with conn.cursor() as cur:
            cur.execute(sql)
    else:
        conn.execute(sql)
    conn.commit()


def registrar_eliminacion(conn: Any, clave_unica: str, *, confirmar: bool = True) -> None:
    """Guarda la marca de baja en la base elegida, sin tocar otros equipos.

    La marca permite reconocer el último evento durante una sincronización
    manual posterior. El borrado físico de archivos nunca se ejecuta aquí.
    """
    if not clave_unica:
        return
    sql_pg = ('INSERT INTO videos_eliminados (clave_unica, fecha_eliminacion) '
              'VALUES (%s, %s) ON CONFLICT (clave_unica) DO UPDATE '
              'SET fecha_eliminacion = EXCLUDED.fecha_eliminacion')
    sql_sq = ('INSERT INTO videos_eliminados (clave_unica, fecha_eliminacion) '
              'VALUES (?, ?) ON CONFLICT (clave_unica) DO UPDATE '
              'SET fecha_eliminacion = excluded.fecha_eliminacion')
    # La tabla se crea al abrir la BD; no silenciar errores de registro.
    if _es_pg(conn):
        with conn.cursor() as cur:
            cur.execute(sql_pg, (clave_unica, fmt_ahora_utc6()))
    else:
        conn.execute(sql_sq, (clave_unica, fmt_ahora_utc6()))
    if confirmar:
        conn.commit()


def _conectar_nube_directa() -> Any:
    """Crea conexión EXPLÍCITA a Neon; nunca degrada a SQLite silenciosamente."""
    if os.environ.get('VIDUP_FORZAR_OFFLINE', '').strip().lower() in ('1', 'true', 'yes', 'sí'):
        raise ErrorSincronizacion('Modo fuera de línea activo: Neon no se abrirá.')
    url = os.environ.get('NEON_DATABASE_URL', '').strip()
    if not url:
        raise ErrorSincronizacion('Falta NEON_DATABASE_URL.')
    try:
        import psycopg2
        # Nunca ejecutar migraciones ni DDL durante una sincronización.
        # La sustitución controlada habrá preparado el esquema y el guardián.
        conn = psycopg2.connect(url, connect_timeout=12,
                               options='-c statement_timeout=110000')
        conn.autocommit = False
        return conn
    except Exception as exc:
        raise ErrorSincronizacion(f'Neon no se pudo abrir: {exc}') from exc


def _leer_videos(conn: Any) -> Dict[str, dict]:
    campos = ('id',) + VIDEO_COLUMNS
    sql = 'SELECT ' + ', '.join(campos) + ' FROM videos'
    if _es_pg(conn):
        with conn.cursor() as cur:
            cur.execute(sql)
            filas = cur.fetchall()
    else:
        filas = conn.execute(sql).fetchall()
    resultado = {}
    for fila in filas:
        video = dict(zip(campos, fila))
        clave = video['clave_unica']
        if not clave:
            raise ErrorSincronizacion('Se encontró un vídeo con clave_unica vacía.')
        if clave in resultado:
            raise ErrorSincronizacion(f'Clave duplicada en base: {clave}')
        resultado[clave] = video
    return resultado


def _huellas_resumen(conn: Any) -> Dict[int, Tuple[int, str]]:
    """Digest por vídeo sin cargar todas las huellas visuales en memoria."""
    sql = 'SELECT video_id, posicion, hash_valor FROM frame_hashes ORDER BY video_id, posicion'
    if _es_pg(conn):
        # Cursor servidor: streaming para bibliotecas grandes.
        cursor = conn.cursor(name='vidup_frame_digest')
        cursor.itersize = 2000
        cursor.execute(sql)
    else:
        cursor = conn.execute(sql)
    digests: Dict[int, Tuple[int, str]] = {}
    anterior = None
    cuenta = 0
    hash_actual = hashlib.sha256()
    try:
        for vid, pos, valor in cursor:
            vid = int(vid)
            if anterior != vid:
                if anterior is not None:
                    digests[anterior] = (cuenta, hash_actual.hexdigest())
                anterior, cuenta, hash_actual = vid, 0, hashlib.sha256()
            hash_actual.update(f'{pos}:'.encode())
            hash_actual.update(str(valor).encode('utf-8', 'replace'))
            hash_actual.update(b'\0')
            cuenta += 1
        if anterior is not None:
            digests[anterior] = (cuenta, hash_actual.hexdigest())
    finally:
        cursor.close()
    return digests


def _ausente(campo: str, valor: Any) -> bool:
    if valor is None or valor == '':
        return True
    if campo == 'hash_rapido' and valor == 'sha256_solo':
        return True
    if campo in NUMERICOS:
        # frame_intervalo=5 es el valor predeterminado; no asumir que es ausente.
        return float(valor or 0) <= 0
    return False


def _enriquecimientos(destino: dict, fuente: dict) -> dict:
    """Sólo completa campos vacíos: nunca sustituye valores existentes."""
    return {campo: fuente[campo] for campo in MEJORABLES
            if _ausente(campo, destino.get(campo)) and
            not _ausente(campo, fuente.get(campo))}


def _diferencias(destino: dict, fuente: dict) -> List[str]:
    ignorar = {'id', 'fecha_escaneo'}
    return [c for c in VIDEO_COLUMNS if c not in ignorar and
            not _ausente(c, destino.get(c)) and not _ausente(c, fuente.get(c))
            and destino.get(c) != fuente.get(c)]


def calcular_plan(local: Dict[str, dict], nube: Dict[str, dict],
                  frames_loc: Dict[int, Tuple[int, str]],
                  frames_nube: Dict[int, Tuple[int, str]],
                  eliminados_loc: set[str]) -> PlanSincronizacion:
    """Plan puro, comprobable sin conexión real y sin modificar ninguna DB."""
    plan = PlanSincronizacion()
    resumen_vacio = (0, '')
    for clave, v in local.items():
        vn = nube.get(clave)
        if vn is None:
            plan.subir.append(v)
            continue
        # Un mismo archivo/ruta puede haber cambiado de contenido. Jamás
        # mezclar huellas SHA256, audio o fotogramas si dos SHA256 difieren.
        if (v.get('hash_archivo') and vn.get('hash_archivo') and
                v['hash_archivo'] != vn['hash_archivo']):
            plan.conflictos.append(f'{clave}: SHA256 distinto; enriquecimiento bloqueado')
            continue
        faltante_nube = _enriquecimientos(vn, v)
        faltante_local = _enriquecimientos(v, vn)
        if faltante_nube:
            plan.meta_nube.append((clave, faltante_nube))
        if faltante_local:
            plan.meta_local.append((clave, faltante_local))
        d1 = frames_loc.get(int(v['id']), resumen_vacio)
        d2 = frames_nube.get(int(vn['id']), resumen_vacio)
        if d1[0] and not d2[0]:
            plan.frames_nube.append(clave)
        elif d2[0] and not d1[0]:
            plan.frames_local.append(clave)
        elif d1[0] and d2[0] and d1 != d2:
            plan.conflictos.append(f'{clave}: huellas visuales diferentes')
        diferencias = _diferencias(v, vn)
        if diferencias:
            plan.conflictos.append(f'{clave}: metadatos distintos ({", ".join(diferencias)})')
    for clave, v in nube.items():
        if clave not in local:
            if clave in eliminados_loc:
                plan.protegidos.append(clave)
            else:
                plan.bajar.append(v)
    # No duplicar rutas que sólo difieren por los separadores. Se dejan en
    # conflicto sin elegir arbitrariamente cuál es la versión correcta.
    local_normal = {}
    nube_normal = {}
    for clave in local:
        local_normal.setdefault(clave.replace('\\', '/'), set()).add(clave)
    for clave in nube:
        nube_normal.setdefault(clave.replace('\\', '/'), set()).add(clave)
    aliases = set()
    for normal in local_normal.keys() & nube_normal.keys():
        for a in local_normal[normal]:
            for b in nube_normal[normal]:
                if a != b:
                    aliases.add(a)
                    aliases.add(b)
                    plan.conflictos.append(f'{a} / {b}: rutas equivalentes; no duplicar')
    if aliases:
        plan.subir = [v for v in plan.subir if v['clave_unica'] not in aliases]
        plan.bajar = [v for v in plan.bajar if v['clave_unica'] not in aliases]

    return plan


def _leer_marcas(conn: Any) -> Dict[str, str]:
    """Lee las eliminaciones de la base elegida sin escribir ni borrar nada."""
    sql = 'SELECT clave_unica, fecha_eliminacion FROM videos_eliminados'
    if _es_pg(conn):
        with conn.cursor() as cur:
            cur.execute(sql)
            filas = cur.fetchall()
    else:
        filas = conn.execute(sql).fetchall()
    return {str(clave): str(fecha) for clave, fecha in filas}


def _fecha_compatible(fecha: Any):
    """Normaliza fechas ISO de VIDUP a datetime en UTC; nunca compara textos.

    Los registros históricos de VIDUP utilizan UTC-6 sin desplazamiento en el
    texto. Los registros con zona explícita se convierten a UTC para evitar
    que una conversión aparente convierta un borrado viejo en uno reciente.
    """
    from datetime import datetime, timezone, timedelta
    if fecha is None or str(fecha).strip() == '':
        return None
    try:
        obj = datetime.fromisoformat(str(fecha).strip().replace('Z', '+00:00'))
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone(timedelta(hours=-6)))
        return obj.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _sha256_valido(hash_archivo: Any) -> bool:
    """Exige SHA256 hexadecimal completo para resolver versiones destructivas."""
    valor = str(hash_archivo or '').strip().lower()
    return len(valor) == 64 and all(c in '0123456789abcdef' for c in valor)


def _quitar_operaciones_clave(plan: PlanSincronizacion, clave: str,
                            *, retirar_conflictos: bool = False) -> None:
    """Impide mezclar altas/huellas con el estado de una eliminación o revisión."""
    plan.subir = [v for v in plan.subir if v['clave_unica'] != clave]
    plan.bajar = [v for v in plan.bajar if v['clave_unica'] != clave]
    plan.meta_nube = [(k, m) for k, m in plan.meta_nube if k != clave]
    plan.meta_local = [(k, m) for k, m in plan.meta_local if k != clave]
    plan.frames_nube = [k for k in plan.frames_nube if k != clave]
    plan.frames_local = [k for k in plan.frames_local if k != clave]
    plan.protegidos = [k for k in plan.protegidos if k != clave]
    if retirar_conflictos:
        plan.conflictos = [v for v in plan.conflictos if not v.startswith(clave + ':')]


def _tipo_revision_mismo_video(ganador: dict, perdedor: dict,
                              huellas_ganador: Tuple[int, str],
                              huellas_perdedor: Tuple[int, str]) -> str:
    """Clasifica cambios del mismo vídeo sin recalcular SHA256 físico.

    No se denomina «actualización de huellas» a una sustitución del vídeo:
    su SHA256 y todos los campos básicos/metadatos deben permanecer iguales.
    Una revisión que pierde huellas existentes no se trata como enriquecimiento.
    """
    sha_g, sha_p = (str(v.get('hash_archivo') or '').strip().lower()
                    for v in (ganador, perdedor))
    if not sha_g or sha_g != sha_p or not _sha256_valido(sha_g):
        return 'reemplazo'
    for campo in VIDEO_COLUMNS:
        if campo in ('fecha_escaneo', 'audio_huella'):
            continue
        if ganador.get(campo) != perdedor.get(campo):
            return 'reemplazo'
    audio_g, audio_p = ganador.get('audio_huella'), perdedor.get('audio_huella')
    visual_g, visual_p = huellas_ganador, huellas_perdedor
    if audio_p and not audio_g:
        return 'conflicto'  # Nunca borrar una huella de audio existente.
    if visual_p[0] and not visual_g[0]:
        return 'conflicto'  # Nunca descartar fotogramas ya analizados.
    if audio_g != audio_p or visual_g != visual_p:
        return 'huellas'
    return 'fecha'


def _planear_marcas(plan: PlanSincronizacion,
                    local: Dict[str, dict], nube: Dict[str, dict],
                    marcas_local: Dict[str, str], marcas_nube: Dict[str, str],
                    *, autorizar_borrados_nube: bool = False,
                    frames_local: Optional[Dict[int, Tuple[int, str]]] = None,
                    frames_nube: Optional[Dict[int, Tuple[int, str]]] = None) -> None:
    """Reconcilia eventos por clave: gana el escaneo o la eliminación posterior.

    La acción de borrar registros sólo se programa si el usuario habilitó la
    reconciliación. Nunca interpreta ausencia de un vídeo como prueba de
    eliminación y nunca intenta tocar el archivo multimedia del disco.
    """
    frames_local = frames_local or {}
    frames_nube = frames_nube or {}
    todas = sorted(set(local) | set(nube) | set(marcas_local) | set(marcas_nube))
    for clave in todas:
        vl, vn = local.get(clave), nube.get(clave)
        fl, fn = marcas_local.get(clave), marcas_nube.get(clave)
        if not (fl or fn or (vl and vn)):
            continue
        fechal, fechan = (_fecha_compatible(v.get('fecha_escaneo')) if v else None
                         for v in (vl, vn))
        fechadl, fechadn = _fecha_compatible(fl), _fecha_compatible(fn)
        # Si las bases afirman que hubo un evento pero no se puede fechar,
        # bloquear únicamente esa clave, no el resto de la sincronización.
        if ((vl and not fechal and (fl or fn or vn)) or
                (vn and not fechan and (fl or fn or vl)) or
                (fl and not fechadl) or (fn and not fechadn)):
            _quitar_operaciones_clave(plan, clave)
            plan.conflictos_eliminacion.append(
                f'{clave}: fecha de escaneo/eliminación ausente o ilegible; revisar')
            continue
        vivos = [(fechal, 'local', vl), (fechan, 'neon', vn)]
        vivos = [v for v in vivos if v[0] is not None]
        vivo = max(vivos, key=lambda v: v[0]) if vivos else None
        fechas_borrado = [(fechadl, 'local', fl), (fechadn, 'neon', fn)]
        fechas_borrado = [v for v in fechas_borrado if v[0] is not None]
        borrado = max(fechas_borrado, key=lambda v: v[0]) if fechas_borrado else None

        if vivo and borrado and vivo[0] == borrado[0]:
            _quitar_operaciones_clave(plan, clave)
            plan.conflictos_eliminacion.append(
                f'{clave}: escaneo y borrado tienen la misma fecha; no se presupone ganador')
            continue
        if borrado and (not vivo or borrado[0] > vivo[0]):
            _quitar_operaciones_clave(plan, clave)
            if not autorizar_borrados_nube and (vl or vn):
                plan.protegidos.append(clave)
                continue
            # La marca ganadora se replica con su fecha original. Primero la
            # marca, después el DELETE del catálogo (FK CASCADE de los frames).
            fecha_borrado = borrado[2]
            if not fechadl or fechadl < borrado[0]:
                plan.marcas_local.append((clave, fecha_borrado))
            if not fechadn or fechadn < borrado[0]:
                plan.marcas_nube.append((clave, fecha_borrado))
            if vl:
                plan.borrar_local.append(vl)
            if vn:
                plan.borrar_nube.append(vn)
            continue

        if not vivo:
            continue
        # Si existe un nuevo escaneo posterior, la lápida es obsoleta.
        if fl:
            plan.reactivar_local.append(clave)
        if fn:
            plan.reactivar_nube.append(clave)
        if not vl and vn:
            _quitar_operaciones_clave(plan, clave)
            plan.bajar.append(vn)
        elif vl and not vn:
            _quitar_operaciones_clave(plan, clave)
            plan.subir.append(vl)
        elif vl and vn:
            if fechal == fechan:
                if (vl.get('hash_archivo') != vn.get('hash_archivo') or
                    frames_local.get(int(vl['id']), (0, '')) !=
                        frames_nube.get(int(vn['id']), (0, ''))):
                    _quitar_operaciones_clave(plan, clave)
                    plan.conflictos_eliminacion.append(
                        f'{clave}: mismo instante con huellas distintas; revisión manual')
                continue
            ganador_local = fechal > fechan
            ganador = vl if ganador_local else vn
            perdedor = vn if ganador_local else vl
            hashes_distintos = (vl.get('hash_archivo') != vn.get('hash_archivo'))
            huellas_distintas = (frames_local.get(int(vl['id']), (0, '')) !=
                                 frames_nube.get(int(vn['id']), (0, '')))
            # Sólo sustituir una versión con SHA256 confirmado; no reconstruir
            # un vídeo completo a partir de metadatos/hashes ausentes.
            if hashes_distintos or huellas_distintas or fechal != fechan:
                if not _sha256_valido(ganador.get('hash_archivo')):
                    _quitar_operaciones_clave(plan, clave)
                    plan.conflictos_eliminacion.append(
                        f'{clave}: última versión sin SHA256; revisión bloqueada')
                    continue
                tipo = _tipo_revision_mismo_video(
                    ganador, perdedor,
                    (frames_local if ganador_local else frames_nube).get(int(ganador['id']), (0, '')),
                    (frames_nube if ganador_local else frames_local).get(int(perdedor['id']), (0, '')),
                )
                _quitar_operaciones_clave(plan, clave, retirar_conflictos=True)
                if tipo == 'conflicto':
                    plan.conflictos_eliminacion.append(
                        f'{clave}: el escaneo reciente tiene menos huellas; '
                        'no se descartan los análisis existentes')
                    continue
                if tipo == 'huellas':
                    destino = plan.actualizar_huellas_nube if ganador_local else plan.actualizar_huellas_local
                elif tipo == 'fecha':
                    destino = plan.actualizar_fecha_nube if ganador_local else plan.actualizar_fecha_local
                else:
                    destino = plan.reemplazar_nube if ganador_local else plan.reemplazar_local
                destino.append((clave, ganador))

def _copia_previa_borrados_nube(conn: Any, videos: List[dict]) -> Optional[Path]:
    """Guarda una recuperación portable de filas y huellas antes de eliminar.

    Aborta si no puede escribir el respaldo. No almacena credenciales de Neon.
    """
    if not videos:
        return None
    from datetime import datetime
    carpeta = DIR_SCRIPT / 'reportes' / 'respaldos' / 'sincronizacion'
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / ('neon_antes_eliminaciones_' +
                     datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.json')
    datos = []
    for i in range(0, len(videos), 100):
        lote = videos[i:i+100]
        huellas = _frames_por_ids_nube(conn, [int(v['id']) for v in lote])
        for video in lote:
            datos.append({'video': video,
                          'frames': huellas.get(int(video['id']), [])})
    contenido = {'formato': 'vidup-neon-recuperacion-v1',
                 'fecha': datetime.now().isoformat(timespec='seconds'),
                 'motivo': 'Sincronización autorizada de eliminaciones locales',
                 'registros': datos}
    with ruta.open('x', encoding='utf-8') as fh:
        json.dump(contenido, fh, ensure_ascii=False, indent=2, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    return ruta


def _imprimir_plan(plan: PlanSincronizacion, n_local: int, n_nube: int) -> None:
    imprimir(f'[cyan]PLAN DE SINCRONIZACIÓN · SQLite {n_local} vídeos · Neon {n_nube} vídeos[/]')
    imprimir(f'  Subir nuevos: {len(plan.subir)} · Bajar nuevos: {len(plan.bajar)}')
    imprimir(f'  Completar metadatos en Neon: {len(plan.meta_nube)} · SQLite: {len(plan.meta_local)}')
    imprimir(f'  Completar huellas en Neon: {len(plan.frames_nube)} · SQLite: {len(plan.frames_local)}')
    imprimir(f'  Conflictos NO sobrescritos: {len(plan.conflictos)}')
    imprimir(f'  Publicaciones bloqueadas (copia antigua o registros ajenos): {len(plan.protegidos)}')
    imprimir(f'  Marcas pendientes de copiar → Neon: {len(plan.marcas_nube)}; → SQLite: {len(plan.marcas_local)}')
    imprimir(f'  Registros propuestos para eliminar: Neon {len(plan.borrar_nube)}; SQLite {len(plan.borrar_local)}')
    imprimir(f'  Reescaneos posteriores al borrado: {len(plan.reactivar_local) + len(plan.reactivar_nube)} marcas obsoletas')
    imprimir(f'  [spring_green2]Actualizaciones de huellas visuales/audio: '
            f'Neon {len(plan.actualizar_huellas_nube)}; SQLite {len(plan.actualizar_huellas_local)}[/]')
    imprimir(f'  Ajustes de fecha de escaneo: Neon {len(plan.actualizar_fecha_nube)}; '
            f'SQLite {len(plan.actualizar_fecha_local)}')
    imprimir(f'  [yellow]Sustituciones reales de registros: Neon '
            f'{len(plan.reemplazar_nube)}; SQLite {len(plan.reemplazar_local)}[/]')
    imprimir(f'  Conflictos de eliminación/revisión: {len(plan.conflictos_eliminacion)}')
    imprimir('  [gold1]Los borrados solo se aplican tras autorización expresa y respaldo JSON.[/]')
    for item in (plan.conflictos + plan.conflictos_eliminacion)[:10]:
        imprimir(f'  [yellow]CONFLICTO: {item}[/]')
    if len(plan.conflictos) + len(plan.conflictos_eliminacion) > 10:
        imprimir(f'  [yellow]... otros {len(plan.conflictos) + len(plan.conflictos_eliminacion) - 10} conflictos.[/]')


def _guardar_plan(plan: PlanSincronizacion, ruta_local: Path,
                  n_local: int, n_nube: int) -> Path:
    """Bitácora del plan calculado; no es copia de seguridad de una depuración."""
    carpeta = DIR_SCRIPT / 'reportes' / 'sincronizacion' / 'json'
    carpeta.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    archivo = carpeta / ('plan_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.json')
    contenido = {
        'fecha': datetime.now().isoformat(timespec='seconds'),
        'sqlite': str(ruta_local), 'neon': 'configurada (URL omitida)',
        'videos_locales': n_local, 'videos_neon': n_nube,
        'subir': [v['clave_unica'] for v in plan.subir],
        'bajar': [v['clave_unica'] for v in plan.bajar],
        'completar_metadatos_neon': [k for k, _ in plan.meta_nube],
        'completar_metadatos_local': [k for k, _ in plan.meta_local],
        'completar_huellas_neon': plan.frames_nube,
        'completar_huellas_local': plan.frames_local,
        'conflictos_no_resueltos': plan.conflictos,
        'publicaciones_bloqueadas_o_protegidas': plan.protegidos,
        'marcas_para_neon': plan.marcas_nube,
        'marcas_para_sqlite': plan.marcas_local,
        'videos_borrar_en_neon': [v['clave_unica'] for v in plan.borrar_nube],
        'videos_borrar_en_sqlite': [v['clave_unica'] for v in plan.borrar_local],
        'reactivar_marcas_neon': plan.reactivar_nube,
        'reactivar_marcas_sqlite': plan.reactivar_local,
        'actualizar_huellas_neon': [k for k, _ in plan.actualizar_huellas_nube],
        'actualizar_huellas_sqlite': [k for k, _ in plan.actualizar_huellas_local],
        'actualizar_fecha_neon': [k for k, _ in plan.actualizar_fecha_nube],
        'actualizar_fecha_sqlite': [k for k, _ in plan.actualizar_fecha_local],
        'reemplazar_version_antigua_neon': [k for k, _ in plan.reemplazar_nube],
        'reemplazar_version_antigua_sqlite': [k for k, _ in plan.reemplazar_local],
        'conflictos_de_eliminacion': plan.conflictos_eliminacion,
        'eliminaciones_automaticas': 0,
        'precedencia': 'último evento comprobable: escaneo SHA256 o marca de eliminación',
        'nota': 'El plan es informativo. Sin confirmación no se aplican cambios.'
    }
    with archivo.open('x', encoding='utf-8') as fp:
        json.dump(contenido, fp, ensure_ascii=False, indent=2)
    from core.auditoria_sincronizacion import guardar_plan_txt
    try:
        guardar_plan_txt(archivo, contenido)
    except BaseException:
        archivo.unlink(missing_ok=True)
        raise
    return archivo


def _frames_por_ids_local(conn: Any, ids: List[int]) -> dict[int, list]:
    if not ids:
        return {}
    signos = ', '.join('?' for _ in ids)
    datos = conn.execute(
        'SELECT video_id, posicion, hash_valor FROM frame_hashes '
        f'WHERE video_id IN ({signos}) ORDER BY video_id, posicion', ids).fetchall()
    sal = {}
    for id_, pos, huella in datos:
        sal.setdefault(int(id_), []).append((int(pos), huella))
    return sal


def _frames_por_ids_nube(conn: Any, ids: List[int]) -> dict[int, list]:
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute('SELECT video_id, posicion, hash_valor FROM frame_hashes '
                    'WHERE video_id = ANY(%s) ORDER BY video_id, posicion', (ids,))
        filas = cur.fetchall()
    salida = {}
    for id_, pos, huella in filas:
        salida.setdefault(int(id_), []).append((int(pos), huella))
    return salida


def _huellas_resumen_un_video(conn: Any, video_id: int) -> Tuple[int, str]:
    """Resumen de huellas almacenadas de un vídeo, sin abrir el MP4."""
    if _es_pg(conn):
        with conn.cursor() as cur:
            cur.execute('SELECT posicion, hash_valor FROM frame_hashes '
                        'WHERE video_id = %s ORDER BY posicion', (video_id,))
            filas = cur.fetchall()
    else:
        filas = conn.execute('SELECT posicion, hash_valor FROM frame_hashes '
                            'WHERE video_id = ? ORDER BY posicion', (video_id,)).fetchall()
    if not filas:
        return 0, ''
    digest = hashlib.sha256()
    for posicion, valor in filas:
        digest.update(f'{posicion}:'.encode())
        digest.update(str(valor).encode('utf-8', 'replace'))
        digest.update(b'\0')
    return len(filas), digest.hexdigest()


def _aplicar_plan(conn_loc: Any, conn_nube: Any, plan: PlanSincronizacion,
                  locales: dict, remotos: dict, progreso=None) -> Tuple[int, int, int]:
    """Altas y enriquecimientos; eliminación remota sólo si fue autorizada."""
    import psycopg2.extras
    subida = bajada = 0
    columnas = ', '.join(VIDEO_COLUMNS)
    placeholders_sq = ', '.join('?' for _ in VIDEO_COLUMNS)
    sql_insert_sq = (f'INSERT INTO videos ({columnas}) VALUES ({placeholders_sq}) '
                     'ON CONFLICT (clave_unica) DO NOTHING')
    sql_insert_pg = (f'INSERT INTO videos ({columnas}) VALUES %s '
                     'ON CONFLICT (clave_unica) DO NOTHING RETURNING id, clave_unica')

    def avance(etapa, actual, total, detalle):
        if progreso is not None:
            progreso.actualizar(etapa, actual, max(1, total), detalle=detalle)

    pendientes_subir = len(plan.subir) + len(plan.meta_nube) + len(plan.frames_nube)
    pendientes_bajar = len(plan.bajar) + len(plan.meta_local) + len(plan.frames_local)
    avance('subir', 0, pendientes_subir, 'Preparando altas de Neon')
    # ALTA EN NEON — en lotes, incluyendo las huellas en la misma transacción.
    for inicio in range(0, len(plan.subir), LOTE):
        lote = plan.subir[inicio:inicio+LOTE]
        ids = [int(v['id']) for v in lote]
        frames = _frames_por_ids_local(conn_loc, ids)
        valores = [tuple(v[c] for c in VIDEO_COLUMNS) for v in lote]
        with conn_nube.cursor() as cur:
            devueltos = psycopg2.extras.execute_values(
                cur, sql_insert_pg, valores, page_size=LOTE, fetch=True)
            if len(devueltos) != len(lote):
                raise ErrorSincronizacion('Neon cambió después de calcular el plan. Cancelado.')
            mapa = {clave: int(id_) for id_, clave in devueltos}
            frames_valores = [(mapa[v['clave_unica']], pos, valor)
                              for v in lote for pos, valor in frames.get(int(v['id']), [])]
            if frames_valores:
                psycopg2.extras.execute_values(
                    cur, 'INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES %s '
                         'ON CONFLICT (video_id, posicion) DO NOTHING',
                    frames_valores, page_size=1000)
        subida += len(lote)
        avance('subir', subida, pendientes_subir, f'{len(lote)} vídeos a Neon')

    # ENRIQUECIMIENTO REMOTO — actualización condicionada al estado leído.
    for clave, cambios in plan.meta_nube:
        original = remotos[clave]
        campos = list(cambios)
        sentencia = 'UPDATE videos SET ' + ', '.join(f'{c} = %s' for c in campos)
        sentencia += ' WHERE id = %s AND clave_unica = %s'
        with conn_nube.cursor() as cur:
            cur.execute(sentencia, tuple(cambios[c] for c in campos) +
                        (int(original['id']), clave))
            if cur.rowcount != 1:
                raise ErrorSincronizacion('Un vídeo cambió en Neon durante la sincronización.')
        subida += 1
        avance('subir', subida, pendientes_subir, f'Actualizando metadatos {clave}')

    for inicio in range(0, len(plan.frames_nube), LOTE):
        claves = plan.frames_nube[inicio:inicio+LOTE]
        registros = [locales[k] for k in claves]
        frames = _frames_por_ids_local(conn_loc, [int(v['id']) for v in registros])
        valores = [(int(remotos[k]['id']), pos, h)
                   for k in claves for pos, h in frames.get(int(locales[k]['id']), [])]
        if valores:
            with conn_nube.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur, 'INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES %s '
                         'ON CONFLICT (video_id, posicion) DO NOTHING',
                    valores, page_size=1000)
        subida += len(claves)
        avance('subir', subida, pendientes_subir, 'Huellas hacia Neon')

    avance('bajar', 0, pendientes_bajar, 'Preparando altas en SQLite')
    # ALTA EN SQLITE — nunca INSERT OR REPLACE (evita cascadas de eliminación).
    for inicio in range(0, len(plan.bajar), LOTE):
        lote = plan.bajar[inicio:inicio+LOTE]
        frames = _frames_por_ids_nube(conn_nube, [int(v['id']) for v in lote])
        for v in lote:
            resultado = conn_loc.execute(sql_insert_sq, tuple(v[c] for c in VIDEO_COLUMNS))
            if resultado.rowcount != 1:
                raise ErrorSincronizacion('SQLite cambió después de calcular el plan. Cancelado.')
            id_nuevo = int(resultado.lastrowid)
            if frames.get(int(v['id'])):
                conn_loc.executemany(
                    'INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES (?, ?, ?)',
                    [(id_nuevo, pos, h) for pos, h in frames[int(v['id'])]])
            bajada += 1
            avance('bajar', bajada, pendientes_bajar, str(v.get('nombre') or v['clave_unica']))

    for clave, cambios in plan.meta_local:
        original = locales[clave]
        campos = list(cambios)
        sentencia = 'UPDATE videos SET ' + ', '.join(f'{c} = ?' for c in campos)
        sentencia += ' WHERE id = ? AND clave_unica = ?'
        resultado = conn_loc.execute(sentencia, tuple(cambios[c] for c in campos) +
                                    (int(original['id']), clave))
        if resultado.rowcount != 1:
            raise ErrorSincronizacion('Un vídeo cambió en SQLite durante la sincronización.')
        bajada += 1
        avance('bajar', bajada, pendientes_bajar, f'Metadatos {clave}')

    for inicio in range(0, len(plan.frames_local), LOTE):
        claves = plan.frames_local[inicio:inicio+LOTE]
        frames = _frames_por_ids_nube(conn_nube, [int(remotos[k]['id']) for k in claves])
        valores = [(int(locales[k]['id']), pos, h)
                   for k in claves for pos, h in frames.get(int(remotos[k]['id']), [])]
        if valores:
            conn_loc.executemany(
                'INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES (?, ?, ?)',
                valores)
        bajada += len(claves)
        avance('bajar', bajada, pendientes_bajar, 'Huellas descargadas de Neon')
    avance('marcas', 0, len(plan.marcas_nube) + len(plan.marcas_local), 'Copiando marcas de eliminación')
    marcas_hechas = 0
    for clave, fecha in plan.marcas_nube:
        with conn_nube.cursor() as cur:
            cur.execute(
                'INSERT INTO videos_eliminados (clave_unica, fecha_eliminacion) '
                'VALUES (%s, %s) ON CONFLICT (clave_unica) DO UPDATE '
                'SET fecha_eliminacion = EXCLUDED.fecha_eliminacion', (clave, fecha))
        marcas_hechas += 1
        avance('marcas', marcas_hechas, len(plan.marcas_nube) + len(plan.marcas_local), clave)
    for clave, fecha in plan.marcas_local:
        conn_loc.execute(
            'INSERT INTO videos_eliminados (clave_unica, fecha_eliminacion) '
            'VALUES (?, ?) ON CONFLICT (clave_unica) DO UPDATE '
            'SET fecha_eliminacion = excluded.fecha_eliminacion', (clave, fecha))
        marcas_hechas += 1
        avance('marcas', marcas_hechas, len(plan.marcas_nube) + len(plan.marcas_local), clave)
    # La confirmación y el respaldo se hicieron antes de llamar esta función.
    avance('eliminar', 0, len(plan.borrar_nube), 'Eliminaciones autorizadas en Neon')
    for indice, video in enumerate(plan.borrar_nube, 1):
        with conn_nube.cursor() as cur:
            cur.execute('DELETE FROM frame_hashes WHERE video_id = %s',
                        (int(video['id']),))
            cur.execute('DELETE FROM videos WHERE clave_unica = %s AND id = %s '
                        'AND fecha_escaneo = %s AND hash_archivo = %s',
                        (video['clave_unica'], int(video['id']),
                         video['fecha_escaneo'], video.get('hash_archivo') or ''))
            if cur.rowcount != 1:
                raise ErrorSincronizacion('El registro de Neon cambió desde el plan; se cancela.')
        avance('eliminar', indice, len(plan.borrar_nube), video.get('nombre') or video['clave_unica'])
    # Las marcas caducadas por reescaneo posterior no deben volver a impedir
    # la presencia de un vídeo activo, incluso si se sincroniza otro equipo.
    n_reactivaciones = len(plan.reactivar_local) + len(plan.reactivar_nube)
    avance('reactivar', 0, n_reactivaciones, 'Retirando marcas obsoletas')
    for i, clave in enumerate(plan.reactivar_nube, 1):
        with conn_nube.cursor() as cur:
            cur.execute('DELETE FROM videos_eliminados WHERE clave_unica = %s', (clave,))
        avance('reactivar', i, n_reactivaciones, clave)
    for i, clave in enumerate(plan.reactivar_local, len(plan.reactivar_nube) + 1):
        conn_loc.execute('DELETE FROM videos_eliminados WHERE clave_unica = ?', (clave,))
        avance('reactivar', i, n_reactivaciones, clave)

    # Enriquecimiento de huellas de un vídeo de IDENTIDAD INALTERADA.
    # Solo se modifican fecha/audio_huella/frame_hashes; no ruta, SHA256,
    # nombre, tamaño ni los demás datos básicos. No se abre ningún vídeo.
    total_huellas = len(plan.actualizar_huellas_nube) + len(plan.actualizar_huellas_local)
    avance('huellas', 0, total_huellas, 'Actualizando análisis existentes')
    for i, (clave, origen) in enumerate(plan.actualizar_huellas_nube, 1):
        destino = remotos[clave]
        # Fuente y destino se mantienen estables según el plan leído.
        visual_origen = _huellas_resumen_un_video(conn_loc, int(origen['id']))
        visual_destino = _huellas_resumen_un_video(conn_nube, int(destino['id']))
        if _tipo_revision_mismo_video(origen, destino,
                                     visual_origen, visual_destino) != 'huellas':
            raise ErrorSincronizacion('Las huellas de Neon cambiaron durante el plan: ' + clave)
        campos = ['fecha_escaneo']
        valores = [origen['fecha_escaneo']]
        if origen.get('audio_huella') is not None and origen.get('audio_huella') != '':
            campos.append('audio_huella')
            valores.append(origen['audio_huella'])
        sql = 'UPDATE videos SET ' + ', '.join(c + ' = %s' for c in campos)
        with conn_nube.cursor() as cur:
            cur.execute(sql + ' WHERE id = %s AND clave_unica = %s AND fecha_escaneo = %s AND hash_archivo = %s',
                        tuple(valores) + (int(destino['id']), clave, destino['fecha_escaneo'],
                                         destino['hash_archivo']))
            if cur.rowcount != 1:
                raise ErrorSincronizacion('Neon cambió durante la actualización de huellas: ' + clave)
            if visual_origen != visual_destino:
                frames_origen = _frames_por_ids_local(conn_loc, [int(origen['id'])]).get(int(origen['id']), [])
                if not frames_origen:
                    raise ErrorSincronizacion('No se hallaron las huellas visuales esperadas: ' + clave)
                cur.execute('DELETE FROM frame_hashes WHERE video_id = %s', (int(destino['id']),))
                psycopg2.extras.execute_values(
                    cur, 'INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES %s',
                    [(int(destino['id']), pos, h) for pos, h in frames_origen], page_size=1000)
        avance('huellas', i, total_huellas, clave)
    for i, (clave, origen) in enumerate(plan.actualizar_huellas_local,
                                        len(plan.actualizar_huellas_nube) + 1):
        destino = locales[clave]
        visual_origen = _huellas_resumen_un_video(conn_nube, int(origen['id']))
        visual_destino = _huellas_resumen_un_video(conn_loc, int(destino['id']))
        if _tipo_revision_mismo_video(origen, destino,
                                     visual_origen, visual_destino) != 'huellas':
            raise ErrorSincronizacion('Las huellas de SQLite cambiaron durante el plan: ' + clave)
        campos = ['fecha_escaneo']
        valores = [origen['fecha_escaneo']]
        if origen.get('audio_huella') is not None and origen.get('audio_huella') != '':
            campos.append('audio_huella')
            valores.append(origen['audio_huella'])
        sql = 'UPDATE videos SET ' + ', '.join(c + ' = ?' for c in campos)
        cur = conn_loc.execute(sql + ' WHERE id = ? AND clave_unica = ? AND fecha_escaneo = ? AND hash_archivo = ?',
                               tuple(valores) + (int(destino['id']), clave, destino['fecha_escaneo'],
                                                destino['hash_archivo']))
        if cur.rowcount != 1:
            raise ErrorSincronizacion('SQLite cambió durante la actualización de huellas: ' + clave)
        if visual_origen != visual_destino:
            frames_origen = _frames_por_ids_nube(conn_nube, [int(origen['id'])]).get(int(origen['id']), [])
            if not frames_origen:
                raise ErrorSincronizacion('No se hallaron las huellas visuales esperadas: ' + clave)
            conn_loc.execute('DELETE FROM frame_hashes WHERE video_id = ?', (int(destino['id']),))
            conn_loc.executemany(
                'INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES (?, ?, ?)',
                [(int(destino['id']), pos, h) for pos, h in frames_origen])
        avance('huellas', i, total_huellas, clave)

    # Alinea únicamente las fechas de escaneo cuando los datos y huellas
    # ya coinciden. No vuelve a copiar las huellas visuales.
    total_fechas = len(plan.actualizar_fecha_nube) + len(plan.actualizar_fecha_local)
    avance('fechas', 0, total_fechas, 'Fechas de escaneo coincidentes')
    for i, (clave, origen) in enumerate(plan.actualizar_fecha_nube, 1):
        destino = remotos[clave]
        with conn_nube.cursor() as cur:
            cur.execute('UPDATE videos SET fecha_escaneo = %s WHERE id = %s AND clave_unica = %s '
                        'AND fecha_escaneo = %s AND hash_archivo = %s',
                        (origen['fecha_escaneo'], int(destino['id']), clave,
                         destino['fecha_escaneo'], destino['hash_archivo']))
            if cur.rowcount != 1:
                raise ErrorSincronizacion('Neon cambió al alinear fecha de escaneo: ' + clave)
        avance('fechas', i, total_fechas, clave)
    for i, (clave, origen) in enumerate(plan.actualizar_fecha_local,
                                        len(plan.actualizar_fecha_nube) + 1):
        destino = locales[clave]
        cur = conn_loc.execute('UPDATE videos SET fecha_escaneo = ? WHERE id = ? AND clave_unica = ? '
                               'AND fecha_escaneo = ? AND hash_archivo = ?',
                               (origen['fecha_escaneo'], int(destino['id']), clave,
                                destino['fecha_escaneo'], destino['hash_archivo']))
        if cur.rowcount != 1:
            raise ErrorSincronizacion('SQLite cambió al alinear fecha de escaneo: ' + clave)
        avance('fechas', i, total_fechas, clave)

    # Restauración de la versión más nueva SIN recalcular huellas. Los frames
    # se copian de la base ganadora y se sustituyen junto con los metadatos,
    # en la misma transacción. El ID de la fila existente no cambia.
    total_versiones = len(plan.reemplazar_nube) + len(plan.reemplazar_local)
    avance('revision', 0, total_versiones, 'Revisiones recientes')
    for i, (clave, origen) in enumerate(plan.reemplazar_nube, 1):
        destino = remotos[clave]
        campos = ', '.join(c + ' = %s' for c in VIDEO_COLUMNS if c != 'clave_unica')
        valores = tuple(origen[c] for c in VIDEO_COLUMNS if c != 'clave_unica')
        with conn_nube.cursor() as cur:
            cur.execute('UPDATE videos SET ' + campos +
                        ' WHERE id = %s AND clave_unica = %s AND fecha_escaneo = %s',
                        valores + (int(destino['id']), clave, destino['fecha_escaneo']))
            if cur.rowcount != 1:
                raise ErrorSincronizacion('Neon cambió durante sustitución de versión: ' + clave)
            cur.execute('DELETE FROM frame_hashes WHERE video_id = %s', (int(destino['id']),))
            frames = _frames_por_ids_local(conn_loc, [int(origen['id'])])
            valores_frames = [(int(destino['id']), pos, h)
                             for pos, h in frames.get(int(origen['id']), [])]
            if valores_frames:
                psycopg2.extras.execute_values(
                    cur, 'INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES %s',
                    valores_frames, page_size=1000)
        avance('revision', i, total_versiones, clave)
    for i, (clave, origen) in enumerate(plan.reemplazar_local,
                                        len(plan.reemplazar_nube) + 1):
        destino = locales[clave]
        campos = ', '.join(c + ' = ?' for c in VIDEO_COLUMNS if c != 'clave_unica')
        valores = tuple(origen[c] for c in VIDEO_COLUMNS if c != 'clave_unica')
        cur = conn_loc.execute('UPDATE videos SET ' + campos +
                               ' WHERE id = ? AND clave_unica = ? AND fecha_escaneo = ?',
                               valores + (int(destino['id']), clave, destino['fecha_escaneo']))
        if cur.rowcount != 1:
            raise ErrorSincronizacion('SQLite cambió durante sustitución de versión: ' + clave)
        conn_loc.execute('DELETE FROM frame_hashes WHERE video_id = ?', (int(destino['id']),))
        frames = _frames_por_ids_nube(conn_nube, [int(origen['id'])])
        if frames.get(int(origen['id'])):
            conn_loc.executemany(
                'INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES (?, ?, ?)',
                [(int(destino['id']), pos, h) for pos, h in frames[int(origen['id'])]])
        avance('revision', i, total_versiones, clave)

    # Borrados locales autorizados: no borran archivos reales, sólo catálogo y
    # huellas asociadas mediante la clave foránea CASCADE de SQLite.
    for i, video in enumerate(plan.borrar_local, len(plan.borrar_nube) + 1):
        conn_loc.execute('DELETE FROM frame_hashes WHERE video_id = ?',
                         (int(video['id']),))
        cur = conn_loc.execute(
            'DELETE FROM videos WHERE id = ? AND clave_unica = ? AND fecha_escaneo = ? '
            'AND hash_archivo = ?',
            (int(video['id']), video['clave_unica'], video['fecha_escaneo'],
             video.get('hash_archivo') or ''))
        if cur.rowcount != 1:
            raise ErrorSincronizacion('SQLite cambió durante reconciliación de borrados.')
        avance('eliminar', i, len(plan.borrar_local) + len(plan.borrar_nube),
               video.get('nombre') or video['clave_unica'])
    return subida, bajada, len(plan.borrar_nube) + len(plan.borrar_local)


def _verificar_convergencia(conn_loc: Any, conn_nube: Any) -> dict:
    """Verifica claves/huellas SHA y tombstones, no solamente totales.

    Usa consultas de metadatos y COUNT(*): no reabre vídeos ni vuelve a
    calcular su SHA256. Un desacuerdo pendiente se informa, nunca se fuerza.
    """
    local, nube = _leer_videos(conn_loc), _leer_videos(conn_nube)
    ml, mn = _leer_marcas(conn_loc), _leer_marcas(conn_nube)
    claves_l, claves_n = set(local), set(nube)
    comunes = claves_l & claves_n
    hashes_distintos = sorted(k for k in comunes
                              if local[k].get('hash_archivo') != nube[k].get('hash_archivo'))
    revisiones_distintas = sorted(k for k in comunes
                                  if local[k].get('fecha_escaneo') != nube[k].get('fecha_escaneo'))
    marcas_distintas = sorted(k for k in set(ml) | set(mn) if ml.get(k) != mn.get(k))
    def count(conn, tabla):
        if _es_pg(conn):
            with conn.cursor() as cur:
                cur.execute('SELECT COUNT(*) FROM ' + tabla)
                return int(cur.fetchone()[0])
        return int(conn.execute('SELECT COUNT(*) FROM ' + tabla).fetchone()[0])
    n_frames_local = count(conn_loc, 'frame_hashes')
    n_frames_neon = count(conn_nube, 'frame_hashes')
    return {
        'solo_sqlite': sorted(claves_l - claves_n),
        'solo_neon': sorted(claves_n - claves_l),
        'sha256_distintos': hashes_distintos,
        'fecha_escaneo_distinta': revisiones_distintas,
        'marcas_distintas': marcas_distintas,
        'frames_sqlite': n_frames_local,
        'frames_neon': n_frames_neon,
        'convergencia': not (claves_l ^ claves_n or hashes_distintos or
                            revisiones_distintas or marcas_distintas or
                            n_frames_local != n_frames_neon),
    }


def _generacion_nube(conn: Any) -> str:
    """Identidad de sustitución completa. Sin ella ninguna réplica puede publicar."""
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT generacion FROM vidup_sync_catalogo WHERE id = 1')
            registro = cur.fetchone()
    except Exception as exc:
        raise ErrorSincronizacion(
            'Neon no tiene referencia de catálogo 2.4.5. Tras verificar que la '
            'SQLite del Xiaomi es íntegra, sustituye Neon desde ella con 2.4.5. '
            'No se ha subido ningún dato antiguo.') from exc
    if not registro or not str(registro[0]).strip():
        raise ErrorSincronizacion(
            'Neon todavía no fue declarado catálogo de referencia con VIDUP 2.4.5. '
            'No se permite publicar desde bases locales antiguas.')
    return str(registro[0])


def _catalogos_iguales(local: dict, nube: dict,
                        hashes_local: dict, hashes_nube: dict,
                        marcas_local: dict, marcas_nube: dict) -> bool:
    """Equivalencia exhaustiva sin leer ni recalcular SHA256 de los vídeos."""
    if set(local) != set(nube) or marcas_local != marcas_nube:
        return False
    for clave, fila in local.items():
        remota = nube[clave]
        if any(fila.get(c) != remota.get(c) for c in VIDEO_COLUMNS):
            return False
        if hashes_local.get(int(fila['id']), (0, '')) != hashes_nube.get(
                int(remota['id']), (0, '')):
            return False
    return True


def _respaldo_sqlite_de_rebase(conn_loc: Any, ruta: Path) -> Path:
    """Copia SQLite consistente, incluyendo WAL, antes de reemplazar el catálogo."""
    import sqlite3
    from datetime import datetime
    carpeta = DIR_SCRIPT / 'reportes' / 'respaldos' / 'sincronizacion'
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = carpeta / (f'sqlite_antes_adoptar_neon_{datetime.now():%Y%m%d_%H%M%S_%f}.db')
    try:
        with sqlite3.connect(destino) as copia:
            conn_loc.backup(copia)
            if copia.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ErrorSincronizacion('La copia local previa no pasó integrity_check.')
        return destino
    except Exception:
        destino.unlink(missing_ok=True)
        raise


def _adoptar_catalogo_neon(conn_loc: Any, conn_nube: Any, ruta: Path,
                            generacion: str, videos_nube: dict,
                            marcas_nube: dict) -> Path:
    """Reemplaza SOLO las tablas del catálogo local con copia previa y atomicidad.

    Esta es una operación expresamente confirmada, no forma parte de la
    sincronización automática. No elimina ni modifica archivos multimedia.
    """
    from core.control_sincronizacion import (establecer_generacion, suprimir_eventos)
    copia = _respaldo_sqlite_de_rebase(conn_loc, ruta)
    columnas = ', '.join(VIDEO_COLUMNS)
    marcadores = ','.join('?' for _ in VIDEO_COLUMNS)
    try:
        conn_loc.execute('BEGIN IMMEDIATE')
        suprimir_eventos(conn_loc, True)
        conn_loc.execute('DELETE FROM frame_hashes')
        conn_loc.execute('DELETE FROM videos')
        conn_loc.execute('DELETE FROM videos_eliminados')
        conn_loc.execute('DELETE FROM vidup_cambios_pendientes')
        mapa = {}
        lista = list(videos_nube.values())
        from core.reporter import ProgresoTareas
        suprimir_eventos(conn_loc, True)
        with ProgresoTareas([('descarga', 'Adoptando catálogo Neon')]) as progreso:
            progreso.actualizar('descarga', 0, max(1, len(lista)), 'Preparando copia local')
            for i in range(0, len(lista), LOTE):
                lote = lista[i:i + LOTE]
                fotogramas = _frames_por_ids_nube(conn_nube, [int(v['id']) for v in lote])
                for video in lote:
                    nuevo = conn_loc.execute(
                        f'INSERT INTO videos ({columnas}) VALUES ({marcadores})',
                        tuple(video[c] for c in VIDEO_COLUMNS))
                    id_nuevo = int(nuevo.lastrowid)
                    mapa[int(video['id'])] = id_nuevo
                    frames = fotogramas.get(int(video['id']), [])
                    if frames:
                        conn_loc.executemany(
                            'INSERT INTO frame_hashes (video_id, posicion, hash_valor) VALUES (?, ?, ?)',
                            [(id_nuevo, pos, valor) for pos, valor in frames])
                progreso.actualizar('descarga', min(i + LOTE, len(lista)), max(1, len(lista)),
                                    f'{min(i + LOTE, len(lista))} registros importados')
        if marcas_nube:
            conn_loc.executemany(
                'INSERT INTO videos_eliminados (clave_unica, fecha_eliminacion) VALUES (?, ?)',
                list(marcas_nube.items()))
        establecer_generacion(conn_loc, generacion)
        suprimir_eventos(conn_loc, False)
        if list(conn_loc.execute('PRAGMA foreign_key_check')):
            raise ErrorSincronizacion('El catálogo importado contiene referencias inválidas.')
        if int(conn_loc.execute('SELECT COUNT(*) FROM videos').fetchone()[0]) != len(lista):
            raise ErrorSincronizacion('La descarga no reprodujo el número de vídeos de Neon.')
        if int(conn_loc.execute('SELECT COUNT(*) FROM videos_eliminados').fetchone()[0]) != len(marcas_nube):
            raise ErrorSincronizacion('La descarga no reprodujo las marcas de eliminación.')
        conn_loc.commit()
        return copia
    except BaseException:
        conn_loc.rollback()
        raise


def _restringir_publicacion(plan: PlanSincronizacion, dispositivo_id: str,
                            diario: dict[str, str]) -> None:
    """Solo cambios POSTERIORES a 2.4.5 y del dispositivo actual pueden salir.

    Se incluye cada operación que pudiera mutar Neon: altas, metadatos,
    frames, eliminaciones, reactivaciones y sustituciones completas.
    """
    autorizado_video = {k for k, e in diario.items()
                        if e == 'video' and k.startswith(dispositivo_id + '|')}
    autorizado_baja = {k for k, e in diario.items()
                       if e == 'eliminado' and k.startswith(dispositivo_id + '|')}
    bloqueadas = set()
    def limpiar_videos(videos):
        for v in videos:
            if v['clave_unica'] not in autorizado_video:
                bloqueadas.add(v['clave_unica'])
        return [v for v in videos if v['clave_unica'] in autorizado_video]
    def limpiar_duplas(items, permitidos):
        for k, _ in items:
            if k not in permitidos:
                bloqueadas.add(k)
        return [(k, v) for k, v in items if k in permitidos]
    def limpiar_claves(items, permitidos):
        for k in items:
            if k not in permitidos:
                bloqueadas.add(k)
        return [k for k in items if k in permitidos]
    plan.subir = limpiar_videos(plan.subir)
    plan.meta_nube = limpiar_duplas(plan.meta_nube, autorizado_video)
    plan.frames_nube = limpiar_claves(plan.frames_nube, autorizado_video)
    plan.marcas_nube = limpiar_duplas(plan.marcas_nube, autorizado_baja)
    for video in plan.borrar_nube:
        if video['clave_unica'] not in autorizado_baja:
            bloqueadas.add(video['clave_unica'])
    plan.borrar_nube = [v for v in plan.borrar_nube
                       if v['clave_unica'] in autorizado_baja]
    plan.reactivar_nube = limpiar_claves(plan.reactivar_nube, autorizado_video)
    plan.actualizar_huellas_nube = limpiar_duplas(plan.actualizar_huellas_nube, autorizado_video)
    plan.actualizar_fecha_nube = limpiar_duplas(plan.actualizar_fecha_nube, autorizado_video)
    plan.reemplazar_nube = limpiar_duplas(plan.reemplazar_nube, autorizado_video)
    plan.protegidos.extend(sorted(bloqueadas - set(plan.protegidos)))


def _resumen_discrepancias_marcas(marcas_local: dict, marcas_nube: dict) -> dict:
    """Identifica lápidas exclusivas y fechas divergentes sin alterar el catálogo.

    Las marcas de otro dispositivo NO son cambios que la SQLite actual tenga
    autorización para subir. Esta distinción evita revivir eliminaciones ajenas.
    """
    llaves_local = set(marcas_local)
    llaves_nube = set(marcas_nube)
    return {
        'solo_sqlite': sorted(llaves_local - llaves_nube),
        'solo_neon': sorted(llaves_nube - llaves_local),
        'fecha_distinta': sorted(k for k in llaves_local & llaves_nube
                                 if marcas_local[k] != marcas_nube[k]),
    }


def _plan_solo_marcas_historicas(plan: PlanSincronizacion, local: dict,
                                 nube: dict) -> PlanSincronizacion:
    """Ofrece copiar solo lápidas de Neon sin tocar vídeos ni huellas.

    No se importa una marca si cualquiera de los catálogos tiene el vídeo
    activo: requeriría decidir si prevalece escaneo o borrado y, eventualmente,
    eliminar un registro. Esa resolución siempre exige el plan completo.
    """
    return PlanSincronizacion(marcas_local=[(clave, fecha)
        for clave, fecha in plan.marcas_local
        if clave not in local and clave not in nube])


def _elegir_solucion_discrepancias(plan: PlanSincronizacion, local: dict,
                                    nube: dict, marcas_local: dict,
                                    marcas_nube: dict, archivo_txt: Optional[Path]) -> str:
    """Pide UNA decisión explícita y muestra alternativas de menor a mayor riesgo.

    Devuelve plan/marcas/adoptar/informe/reubicaciones/salir. No ejecuta operaciones por sí
    misma; una adopción sigue exigiendo una frase completa y respaldo íntegro.
    """
    from core.presentacion import pedir_texto, RegresarAlMenu
    diferencia = _resumen_discrepancias_marcas(marcas_local, marcas_nube)
    imprimir('[bold gold1]Se encontraron discrepancias entre SQLite y Neon.[/]')
    imprimir('  Marcas solo SQLite: {0} · solo Neon: {1} · fechas distintas: {2}'.format(
        len(diferencia['solo_sqlite']), len(diferencia['solo_neon']),
        len(diferencia['fecha_distinta'])))
    if archivo_txt:
        from core.presentacion import rich_escape
        imprimir(f'  [cyan]Detalle y explicación: {rich_escape(str(archivo_txt))}[/]')
    imprimir('[bold bright_cyan]Alternativas para resolverlas:[/]')
    imprimir('  [spring_green2]1[/] Aplicar los cambios seguros del plan, previa confirmación.')
    imprimir('  [turquoise2]2[/] Copiar SOLO marcas históricas de Neon que no correspondan a vídeos activos; no borrar vídeos.')
    imprimir('  [orange1]3[/] Adoptar TODO el catálogo de Neon en esta SQLite, con respaldo íntegro.')
    imprimir('  [sky_blue1]4[/] Conservar bases intactas y consultar el informe TXT.')
    imprimir('  [medium_spring_green]5[/] Conciliar reubicaciones propias: publicar marcas de rutas anteriores y retirar identificadores temporales, sin borrar vídeos.')
    imprimir('  [light_sky_blue1]6[/] Completar huellas y metadatos faltantes desde Neon sin sustituir vídeos.')
    imprimir('  [grey70]0[/] Cancelar sin cambios.')
    for _ in range(5):
        try:
            valor = pedir_texto('Elige cómo resolver las diferencias (0-6)', '1',
                                permitir_escape=False).strip()
        except (RegresarAlMenu, KeyboardInterrupt):
            return 'salir'
        respuesta = {'1': 'plan', '2': 'marcas', '3': 'adoptar',
                     '4': 'informe', '5': 'reubicaciones', '6': 'completar', '0': 'salir'}.get(valor)
        if respuesta:
            return respuesta
        imprimir('[yellow]Selecciona 0, 1, 2, 3, 4, 5 o 6. Ninguna opción se ejecuta automáticamente.[/]')
    return 'salir'

def sincronizar_local_y_nube(*, confirmar: bool = True,
                            ruta_local: Optional[str] = None,
                            solo_plan: bool = False,
                            permitir_eliminaciones: bool = False) -> Tuple[int, int, int]:
    """Sincronización con generación canónica y diario de cambios propios.

    En errores lanza ErrorSincronizacion; NUNCA devuelve (0,0,0) como éxito falso.
    `solo_plan` sirve para inspeccionar sin mutaciones (salvo inicializar SQLite
    si aún no existe; la base de escritorio se debe conservar intacta).
    """
    if os.environ.get('VIDUP_FORZAR_OFFLINE', '').strip().lower() in ('1', 'true', 'yes', 'sí'):
        raise ErrorSincronizacion('La sincronización está deshabilitada por VIDUP_FORZAR_OFFLINE.')
    if not _usa_neon():
        raise ErrorSincronizacion('No hay credenciales o controlador para Neon.')

    ruta = Path(ruta_local) if ruta_local else obtener_ruta_db_local_dispositivo()
    if not ruta.is_file():
        raise ErrorSincronizacion(
            f'No existe la SQLite local {ruta}. Escanea primero o selecciona otra base; '
            'no se creará una base vacía durante una sincronización.')

    conn_loc = None
    conn_nube = None
    nube_confirmada = False
    try:
        conn_loc = conectar_sqlite(str(ruta))
        conn_nube = _conectar_nube_directa()
        from core.control_sincronizacion import (estado_generacion,
            establecer_generacion, pendientes, marcar_sincronizados, suprimir_eventos)
        # Congelar lectura consistente del catálogo durante todo el plan.
        if hasattr(conn_nube, 'set_session'):
            conn_nube.set_session(isolation_level='REPEATABLE READ')
        with conn_nube.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_xact_lock(%s)', (24061111,))
            bloqueo = cur.fetchone()
            if not bloqueo or not bloqueo[0]:
                raise ErrorSincronizacion('Otro VIDUP está sincronizando; inténtalo después.')
        generacion = _generacion_nube(conn_nube)
        # La guarda de PostgreSQL impide que los clientes 2.4.4 o anteriores
        # vuelvan a escribir en el catálogo, incluso si siguen instalados.
        if hasattr(conn_nube, 'get_backend_pid'):
            with conn_nube.cursor() as cur:
                cur.execute('SELECT set_config(%s, %s, true)',
                            ('vidup.catalog_generation', generacion))
        locales = _leer_videos(conn_loc)
        remotos = _leer_videos(conn_nube)
        hashes_local = _huellas_resumen(conn_loc)
        hashes_nube = _huellas_resumen(conn_nube)
        marcas_locales = _leer_marcas(conn_loc)
        marcas_remotas = _leer_marcas(conn_nube)
        anterior = estado_generacion(conn_loc)
        if anterior != generacion:
            if _catalogos_iguales(locales, remotos, hashes_local, hashes_nube,
                                  marcas_locales, marcas_remotas):
                # La fuente usada para reemplazar Neon (p. ej. Xiaomi).
                establecer_generacion(conn_loc, generacion)
                conn_loc.commit()
                imprimir('[bold spring_green2]Catálogo local idéntico a Neon; '
                         'referencia reconocida sin copiar ni borrar vídeos.[/]')
                return 0, 0, 0
            imprimir('[bold orange1]Esta SQLite pertenece a una generación anterior '
                     'a la referencia actual de Neon. Se BLOQUEA la subida.[/]')
            # Informe exhaustivo: metadatos, huella de audio y fotogramas.
            # No basta con contrastar claves ni los totales del catálogo.
            from core.auditoria_sincronizacion import (
                construir_auditoria, guardar_auditoria)
            informe = construir_auditoria(
                local=locales, neon=remotos,
                frames_local=hashes_local, frames_neon=hashes_nube,
                marcas_local=marcas_locales, marcas_neon=marcas_remotas,
                columnas=VIDEO_COLUMNS, generacion=generacion,
                ruta_sqlite=str(ruta))
            categorias = informe['categorias']
            imprimir(f'[cyan]SQLite: {len(locales)} vídeos · Neon: {len(remotos)} vídeos.[/]')
            imprimir(f'[gold1]Diferencias: solo SQLite {categorias["solo_sqlite"]}, '
                     f'solo Neon {categorias["solo_neon"]}, '
                     f'versiones distintas {categorias["datos_diferentes"]}.[/]')
            imprimir(f'[spring_green2]Huellas visuales por vídeo: '
                     f'SQLite {informe["resumen_sqlite"]["con_visual"]}, '
                     f'Neon {informe["resumen_neon"]["con_visual"]}.[/]')
            imprimir(f'[spring_green2]Huellas de audio por vídeo: '
                     f'SQLite {informe["resumen_sqlite"]["con_audio"]}, '
                     f'Neon {informe["resumen_neon"]["con_audio"]}.[/]')
            json_aud, txt_aud = guardar_auditoria(
                informe, DIR_SCRIPT / 'reportes' / 'sincronizacion', 'adopcion_neon')
            imprimir(f'[cyan]Informe legible (TXT): {txt_aud}[/]')
            imprimir(f'[cyan]Detalle técnico (JSON): {json_aud}[/]')
            if categorias['conflictos_de_valores'] or categorias['datos_faltantes_en_neon']:
                imprimir('[bold yellow]Atención: algunos registros contienen conflictos '
                         'o información que Neon no tiene. Revise el TXT antes de adoptar.[/]')
            elif (categorias['datos_faltantes_en_sqlite'] and
                  not categorias['solo_sqlite'] and not categorias['solo_neon'] and
                  not informe['marcas_diferentes']):
                imprimir('[green]Las diferencias se deben a datos faltantes en SQLite '
                         'que sí están en Neon. Revise el TXT para ver cada campo.[/]')
            # El control por generación sigue bloqueando CUALQUIER subida:
            # ofrecemos solo completar datos locales ausentes antes de proponer
            # una sustitución total. No se interpretan registros exclusivos
            # como archivos físicos inexistentes.
            from core.reconciliacion_selectiva import (
                construir_plan as _plan_selectivo, guardar_plan as _guardar_selectivo,
                aplicar_plan as _aplicar_selectivo)
            propuesta = _plan_selectivo(locales,remotos,hashes_local,hashes_nube,
                                        marcas_locales,marcas_remotas)
            ruta_plan_json,ruta_plan_txt = _guardar_selectivo(
                propuesta, DIR_SCRIPT/'reportes'/'sincronizacion')
            imprimir(f'[bold turquoise2]Se pueden completar datos de '
                     f'{len(propuesta["actualizaciones"])} registros sin sustituir bases.[/]')
            imprimir(f'[gold1]Conflictos a revisar: {len(propuesta["bloqueados"])} · '
                     f'marcas distintas: {len(propuesta["marcas_diferentes"])}.[/]')
            imprimir(f'[cyan]Explicación de opciones TXT: {ruta_plan_txt}[/]')
            imprimir('[orange1]La adopción TOTAL sigue disponible, pero no se recomienda '
                     'automáticamente cuando hay registros exclusivos o conflictos.[/]')
            if solo_plan or not confirmar:
                raise ErrorSincronizacion('Referencia distinta: adopción pendiente u otras opciones; informe generado, ninguna escritura.')
            from core.presentacion import pedir_texto, confirma_frase
            from core.interfaz import menu_rich
            menu_rich('Resolver generación anterior · sin subir datos antiguos', [
                ('1','Completar en SQLite únicamente huellas y campos faltantes de registros idénticos'),
                ('2','Adoptar TODO Neon, con respaldo; retira registros locales exclusivos'),
                ('3','Consultar informes y conservar ambas bases intactas'),
                ('0','Cancelar y conservar ambas bases'),
                ('00','Volver al menú principal')],
                subtitulo='La generación seguirá bloqueada hasta verificar equivalencia integral.')
            decision = pedir_texto('Elige una alternativa','3',permitir_escape=False).strip()
            if decision == '1':
                if not propuesta['actualizaciones']:
                    imprimir('[yellow]No hay enriquecimientos seguros. Revisa el TXT; '
                             'no se modificó ninguna base.[/]')
                    return 0,0,0
                frase=pedir_texto('Escribe COMPLETAR DATOS para aplicar solo esos campos',
                                  '',permitir_escape=False)
                if not confirma_frase(frase,'COMPLETAR DATOS'):
                    raise SincronizacionCancelada('No se autorizaron los enriquecimientos.')
                resultado=_aplicar_selectivo(conn_loc,conn_nube,propuesta,str(ruta),
                                DIR_SCRIPT/'reportes')
                imprimir(f'[spring_green2]Se completaron datos de '
                         f'{resultado["aplicados"]} registros.[/] '
                         f'Respaldo: {resultado["respaldo"]}')
                # NO aceptar la generación hasta comprobar equivalencia total.
                locales_nuevos=_leer_videos(conn_loc)
                hashes_nuevos=_huellas_resumen(conn_loc)
                if _catalogos_iguales(locales_nuevos,remotos,hashes_nuevos,hashes_nube,
                                      _leer_marcas(conn_loc),marcas_remotas):
                    establecer_generacion(conn_loc,generacion)
                    conn_loc.commit()
                    imprimir('[spring_green2]Catálogos equivalentes: generación reconocida.[/]')
                else:
                    imprimir('[yellow]Quedan diferencias. La subida continúa BLOQUEADA; '
                             'consulte el informe para resolver los casos restantes.[/]')
                return 0,resultado['aplicados'],0
            if decision != '2':
                raise SincronizacionCancelada('Sin modificaciones. TXT disponible para revisar.')
            frase=pedir_texto('Escribe ADOPTAR NEON para sustituir TODO el catálogo local',
                              '',permitir_escape=False)
            if not confirma_frase(frase,'ADOPTAR NEON'):
                raise SincronizacionCancelada('Confirmación incorrecta; se conservó SQLite.')
            respaldo=_adoptar_catalogo_neon(conn_loc,conn_nube,ruta,generacion,remotos,marcas_remotas)
            imprimir(f'[bold spring_green2]SQLite adoptó Neon tras autorización explícita.[/] '
                     f'Respaldo: {respaldo}')
            return 0,len(remotos),0
        # Incluso con la misma generación, generar diagnóstico legible cuando
        # hay diferencias. Un plan de operaciones no explica por sí solo qué
        # huella, fecha o metadato difiere, y no debe inducir a sustituir datos.
        hay_discrepancias = not _catalogos_iguales(
            locales, remotos, hashes_local, hashes_nube, marcas_locales, marcas_remotas)
        ruta_txt = None
        if hay_discrepancias:
            from core.auditoria_sincronizacion import (
                construir_auditoria, guardar_auditoria)
            diagnostico = construir_auditoria(
                local=locales, neon=remotos,
                frames_local=hashes_local, frames_neon=hashes_nube,
                marcas_local=marcas_locales, marcas_neon=marcas_remotas,
                columnas=VIDEO_COLUMNS, generacion=generacion,
                ruta_sqlite=str(ruta))
            ruta_json, ruta_txt = guardar_auditoria(
                diagnostico, DIR_SCRIPT / 'reportes' / 'sincronizacion',
                'comparacion_neon')
            imprimir(f'[cyan]Diagnóstico de diferencias TXT: {ruta_txt}[/]')
            imprimir(f'[cyan]Diagnóstico detallado JSON: {ruta_json}[/]')
        # Solo los cambios capturados por VIDUP 2.4.5 desde esta generación
        # pueden salir; nunca un catálogo histórico ni copias ajenas.
        diario = pendientes(conn_loc)
        identidad = info_dispositivo_actual()['dispositivo_id']
        # Una marca local no publicada no puede prevalecer sin evento pendiente.
        # La política de publicación solo reconoce borrados locales propios
        # registrados como evento pendiente. Las marcas de Neon se tratan como
        # fuente remota real, NO como si ya existieran en SQLite.
        marcas_locales_publicables = {
            k: v for k, v in marcas_locales.items()
            if diario.get(k) == 'eliminado' and k.startswith(identidad + '|')
        }
        marcas_bloqueo = set(marcas_locales_publicables) | set(marcas_remotas)
        conn_loc.execute('BEGIN IMMEDIATE')
        plan = calcular_plan(locales, remotos, hashes_local, hashes_nube,
                            marcas_bloqueo)
        _planear_marcas(plan, locales, remotos,
                       marcas_locales_publicables, marcas_remotas,
                       autorizar_borrados_nube=permitir_eliminaciones,
                       frames_local=hashes_local, frames_nube=hashes_nube)
        _restringir_publicacion(plan, identidad, diario)
        _imprimir_plan(plan, len(locales), len(remotos))
        archivo_plan = _guardar_plan(plan, ruta, len(locales), len(remotos))
        imprimir(f'[cyan]Plan verificable JSON: {archivo_plan}[/]')
        imprimir(f'[cyan]Plan legible TXT: {archivo_plan.parent.parent / "txt" / (archivo_plan.stem + ".txt")}[/]')

        if solo_plan:
            if hay_discrepancias:
                imprimir('[bold orange1]Solo diagnóstico: existen diferencias, aun si '
                         'el plan no contiene acciones aplicables.[/]')
            return 0, 0, 0
        if confirmar and hay_discrepancias:
            decision = _elegir_solucion_discrepancias(
                plan, locales, remotos, marcas_locales, marcas_remotas, ruta_txt)
            if decision in ('salir', 'informe'):
                conn_loc.rollback()
                imprimir('[yellow]Sin cambios. Las diferencias siguen pendientes; '
                         'consulta el informe TXT antes de decidir.[/]')
                return 0, 0, 0
            if decision == 'adoptar':
                # La copia SQLite se genera antes de cualquier modificación.
                # No realizar backup mientras hay una escritura abierta.
                conn_loc.rollback()
                from core.presentacion import pedir_texto, confirma_frase
                texto = pedir_texto('Escribe ADOPTAR NEON para sustituir todo el '
                                    'catálogo SQLite (sin tocar vídeos físicos)',
                                    '', permitir_escape=False)
                if not confirma_frase(texto, 'ADOPTAR NEON'):
                    raise SincronizacionCancelada('Adopción no autorizada; SQLite intacta.')
                respaldo = _adoptar_catalogo_neon(
                    conn_loc, conn_nube, ruta, generacion, remotos, marcas_remotas)
                imprimir(f'[bold spring_green2]Catálogo local adoptado desde Neon.[/] '
                         f'Respaldo completo: {respaldo}')
                return 0, len(remotos), 0
            if decision == 'reubicaciones':
                # Ninguna copia general del catálogo: únicamente las rutas
                # anteriores verificadas por auditorías del propio dispositivo.
                from core.conciliacion_reubicaciones import (
                    construir_plan_reubicaciones, aplicar_conciliacion)
                from core.presentacion import pedir_texto, confirma_frase
                conn_loc.rollback()
                plan_rutas = construir_plan_reubicaciones(
                    dispositivo_id=identidad, local=locales, neon=remotos,
                    marcas_local=marcas_locales, marcas_neon=marcas_remotas,
                    reportes=DIR_SCRIPT / 'reportes')
                imprimir('[bold turquoise2]Conciliación de reubicaciones del dispositivo actual[/]')
                imprimir(f'  Marcas temporales verificadas para retirar de SQLite: '
                         f'{len(plan_rutas.tecnicas_a_quitar)}')
                imprimir(f'  Marcas históricas de rutas anteriores para publicar en Neon: '
                         f'{len(plan_rutas.historicas_a_neon)}')
                imprimir(f'  Casos no verificables, que se conservarán: '
                         f'{len(plan_rutas.no_resueltas)}')
                imprimir('[yellow]No se eliminan vídeos físicos, registros activos ni '
                         'huellas. No se recalcula SHA256.[/]')
                if plan_rutas.cambios:
                    frase = pedir_texto('Escribe CONCILIAR REUBICACIONES para aplicar '
                                       'únicamente estos cambios', '', permitir_escape=False)
                    if not confirma_frase(frase, 'CONCILIAR REUBICACIONES'):
                        raise SincronizacionCancelada('No se modificaron las marcas de reubicación.')
                rutas = aplicar_conciliacion(
                    conn_loc=conn_loc, conn_neon=conn_nube, plan=plan_rutas,
                    reportes=DIR_SCRIPT / 'reportes',
                    dispositivo_id=identidad, generacion=generacion)
                from core.presentacion import rich_escape
                imprimir(f'[cyan]Informe TXT: {rich_escape(str(rutas[1]))}[/]')
                imprimir(f'[cyan]Auditoría JSON: {rich_escape(str(rutas[0]))}[/]')
                if plan_rutas.cambios:
                    imprimir('[bold spring_green2]Marcas de reubicación conciliadas. '
                             'Los vídeos vigentes y sus huellas no se modificaron.[/]')
                else:
                    imprimir('[yellow]No había cambios de reubicación verificables. '
                             'Consulta los casos pendientes en el TXT.[/]')
                revision = _verificar_convergencia(conn_loc, conn_nube)
                if revision['convergencia']:
                    imprimir('[bold spring_green2]Los catálogos y las marcas coinciden.[/]')
                else:
                    imprimir('[yellow]Persisten diferencias ajenas a estas reubicaciones. '
                             'Revisa el diagnóstico general.[/]')
                return 0, 0, 0
            if decision == 'completar':
                from core.reconciliacion_selectiva import (
                    construir_plan as _plan_selectivo, guardar_plan as _guardar_selectivo,
                    aplicar_plan as _aplicar_selectivo)
                from core.presentacion import pedir_texto, confirma_frase
                conn_loc.rollback()
                pro=_plan_selectivo(locales,remotos,hashes_local,hashes_nube,
                                    marcas_locales,marcas_remotas)
                _,archivo=_guardar_selectivo(pro,DIR_SCRIPT/'reportes'/'sincronizacion')
                imprimir(f'[cyan]Plan de enriquecimientos: {archivo}[/]')
                if not pro['actualizaciones']:
                    imprimir('[yellow]No hay datos faltantes seguros que copiar.[/]')
                    return 0,0,0
                frase=pedir_texto('Escribe COMPLETAR DATOS para actualizar SQLite',
                                  '',permitir_escape=False)
                if not confirma_frase(frase,'COMPLETAR DATOS'):
                    raise SincronizacionCancelada('Sin cambios.')
                result=_aplicar_selectivo(conn_loc,conn_nube,pro,str(ruta),
                                          DIR_SCRIPT/'reportes')
                imprimir(f'[spring_green2]{result["aplicados"]} registros enriquecidos.[/] '
                         f'Respaldo: {result["respaldo"]}')
                return 0,result['aplicados'],0
            if decision == 'marcas':
                plan = _plan_solo_marcas_historicas(plan, locales, remotos)
                imprimir(f'[cyan]Modo marcas históricas: {len(plan.marcas_local)} '
                         'marcas copiables desde Neon; ninguna eliminación de vídeos.[/]')
        if plan.cambios == 0:
            conn_loc.rollback()
            if hay_discrepancias:
                pendientes_marcas = _resumen_discrepancias_marcas(marcas_locales, marcas_remotas)
                imprimir('[bold orange1]Sin operaciones seguras, pero las bases NO son '
                         'idénticas. No se han ocultado las discrepancias.[/]')
                imprimir('  Marcas sin conciliar: {0}'.format(
                    len(set(pendientes_marcas['solo_sqlite']) |
                        set(pendientes_marcas['solo_neon']) |
                        set(pendientes_marcas['fecha_distinta']))))
                imprimir('[yellow]Alternativas: consultar el TXT, revisar la autoridad '
                         'de las marcas o adoptar explícitamente Neon con respaldo.[/]')
            elif plan.conflictos or plan.conflictos_eliminacion or plan.protegidos:
                imprimir('[yellow]Sin cambios seguros; persisten bloqueos del plan.[/]')
            else:
                imprimir('[green]Catálogos ya idénticos; no hay cambios pendientes.[/]')
            return 0, 0, 0

        if (plan.borrar_nube or plan.borrar_local) and not permitir_eliminaciones:
            raise ErrorSincronizacion('Las eliminaciones requieren autorización explícita.')
        if confirmar:
            from core.presentacion import pedir_si_no, pedir_texto
            if not pedir_si_no('¿Aplicar el plan verificado de sincronización?', False):
                raise SincronizacionCancelada('No se aplicaron cambios.')
            if plan.borrar_nube or plan.borrar_local:
                frase = pedir_texto(
                    f'Se eliminarán {len(plan.borrar_nube)} registros en Neon y '
                    f'{len(plan.borrar_local)} en SQLite (no archivos físicos). '
                    'Escribe CONFIRMAR ELIMINACIONES', '', permitir_escape=False)
                from core.presentacion import confirma_frase
                if not confirma_frase(frase, 'CONFIRMAR ELIMINACIONES'):
                    raise SincronizacionCancelada('No se autorizó la eliminación en Neon.')
        elif (plan.borrar_nube or plan.borrar_local) and not os.environ.get('VIDUP_AUTORIZAR_BORRADO_TEST'):
            # Una API sin diálogo no puede habilitar borrados por accidente.
            raise ErrorSincronizacion('Los borrados requieren confirmación interactiva.')
        copia = _copia_previa_borrados_nube(conn_nube, plan.borrar_nube)
        if copia:
            imprimir(f'[bold gold1]Recuperación de registros Neon guardada en: {copia}[/]')

        # La descarga y la copia de lápidas procedentes de Neon no son eventos
        # nuevos del dispositivo. Mantener sus triggers deshabilitados hasta
        # terminar evita que una simple sincronización invente borrados locales.
        suprimir_eventos(conn_loc, True)
        from core.reporter import ProgresoTareas
        with ProgresoTareas([
            ('subir', 'Envío a Neon'), ('bajar', 'Descarga a SQLite'),
            ('marcas', 'Reconciliación de marcas'), ('eliminar', 'Borrado de registros'),
            ('reactivar', 'Reescaneos posteriores'),
            ('huellas', 'Actualización de huellas'),
            ('fechas', 'Fechas de escaneo'), ('revision', 'Sustitución real'),
        ]) as progreso:
            subida, bajada, eliminados = _aplicar_plan(
                conn_loc, conn_nube, plan, locales, remotos, progreso=progreso)
        suprimir_eventos(conn_loc, False)
        with conn_nube.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM videos')
            total_nube = int(cur.fetchone()[0])
        total_local = int(conn_loc.execute('SELECT COUNT(*) FROM videos').fetchone()[0])
        if (total_local != len(locales) + len(plan.bajar) - len(plan.borrar_local) or
                total_nube != len(remotos) + len(plan.subir) - len(plan.borrar_nube)):
            raise ErrorSincronizacion('Los conteos finales no corresponden al plan. Se canceló.')
        if list(conn_loc.execute('PRAGMA foreign_key_check')):
            raise ErrorSincronizacion('La base local tiene huellas huérfanas. Se canceló.')
        auditoria = _verificar_convergencia(conn_loc, conn_nube)
        if plan.conflictos or plan.conflictos_eliminacion:
            auditoria['convergencia'] = False
        # PostgreSQL primero; un error posterior de SQLite no se anuncia como
        # éxito: las altas son idempotentes y se recuperan en el siguiente intento.
        # Solo se retiran del diario cambios que ya coinciden remotamente;
        # los conflictos permanecen pendientes, nunca se descartan.
        remotos_final = _leer_videos(conn_nube)
        marcas_final = _leer_marcas(conn_nube)
        locales_final = _leer_videos(conn_loc)
        marcas_loc_final = _leer_marcas(conn_loc)
        digest_final_loc = _huellas_resumen(conn_loc)
        digest_final_neon = _huellas_resumen(conn_nube)
        confirmadas = set()
        for clave, evento in diario.items():
            if not clave.startswith(identidad + '|'):
                continue
            a, b = locales_final.get(clave), remotos_final.get(clave)
            if evento == 'eliminado':
                if not a and not b and marcas_loc_final.get(clave) == marcas_final.get(clave) \
                        and clave in marcas_final:
                    confirmadas.add(clave)
            elif (a is not None and b is not None and all(
                    a.get(c) == b.get(c) for c in VIDEO_COLUMNS)
                    and digest_final_loc.get(int(a['id']), (0, '')) ==
                        digest_final_neon.get(int(b['id']), (0, ''))):
                confirmadas.add(clave)
        marcar_sincronizados(conn_loc, confirmadas)
        conn_nube.commit()
        nube_confirmada = True
        conn_loc.commit()
        imprimir(f'[green]Sincronización aplicada: {subida} subidas, {bajada} bajadas, '
                 f'{eliminados} eliminaciones en Neon.[/]')
        # Los totales no garantizan identidad: también se comparan marcas y
        # huellas, y las diferencias no resueltas permanecen como conflictos.
        resumen_sqlite = (total_local, int(conn_loc.execute('SELECT COUNT(*) FROM frame_hashes').fetchone()[0]),
                          int(conn_loc.execute('SELECT COUNT(*) FROM videos_eliminados').fetchone()[0]))
        with conn_nube.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM frame_hashes')
            n_frames = int(cur.fetchone()[0])
            cur.execute('SELECT COUNT(*) FROM videos_eliminados')
            n_marcas = int(cur.fetchone()[0])
        resumen_neon = (total_nube, n_frames, n_marcas)
        imprimir(f'[bold turquoise2]Totales SQLite[/]: vídeos {resumen_sqlite[0]}, '
                 f'frames {resumen_sqlite[1]}, eliminaciones {resumen_sqlite[2]}')
        imprimir(f'[bold light_sky_blue1]Totales Neon[/]: vídeos {resumen_neon[0]}, '
                 f'frames {resumen_neon[1]}, eliminaciones {resumen_neon[2]}')
        if auditoria['convergencia']:
            imprimir('[bold spring_green2]Catálogos conciliados: coinciden claves, SHA256, '
                     'fechas, marcas de eliminación y total de huellas visuales.[/]')
        else:
            imprimir('[bold orange1]Las bases todavía no son idénticas.[/]')
            imprimir('  Solo SQLite: {0} · Solo Neon: {1} · SHA256 distintos: {2}'.format(
                len(auditoria['solo_sqlite']), len(auditoria['solo_neon']),
                len(auditoria['sha256_distintos'])))
            imprimir('  Revisiones distintas: {0} · Marcas distintas: {1} · '
                     'Frames SQLite/Neon: {2}/{3}'.format(
                len(auditoria['fecha_escaneo_distinta']), len(auditoria['marcas_distintas']),
                auditoria['frames_sqlite'], auditoria['frames_neon']))
            imprimir('[yellow]Consulta los conflictos del plan. No se igualaron datos a la fuerza.[/]')
        if plan.conflictos:
            imprimir(f'[yellow]Quedan {len(plan.conflictos)} conflictos sin sobrescribir.[/]')
        return subida, bajada, eliminados
    except (SincronizacionCancelada, ErrorSincronizacion):
        raise
    except Exception as exc:
        sufijo = (' Los cambios confirmados en Neon pueden mantenerse y son '
                  'recuperables en un reintento.' if nube_confirmada else '')
        raise ErrorSincronizacion(f'Fallo real de sincronización: {exc}.{sufijo}') from exc
    finally:
        for conexion in (conn_nube, conn_loc):
            if conexion is not None:
                try:
                    conexion.rollback()
                except Exception:
                    pass
                cerrar(conexion)


def sincronizar_al_iniciar() -> None:
    """Desactivada para preservar catálogos locales existentes.

    La sincronización necesita revisión humana del plan en Configuración > 7.
    """
    return
