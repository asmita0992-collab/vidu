"""
Comparación de duplicados exactos, visuales y fragmentos.

La comparación visual requiere que ambas huellas se hayan generado
con el mismo intervalo temporal de muestreo.
"""

import math

from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

from .fingerprint import VideoFingerprint


UMBRAL_PHASH = 10
UMBRAL_VISUAL = 0.80
UMBRAL_FRAGMENTO = 0.75
UMBRAL_POSIBLE_FRAGMENTO = 0.25
MARGEN_DURACION = 0.05
RATIO_MIN_FRAGMENTO = 0.05

ProgresoPar = Optional[
    Callable[[int, int], None]
]


@dataclass
class ResultadoComparacion:
    tipo: str
    ruta_a: str
    nombre_a: str
    ruta_b: str
    nombre_b: str
    similitud_visual: float
    similitud_audio: float
    confianza: float
    offset_inicio: Optional[float] = None
    offset_fin: Optional[float] = None
    notas: str = ''

    # Información de cada video para justificar la decisión.
    info_a: Dict[str, Any] = field(default_factory=dict)
    info_b: Dict[str, Any] = field(default_factory=dict)

    # Recomendación de eliminar/conservar.
    # Vacío si el caso no admite recomendación automática.
    recomendacion: Dict[str, Any] = field(default_factory=dict)


# ==================================================
# Utilidades internas
# ==================================================

def _mismo_archivo_fisico(
    ruta_a: str,
    ruta_b: str,
) -> bool:
    """
    Protege frente a alias del mismo archivo local, incluso si
    llegaron de bases de datos adicionales diferentes.

    No presupone que rutas importadas desde otros dispositivos
    sean accesibles localmente.
    """
    # La igualdad textual de rutas NO implica el mismo archivo entre dispositivos.
    if ruta_a == ruta_b:
        return True

    try:
        if Path(ruta_a).resolve() == Path(ruta_b).resolve():
            return True
    except (OSError, ValueError):
        pass

    try:
        a = Path(ruta_a).stat()
        b = Path(ruta_b).stat()
    except (OSError, ValueError):
        return False

    if (
        not a.st_ino
        or not b.st_ino
    ):
        return False

    return (
        a.st_dev == b.st_dev
        and a.st_ino == b.st_ino
    )


def _hash_a_entero(
    valor: str,
) -> Optional[int]:
    if (
        not isinstance(valor, str)
        or len(valor) != 16
    ):
        return None

    try:
        return int(valor, 16)
    except ValueError:
        return None


def _hamming(
    h1: str,
    h2: str,
) -> int:
    a = _hash_a_entero(h1)
    b = _hash_a_entero(h2)

    if (
        a is None
        or b is None
    ):
        return 65

    return (a ^ b).bit_count()


def _similitud_sec(
    s1: List[str],
    s2: List[str],
    umbral: int = UMBRAL_PHASH,
) -> float:
    """
    Compara secuencias posición a posición.

    Divide por la longitud mayor para penalizar secuencias
    incompletas.
    """
    total = max(len(s1), len(s2))

    if total == 0:
        return 0.0

    aciertos = sum(
        _hamming(a, b) <= umbral
        for a, b in zip(s1, s2)
    )

    return aciertos / total


def _similitud_audio(
    a: Optional[str],
    b: Optional[str],
) -> float:
    """
    Compara huellas de audio del mismo método.

    Esta función sirve para videos completos alineados desde
    su inicio. No alinea automáticamente fragmentos.
    """
    if (
        not a
        or not b
        or ':' not in a
        or ':' not in b
    ):
        return -1.0

    tipo_a, datos_a = a.split(':', 1)
    tipo_b, datos_b = b.split(':', 1)

    if tipo_a != tipo_b:
        return -1.0

    if tipo_a == 'rms':
        try:
            ba = bytes.fromhex(datos_a)
            bb = bytes.fromhex(datos_b)

            n = min(len(ba), len(bb))

            if n == 0:
                return -1.0

            diferencia = sum(
                abs(x - y)
                for x, y in zip(ba[:n], bb[:n])
            )

            return max(
                0.0,
                1.0 - diferencia / (n * 255),
            )

        except (ValueError, TypeError):
            return -1.0

    if tipo_a == 'chroma':
        try:
            va = [
                int(x)
                for x in datos_a.split(',')
                if x.strip()
            ]
            vb = [
                int(x)
                for x in datos_b.split(',')
                if x.strip()
            ]

            n = min(len(va), len(vb))

            if n == 0:
                return -1.0

            iguales = sum(
                32 - (
                    (va[i] ^ vb[i]) & 0xFFFFFFFF
                ).bit_count()
                for i in range(n)
            )

            return max(
                0.0,
                min(1.0, iguales / (n * 32)),
            )

        except (ValueError, TypeError):
            return -1.0

    return -1.0


def _buscar_fragmento(
    largo: List[str],
    corto: List[str],
    umbral: int = UMBRAL_PHASH,
    min_sim_abs: float = 0.15,
    progreso: ProgresoPar = None,
) -> Optional[Tuple[int, float]]:
    """
    Busca el corto dentro del largo revisando cada desplazamiento.

    Descarta anticipadamente posiciones incapaces de superar el
    mejor resultado ya encontrado.
    """
    nl = len(largo)
    nc = len(corto)

    if nc < 4 or nc > nl:
        return None

    largos = [_hash_a_entero(h) for h in largo]
    cortos = [_hash_a_entero(h) for h in corto]

    posiciones = nl - nc + 1

    aciertos_minimos = max(
        2,
        math.ceil(min_sim_abs * nc),
    )

    mejor_pos = -1
    mejor_aciertos = aciertos_minimos - 1

    paso_aviso = max(1, posiciones // 200)

    for pos in range(posiciones):
        aciertos = 0

        for i in range(nc):
            h1 = largos[pos + i]
            h2 = cortos[i]

            if (
                h1 is not None
                and h2 is not None
                and (h1 ^ h2).bit_count() <= umbral
            ):
                aciertos += 1

            restantes = nc - i - 1

            if aciertos + restantes <= mejor_aciertos:
                break

        if aciertos > mejor_aciertos:
            mejor_aciertos = aciertos
            mejor_pos = pos

        if (
            progreso is not None
            and (
                pos % paso_aviso == 0
                or pos + 1 == posiciones
            )
        ):
            progreso(pos + 1, posiciones)

    if mejor_pos < 0:
        return None

    return (mejor_pos, mejor_aciertos / nc)


def _confianza(
    similitud_visual: float,
    similitud_audio: float,
) -> float:
    if similitud_audio < 0:
        return similitud_visual

    return (
        similitud_visual * 0.7
        + similitud_audio * 0.3
    )


# ==================================================
# Recomendación de eliminar/conservar
# ==================================================

def _normalizar_codec(codec: str) -> str:
    """
    Normaliza nombres de códec para comparación.

    hevc y h265 se consideran equivalentes.
    """
    c = (codec or '').strip().lower()
    return 'hevc' if c in ('hevc', 'h265', 'h.265') else c


def _prioridad_codec(codec: str) -> int:
    """
    Devuelve un número mayor para códecs con mejor compresión/calidad
    a igualdad de bitrate.

    Criterio pedido: hevc (h265) gana.
    Después el resto por orden descendente de eficiencia conocida.
    """
    c = _normalizar_codec(codec)

    prioridades = {
        'hevc': 5,
        'av1': 4,
        'vp9': 3,
        'h264': 2,
        'mpeg4': 1,
        'mpeg2video': 1,
    }

    return prioridades.get(c, 0)


def _decidir_cual_eliminar(fp_a: VideoFingerprint, fp_b: VideoFingerprint) -> Dict[str, Any]:
    """Resolución, códec, mayor bitrate de vídeo, menor tamaño y empate."""
    pa = fp_a.resolucion[0] * fp_a.resolucion[1]
    pb = fp_b.resolucion[0] * fp_b.resolucion[1]
    ca, cb = _prioridad_codec(fp_a.codec_video), _prioridad_codec(fp_b.codec_video)
    ba, bb = fp_a.bitrate_video_kbps, fp_b.bitrate_video_kbps
    if fp_a.hash_archivo and fp_a.hash_archivo == fp_b.hash_archivo:
        # SHA256 igual => mismos bytes; metadatos ausentes no significan peor calidad.
        conservar, eliminar = fp_a, fp_b
        criterio = 'empate'
        motivo = ('SHA256 idéntico: son los mismos bytes. No existe diferencia de '
                  'calidad; el orden sugerido es convencional y puede invertirse '
                  'según el dispositivo y la disponibilidad real de las copias.')
    elif pa > 0 and pb > 0 and pa != pb:
        conservar, eliminar = (fp_a, fp_b) if pa > pb else (fp_b, fp_a)
        criterio = 'resolucion'
        motivo = (f'Mayor resolución: {conservar.resolucion[0]}x{conservar.resolucion[1]} '
                  f'frente a {eliminar.resolucion[0]}x{eliminar.resolucion[1]}')
    elif ca > 0 and cb > 0 and ca != cb:
        conservar, eliminar = (fp_a, fp_b) if ca > cb else (fp_b, fp_a)
        criterio = 'codec'
        motivo = f'Códec preferido: {conservar.codec_video} frente a {eliminar.codec_video}'
    elif ba > 0 and bb > 0 and math.isfinite(ba) and math.isfinite(bb) and ba != bb:
        conservar, eliminar = (fp_a, fp_b) if ba > bb else (fp_b, fp_a)
        criterio = 'bitrate_video'
        motivo = (f'Mayor bitrate de vídeo: {conservar.bitrate_video_kbps:.0f} kbps '
                  f'frente a {eliminar.bitrate_video_kbps:.0f} kbps')
    elif fp_a.tamanio_bytes > 0 and fp_b.tamanio_bytes > 0 and fp_a.tamanio_bytes != fp_b.tamanio_bytes:
        conservar, eliminar = (fp_a, fp_b) if fp_a.tamanio_bytes < fp_b.tamanio_bytes else (fp_b, fp_a)
        criterio = 'tamanio'
        motivo = (f'Menor tamaño: {conservar.tamanio_bytes / 1024**2:.1f} MB '
                  f'frente a {eliminar.tamanio_bytes / 1024**2:.1f} MB')
    else:
        conservar, eliminar = fp_a, fp_b
        criterio = 'empate'
        motivo = ('No hay una diferencia fiable con los datos disponibles. '
                  'Verifica el archivo antes de escoger uno para eliminar.')
    if (not fp_a.hash_archivo or fp_a.hash_archivo != fp_b.hash_archivo) and not (ba > 0 and bb > 0):
        motivo += ' Bitrate de vídeo no disponible en ambos archivos; criterio omitido.'
    return dict(criterio=criterio, conservar_ruta=conservar.ruta, conservar_nombre=conservar.nombre,
                eliminar_ruta=eliminar.ruta, eliminar_nombre=eliminar.nombre, motivo=motivo,
                conservar_dispositivo=conservar.dispositivo, conservar_dispositivo_id=conservar.dispositivo_id,
                eliminar_dispositivo=eliminar.dispositivo, eliminar_dispositivo_id=eliminar.dispositivo_id)


def _info_video(fp: VideoFingerprint) -> Dict[str, Any]:
    """
    Extrae la información objetiva de un video para mostrarla en el
    reporte y justificar decisiones.
    """
    ancho, alto = fp.resolucion

    bitrate_kbps = 0.0

    if fp.duracion > 0 and fp.tamanio_bytes > 0:
        bitrate_kbps = (
            fp.tamanio_bytes * 8
            / fp.duracion
            / 1000.0
        )

    return {
        'ruta': fp.ruta,
        'nombre': fp.nombre,
        'dispositivo': fp.dispositivo or 'Dispositivo no identificado',
        'dispositivo_id': fp.dispositivo_id or 'N/D',
        'duracion': fp.duracion,
        'resolucion': (ancho, alto),
        'fps': fp.fps,
        'codec_video': fp.codec_video,
        'codec_audio': fp.codec_audio,
        'tamanio_bytes': fp.tamanio_bytes,
        'bitrate_kbps': round(bitrate_kbps, 1),
        'bitrate_video_kbps': fp.bitrate_video_kbps,
        'hash_archivo': fp.hash_archivo,
        'frames_muestreados': len(fp.frame_hashes),
    }


# ==================================================
# Comparación de un par
# ==================================================

def comparar_par(
    fp1: VideoFingerprint,
    fp2: VideoFingerprint,
    umbral_phash: int = UMBRAL_PHASH,
    umbral_visual: float = UMBRAL_VISUAL,
    umbral_fragmento: float = UMBRAL_FRAGMENTO,
    umbral_posible_fragmento: float = UMBRAL_POSIBLE_FRAGMENTO,
    margen_dur: float = MARGEN_DURACION,
    ratio_min: float = RATIO_MIN_FRAGMENTO,
    progreso_par: ProgresoPar = None,
) -> Optional[ResultadoComparacion]:
    """
    Compara dos videos.

    Primero descarta el caso de dos rutas que apuntan al mismo
    archivo físico. Ese caso no es un duplicado entre archivos.
    """
    if (fp1.dispositivo_id and fp2.dispositivo_id
            and fp1.dispositivo_id != fp2.dispositivo_id):
        mismo_fisico = False
    else:
        mismo_fisico = _mismo_archivo_fisico(fp1.ruta, fp2.ruta)
    if mismo_fisico:
        return None

    fp_a, fp_b = (
        (fp1, fp2)
        if fp1.duracion >= fp2.duracion
        else (fp2, fp1)
    )

    info_a = _info_video(fp_a)
    info_b = _info_video(fp_b)

    def resultado(
        tipo: str,
        sv: float,
        sa: float,
        confianza: float,
        notas: str = '',
        inicio: Optional[float] = None,
        fin: Optional[float] = None,
        recomendacion: Optional[Dict[str, Any]] = None,
    ) -> ResultadoComparacion:
        return ResultadoComparacion(
            tipo=tipo,
            ruta_a=fp_a.ruta,
            nombre_a=fp_a.nombre,
            ruta_b=fp_b.ruta,
            nombre_b=fp_b.nombre,
            similitud_visual=round(sv, 4),
            similitud_audio=(
                round(sa, 4)
                if sa >= 0
                else -1.0
            ),
            confianza=round(confianza, 4),
            offset_inicio=inicio,
            offset_fin=fin,
            notas=notas,
            info_a=info_a,
            info_b=info_b,
            recomendacion=recomendacion or {},
        )

    # --------------------------------------------------
    # 1. Duplicado exacto (SHA256)
    # --------------------------------------------------
    if (
        fp_a.hash_archivo
        and fp_a.hash_archivo == fp_b.hash_archivo
    ):
        recomendacion = _decidir_cual_eliminar(fp_a, fp_b)

        return resultado(
            'duplicado_exacto',
            1.0,
            -1.0,
            1.0,
            (
                'Hash SHA256 idéntico; '
                'audio no evaluado'
            ),
            recomendacion=recomendacion,
        )

    if (
        not fp_a.frame_hashes
        or not fp_b.frame_hashes
    ):
        return None

    if (
        fp_a.frame_intervalo <= 0
        or fp_b.frame_intervalo <= 0
        or abs(
            fp_a.frame_intervalo
            - fp_b.frame_intervalo
        ) > 1e-6
    ):
        return None

    dur_a = fp_a.duracion
    dur_b = fp_b.duracion

    if dur_a <= 0 or dur_b <= 0:
        return None

    ratio = dur_b / dur_a

    if ratio < ratio_min:
        return None

    duraciones_similares = (
        abs(dur_a - dur_b)
        <= dur_b * margen_dur
    )

    # --------------------------------------------------
    # 2. Duplicado visual o posible duplicado
    # --------------------------------------------------
    if duraciones_similares:
        sv = _similitud_sec(
            fp_a.frame_hashes,
            fp_b.frame_hashes,
            umbral_phash,
        )

        sa = _similitud_audio(
            fp_a.audio_huella,
            fp_b.audio_huella,
        )

        confianza = _confianza(sv, sa)

        if sv >= umbral_visual:
            notas = f'Similitud visual: {sv:.1%}'

            if sa >= 0:
                notas += f', audio: {sa:.1%}'

            return resultado(
                'duplicado_visual',
                sv,
                sa,
                confianza,
                notas,
                recomendacion=_decidir_cual_eliminar(fp_a, fp_b),
            )

        if sv >= 0.50:
            return resultado(
                'posible_duplicado',
                sv,
                sa,
                confianza * 0.7,
                (
                    f'Similitud parcial ({sv:.1%}); '
                    'verificar manualmente'
                ),
                recomendacion=_decidir_cual_eliminar(fp_a, fp_b),
            )

    # --------------------------------------------------
    # 3. Fragmento o posible fragmento
    # --------------------------------------------------
    if ratio < (1.0 - margen_dur):
        piso = max(
            0.12,
            umbral_posible_fragmento * 0.7,
        )

        hit = _buscar_fragmento(
            fp_a.frame_hashes,
            fp_b.frame_hashes,
            umbral=umbral_phash,
            min_sim_abs=piso,
            progreso=progreso_par,
        )

        if hit is None:
            return None

        pos, sv = hit

        if sv >= umbral_fragmento:
            tipo = 'fragmento'
        elif sv >= umbral_posible_fragmento:
            tipo = 'posible_fragmento'
        else:
            return None

        inicio = round(
            pos * fp_a.frame_intervalo,
            1,
        )

        fin = round(
            min(
                inicio + dur_b,
                dur_a,
            ),
            1,
        )

        sa = -1.0
        confianza = sv

        notas = (
            f'Fragmento del minuto '
            f'{inicio / 60:.1f} al {fin / 60:.1f} '
            f'del video largo. '
            f'Similitud visual: {sv:.1%}. '
            'Audio no evaluado: '
            'requiere alineación temporal.'
        )

        # En fragmentos no recomendamos eliminar automáticamente
        # porque el corto suele ser un recorte del largo y ambos
        # pueden ser útiles. Solo indicamos cuál es el fragmento.
        recomendacion_fragmento = {
            'criterio': 'fragmento',
            'conservar_ruta': fp_a.ruta,
            'conservar_nombre': fp_a.nombre,
            'eliminar_ruta': '',  # no recomendamos borrar el corto
            'eliminar_nombre': '',
            'motivo': (
                f'"{fp_b.nombre}" es un fragmento '
                f'de "{fp_a.nombre}" ubicado entre '
                f'{inicio / 60:.1f} y {fin / 60:.1f} minutos. '
                'No se recomienda eliminar automáticamente; '
                'decide manualmente si el fragmento es redundante.'
            ),
        }

        return resultado(
            tipo,
            sv,
            sa,
            confianza,
            notas,
            inicio,
            fin,
            recomendacion=recomendacion_fragmento,
        )

    return None


# ==================================================
# Comparar todos los pares
# ==================================================

def comparar_todos(
    videos: List[VideoFingerprint],
    callback_progreso=None,
    resumen=None,
    callback_videos=None,
    **kwargs,
) -> List[ResultadoComparacion]:
    """
    Compara todos los pares y comunica también progreso dentro
    de búsquedas largas de fragmentos.
    """
    n = len(videos)

    total = n * (n - 1) // 2

    resultados: List[ResultadoComparacion] = []

    procesados = 0
    conteos = [0] * n
    aliases = incompatibles = sin_huellas = 0

    for i in range(n):
        for j in range(i + 1, n):
            a = videos[i]
            b = videos[j]

            if callback_progreso is not None:
                try:
                    callback_progreso(
                        procesados,
                        total,
                        a.nombre,
                        b.nombre,
                    )
                except Exception:
                    pass

            def avance_par(
                actual: int,
                total_posiciones: int,
            ) -> None:
                if callback_progreso is None:
                    return

                try:
                    callback_progreso(
                        procesados
                        + actual / max(1, total_posiciones),
                        total,
                        a.nombre,
                        b.nombre,
                    )
                except Exception:
                    pass

            exacto = bool(a.hash_archivo and a.hash_archivo == b.hash_archivo)
            if _mismo_archivo_fisico(a.ruta, b.ruta):
                aliases += 1
            elif not exacto and (not a.frame_hashes or not b.frame_hashes):
                sin_huellas += 1
            elif not exacto and (a.frame_intervalo <= 0 or b.frame_intervalo <= 0 or abs(a.frame_intervalo - b.frame_intervalo) > 1e-6):
                incompatibles += 1

            hallazgo = comparar_par(
                a,
                b,
                progreso_par=avance_par,
                **kwargs,
            )

            if hallazgo is not None:
                resultados.append(hallazgo)

            procesados += 1
            conteos[i] += 1
            conteos[j] += 1
            if callback_videos is not None:
                # En el último par terminan los dos últimos vídeos a la vez.
                completos = n if procesados == total else i + (j == n - 1)
                callback_videos(completos, n)

            if callback_progreso is not None:
                try:
                    callback_progreso(
                        procesados,
                        total,
                        a.nombre,
                        b.nombre,
                    )
                except Exception:
                    pass

    resultados.sort(
        key=lambda r: r.confianza,
        reverse=True,
    )

    if resumen is not None:
        resumen.update(total_videos=n, total_pares=total, pares_evaluados=procesados,
                       mismo_archivo_fisico=aliases, intervalos_incompatibles=incompatibles,
                       sin_huellas_visuales=sin_huellas, cobertura_completa=(procesados == total),
                       por_video=[{'ruta': v.ruta, 'nombre': v.nombre, 'pares_evaluados': conteos[k]}
                                  for k, v in enumerate(videos)])
    return resultados