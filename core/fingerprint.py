"""
Extracción de huellas de videos.

Genera SHA256, pHash de frames muestreados y, cuando es posible,
una huella de audio.

Compatible con escaneo concurrente:
- Recibe un evento de cancelación compartido.
- Permite limitar los hilos solicitados a FFmpeg.
- Detecta dinámicamente los aceleradores disponibles en el FFmpeg
  instalado y los usa en cascada: hardware -> software.
- Termina procesos externos limpios al cancelar.
- Puede reutilizar un SHA256 ya registrado si el archivo no cambió.

Pillow e ImageHash son dependencias obligatorias: vidup.py las valida
al arrancar. Este módulo las importa directamente.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from PIL import Image
import imagehash
from core.horario import fmt_ahora_utc6 as _ahora_utc6_str

# ============================================================================
# Rich (opcional) para mensajes internos
# ============================================================================
try:
    from rich.console import Console
    from rich import box as rich_box
    TIENE_RICH = True
except ImportError:
    Console = None      # type: ignore
    rich_box = None     # type: ignore
    TIENE_RICH = False

_CONSOLA = Console(highlight=False, soft_wrap=True) if Console is not None else None


def _imprimir(mensaje: str, estilo: str = '') -> None:
    if _CONSOLA is not None:
        _CONSOLA.print(mensaje, style=estilo or None)
    else:
        print(mensaje)


def _avisar(mensaje: str) -> None:
    """Muestra un aviso en amarillo sin interpretar corchetes como markup."""
    if _CONSOLA is not None:
        _CONSOLA.print(mensaje, style='yellow', markup=False, highlight=False)
    else:
        print(mensaje)


# ============================================================================
# Tipos
# ============================================================================
Progreso = Optional[Callable[..., None]]


@dataclass
class VideoFingerprint:
    ruta: str
    nombre: str
    duracion: float
    resolucion: Tuple[int, int]
    fps: float
    codec_video: str
    codec_audio: str
    hash_archivo: str
    hash_rapido: str
    frame_hashes: List[str]
    frame_intervalo: float
    audio_huella: Optional[str]
    tamanio_bytes: int
    metodo_acel: str = 'software'
    error: Optional[str] = None
    bitrate_video_kbps: float = 0.0
    dispositivo: str = ''
    dispositivo_id: str = ''


class ProcesamientoCancelado(Exception):
    """Se utiliza internamente para abortar un video sin guardar su huella."""
    pass


# ============================================================================
# Cancelación
# ============================================================================
def _esta_cancelado(cancelar: Optional[threading.Event]) -> bool:
    return cancelar is not None and cancelar.is_set()


def _verificar_cancelacion(cancelar: Optional[threading.Event]) -> None:
    if _esta_cancelado(cancelar):
        raise ProcesamientoCancelado('Interrumpido por el usuario')


# ============================================================================
# Notificación de progreso
# ============================================================================
def _notificar(progreso: Progreso, fase: str, fraccion: float, inicio: float, duracion: float = 0.0) -> None:
    """Calcula un ETA aproximado para la fase actual."""
    if progreso is None:
        return
    fraccion = max(0.0, min(1.0, fraccion))
    transcurrido = time.monotonic() - inicio
    if fraccion > 0:
        eta = transcurrido * (1.0 - fraccion) / fraccion
    else:
        eta = -1.0
    try:
        progreso(fase, fraccion, eta, duracion)
    except TypeError:
        progreso(fase, fraccion, eta)


# ============================================================================
# Terminación limpia de subprocesos
# ============================================================================
def _terminar_proceso(proceso: subprocess.Popen) -> None:
    """Intenta terminar un proceso externo sin dejarlo ejecutándose."""
    try:
        if proceso.poll() is not None:
            return
        proceso.terminate()
        try:
            proceso.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        proceso.kill()
        try:
            proceso.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    except (OSError, subprocess.SubprocessError):
        pass


def _ejecutar_con_cancelacion(
    cmd: List[str],
    cancelar: Optional[threading.Event],
    timeout: float,
    capturar_salida: bool = True,
) -> Optional[subprocess.CompletedProcess]:
    """Ejecuta un comando revisando periódicamente el evento de cancelación."""
    stdout = subprocess.PIPE if capturar_salida else subprocess.DEVNULL
    stderr = subprocess.PIPE if capturar_salida else subprocess.DEVNULL

    try:
        proceso = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            text=True,
            errors='replace',
        )
    except OSError:
        return None

    inicio = time.monotonic()
    while True:
        if _esta_cancelado(cancelar):
            _terminar_proceso(proceso)
            return None
        try:
            salida, errores = proceso.communicate(timeout=0.20)
            return subprocess.CompletedProcess(cmd, proceso.returncode, salida, errores)
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() - inicio >= timeout:
            _terminar_proceso(proceso)
            raise subprocess.TimeoutExpired(cmd, timeout)


# ============================================================================
# FFprobe
# ============================================================================
def ffprobe_info(ruta: Path, cancelar: Optional[threading.Event] = None) -> dict:
    """Obtiene metadatos de video mediante ffprobe."""
    _verificar_cancelacion(cancelar)
    cmd = [
        'ffprobe', '-v', 'error', '-print_format', 'json',
        '-show_streams', '-show_format', str(ruta),
    ]
    try:
        resultado = _ejecutar_con_cancelacion(
            cmd, cancelar=cancelar, timeout=30, capturar_salida=True)
        if resultado is None:
            raise ProcesamientoCancelado('Interrumpido por el usuario')
        if resultado.returncode != 0 or not resultado.stdout.strip():
            return {}
        return json.loads(resultado.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ProcesamientoCancelado):
        return {}


def _es_video_valido(ruta: Path, cancelar=None) -> Tuple[bool, str]:
    """Verificación rápida con ffprobe."""
    try:
        resultado = _ejecutar_con_cancelacion(
            [
                'ffprobe', '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=codec_name,duration',
                '-of', 'default=noprint_wrappers=1',
                str(ruta),
            ],
            cancelar=cancelar,
            timeout=15,
            capturar_salida=True,
        )
    except subprocess.TimeoutExpired:
        return False, 'ffprobe agotó el tiempo (archivo posiblemente dañado)'
    if resultado is None:
        return False, 'ffprobe cancelado'
    if resultado.returncode != 0:
        return False, f'ffprobe falló: {resultado.stderr.strip()[:150]}'
    return True, ''


# ============================================================================
# SHA256
# ============================================================================
def calcular_hash_completo(
    ruta: Path,
    chunk: int = 1024 * 1024,
    progreso: Progreso = None,
    cancelar: Optional[threading.Event] = None,
) -> str:
    """Calcula SHA256 completo mostrando avance según bytes leídos."""
    _verificar_cancelacion(cancelar)
    sha = hashlib.sha256()
    tamanio = ruta.stat().st_size
    leidos = 0
    inicio = time.monotonic()
    _notificar(progreso, 'SHA256', 0.0, inicio)

    with ruta.open('rb') as archivo:
        while True:
            _verificar_cancelacion(cancelar)
            datos = archivo.read(chunk)
            if not datos:
                break
            sha.update(datos)
            leidos += len(datos)
            fraccion = leidos / tamanio if tamanio else 1.0
            _notificar(progreso, 'SHA256', fraccion, inicio)

    _verificar_cancelacion(cancelar)
    _notificar(progreso, 'SHA256', 1.0, inicio)
    return sha.hexdigest()


def calcular_hash_rapido(
    ruta: Path,
    muestra: int = 65_536,
    cancelar: Optional[threading.Event] = None,
) -> str:
    """SHA256 del tamaño y de muestras al inicio y final del archivo."""
    _verificar_cancelacion(cancelar)
    tamanio = ruta.stat().st_size
    sha = hashlib.sha256()
    sha.update(str(tamanio).encode('ascii'))
    with ruta.open('rb') as archivo:
        _verificar_cancelacion(cancelar)
        sha.update(archivo.read(muestra))
        if tamanio > muestra * 2:
            _verificar_cancelacion(cancelar)
            archivo.seek(-muestra, os.SEEK_END)
            sha.update(archivo.read(muestra))
    return sha.hexdigest()


# ============================================================================
# Detección de aceleradores
# ============================================================================
@lru_cache(maxsize=1)
def _opciones_fps_ffmpeg() -> List[str]:
    """Selecciona una opción VFR compatible con el FFmpeg instalado."""
    try:
        resultado = subprocess.run(
            ['ffmpeg', '-hide_banner', '-h', 'full'],
            capture_output=True, text=True, errors='replace', timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    ayuda = resultado.stdout + resultado.stderr
    if '-fps_mode' in ayuda:
        return ['-fps_mode', 'vfr']
    if '-vsync' in ayuda:
        return ['-vsync', 'vfr']
    return []


def _opciones_hilos_ffmpeg(hilos_ffmpeg: int) -> List[str]:
    hilos = max(1, int(hilos_ffmpeg))
    return ['-threads', str(hilos), '-filter_threads', str(hilos)]


@lru_cache(maxsize=1)
def _aceleradores_disponibles() -> frozenset:
    """Consulta al FFmpeg instalado qué aceleradores soporta."""
    try:
        resultado = subprocess.run(
            ['ffmpeg', '-hide_banner', '-hwaccels'],
            capture_output=True, text=True, errors='replace', timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()

    disponibles = set()
    salida = resultado.stdout + '\n' + resultado.stderr
    for linea in salida.splitlines():
        linea = linea.strip().lower()
        if not linea:
            continue
        if ':' in linea or 'acceleration' in linea or 'methods' in linea:
            continue
        disponibles.add(linea)
    return frozenset(disponibles)


def _plataforma() -> str:
    if os.name == 'nt':
        return 'windows'
    if sys.platform == 'darwin':
        return 'macos'
    if (
        'ANDROID_ROOT' in os.environ
        or 'ANDROID_DATA' in os.environ
        or 'com.termux' in os.environ.get('PREFIX', '')
        or 'com.termux' in os.environ.get('PATH', '')
    ):
        return 'android'
    return 'linux'


def _orden_plataforma() -> List[str]:
    plataforma = _plataforma()
    if plataforma == 'windows':
        return ['amf', 'd3d11va', 'qsv', 'dxva2', 'cuda']
    if plataforma == 'macos':
        return ['videotoolbox']
    if plataforma == 'android':
        return ['mediacodec']
    return ['vaapi', 'vulkan', 'cuda']


def _normalizar_aceleracion(aceleracion: str) -> str:
    alias = {
        'd3': 'd3d11va',
        'dxva': 'dxva2',
        'va': 'vaapi',
        'vt': 'videotoolbox',
        'mc': 'mediacodec',
        'vk': 'vulkan',
    }
    return alias.get(aceleracion, aceleracion)


def obtener_exclusiones() -> set:
    """Retorna el conjunto de modos de aceleración/encoders excluidos permanentemente por fallar."""
    DIR_SCRIPT = Path(__file__).resolve().parent.parent
    ruta = DIR_SCRIPT / '.vidup_exclusiones.json'
    if not ruta.is_file():
        return set()
    try:
        with ruta.open('r', encoding='utf-8') as f:
            datos = json.load(f)
            return set(datos.get('exclusiones', []))
    except Exception:
        return set()


def registrar_exclusion(modo_acel: str, motivo: str, con_hwdownload: bool = False) -> None:
    """Registra de manera permanente un modo de aceleración/variante defectuoso para omitirlo en futuros escaneos."""
    if not modo_acel or modo_acel in ('software', 'auto'):
        return
    clave_exclusion = f"{modo_acel}-con-hwdownload" if con_hwdownload else f"{modo_acel}-sin-hwdownload"
    DIR_SCRIPT = Path(__file__).resolve().parent.parent
    ruta = DIR_SCRIPT / '.vidup_exclusiones.json'
    datos = {'exclusiones': [], 'detalle': {}}
    if ruta.is_file():
        try:
            with ruta.open('r', encoding='utf-8') as f:
                datos = json.load(f)
        except Exception:
            pass
    exclusiones = set(datos.get('exclusiones', []))
    exclusiones.add(clave_exclusion)
    datos['exclusiones'] = list(exclusiones)
    detalles = datos.get('detalle', {})
    detalles[clave_exclusion] = {'motivo': motivo, 'fecha': _ahora_utc6_str()}
    datos['detalle'] = detalles
    try:
        with ruta.open('w', encoding='utf-8') as f:
            json.dump(datos, f, indent=2, ensure_ascii=False)
        _avisar(f'  [yellow]⚠ Se registró la exclusión automática de hardware "{clave_exclusion}" para omitirlo en futuros escaneos.[/]')
    except Exception:
        pass


def _metodos_cascada(aceleracion: str) -> List[str]:
    """Construye la lista de métodos a intentar según el modo solicitado, excluyendo modos fallidos."""
    aceleracion = _normalizar_aceleracion(aceleracion)
    if aceleracion == 'software':
        return ['software']

    disponibles = _aceleradores_disponibles()

    def disponible(nombre: str) -> bool:
        return nombre in disponibles

    if aceleracion == 'auto':
        candidatos = _orden_plataforma()
    else:
        candidatos = [aceleracion]

    metodos: List[str] = []
    for nombre in candidatos:
        if nombre == 'software':
            continue
        if disponible(nombre):
            metodos.append(nombre)
    metodos.append('software')
    return metodos


def _intentos_cascada(aceleracion: str) -> List[Tuple[str, bool]]:
    """Lista de intentos (método, usar_hwdownload) en el orden a probar.

    Para cada acelerador de hardware se prueba primero sin hwdownload:
    FFmpeg baja los cuadros a memoria por su cuenta, que es lo que
    funcionó con mediacodec y con vaapi. Si ese intento falla, se
    reintenta con hwdownload explícito antes de pasar al siguiente método.
    """
    intentos: List[Tuple[str, bool]] = []
    excluidos = obtener_exclusiones()
    for metodo in _metodos_cascada(aceleracion):
        if metodo == 'software':
            intentos.append(('software', False))
            continue
        # Probar versión sin hwdownload si no está excluida explícitamente
        if f"{metodo}-sin-hwdownload" not in excluidos:
            intentos.append((metodo, False))
        # Probar versión con hwdownload si el acelerador lo soporta y no está excluida
        if _formato_hwdownload(metodo) is not None and f"{metodo}-con-hwdownload" not in excluidos:
            intentos.append((metodo, True))
    return intentos


def _opciones_aceleracion(aceleracion: str) -> List[str]:
    aceleracion = _normalizar_aceleracion(aceleracion)
    if aceleracion == 'software':
        return []
    return ['-hwaccel', aceleracion]


def _formato_hwdownload(metodo: str) -> Optional[str]:
    """Formato correcto para 'hwdownload' según el acelerador."""
    metodo = _normalizar_aceleracion(metodo)
    if metodo == 'software':
        return None
    if metodo == 'mediacodec':
        return 'yuv420p'
    return 'nv12'


def _vf_para_metodo(intervalo: float, metodo: str, con_hwdownload: bool = True) -> str:
    """Construye el filtro -vf correcto según el método de decodificación."""
    formato = _formato_hwdownload(metodo)
    if formato is None or not con_hwdownload:
        return f'fps=1/{intervalo},scale=32:32'
    return f'fps=1/{intervalo},hwdownload,format={formato},scale=32:32'


def _etiqueta_intento(modo: str, con_hwdownload: bool) -> str:
    return f'{modo} + hwdownload' if con_hwdownload else modo


def _avisar_fallback(
    modo: str,
    con_hwdownload: bool,
    motivo: str,
    siguiente: Tuple[str, bool],
) -> None:
    """Muestra en terminal por qué se descartó un intento de FFmpeg."""
    actual = _etiqueta_intento(modo, con_hwdownload)
    sig = _etiqueta_intento(siguiente[0], siguiente[1])
    _avisar(f'  ⚠ {actual} falló: {motivo}')
    _avisar(f'    Probando: {sig}')


# ============================================================================
# Extracción de frames
# ============================================================================
def _extraer_frames_ffmpeg(
    ruta: Path,
    duracion: float,
    intervalo: float,
    patron_salida: str,
    progreso: Progreso,
    cancelar: Optional[threading.Event],
    hilos_ffmpeg: int,
    aceleracion: str,
    codec_video: str = '',
) -> Tuple[bool, str, str]:
    """Ejecuta FFmpeg para extraer frames muestreados.

    La cascada de intentos se construye en _intentos_cascada().
    Si un método no reporta avance real de tiempo en 30 s, se aborta y
    se prueba el siguiente.
    """
    import queue

    intentos = _intentos_cascada(aceleracion)
    ultimo_error = 'Error desconocido al invocar FFmpeg'
    timeout_sin_eta = 30.0
    timeout_congelado = 5 * 60
    timeout_absoluto = 60 * 60

    for indice_metodo, (modo_acel, con_hwdownload) in enumerate(intentos):
        cmd = [
            'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-nostats',
            *_opciones_hilos_ffmpeg(hilos_ffmpeg),
            *_opciones_aceleracion(modo_acel),
            '-progress', 'pipe:1',
            '-i', str(ruta),
            '-vf', _vf_para_metodo(intervalo, modo_acel, con_hwdownload),
            *_opciones_fps_ffmpeg(),
            '-f', 'image2',
            patron_salida,
        ]

        inicio = time.monotonic()
        fase_nombre = f'{codec_video[:6]}>{modo_acel[:8]}' if codec_video else f'Extrac ({modo_acel})'
        _notificar(progreso, fase_nombre, 0.0, inicio)

        def _al_fallar(metodo: str, error_msg: str, idx: int) -> bool:
            siguiente_tuple = intentos[idx + 1] if idx < len(intentos) - 1 else None
            siguiente_modo = siguiente_tuple[0] if siguiente_tuple else None

            # Registrar la exclusión específica distinguiendo con/sin hwdownload
            registrar_exclusion(metodo, error_msg, con_hwdownload=con_hwdownload)

            for f in Path(patron_salida).parent.glob('f_*.png'):
                try:
                    f.unlink()
                except OSError:
                    pass

            if siguiente_tuple:
                _avisar_fallback(metodo, con_hwdownload, error_msg, siguiente_tuple)
                return True
            return False

        try:
            proceso = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors='replace',
                bufsize=1,
            )
        except OSError as exc:
            ultimo_error = f'No se pudo ejecutar FFmpeg ({modo_acel}): {exc}'
            if _al_fallar(modo_acel, ultimo_error, indice_metodo):
                continue
            return False, modo_acel, ultimo_error

        cola_lineas: "queue.Queue[Optional[str]]" = queue.Queue()

        def lector_stdout(proc=proceso, cola=cola_lineas):
            try:
                if proc.stdout is None:
                    cola.put(None)
                    return
                for linea in proc.stdout:
                    cola.put(linea)
            except Exception:
                pass
            finally:
                cola.put(None)

        hilo_lector = threading.Thread(
            target=lector_stdout,
            name=f'ffmpeg-stdout-{modo_acel}',
            daemon=True,
        )
        hilo_lector.start()

        ultimo_avance = time.monotonic()
        primera_linea_vista = False
        stderr_texto = ''
        motivo_aborto = ''
        ultima_fraccion = 0.0

        try:
            while True:
                if _esta_cancelado(cancelar):
                    _terminar_proceso(proceso)
                    raise ProcesamientoCancelado('Interrumpido por el usuario')

                ahora = time.monotonic()
                if (ahora - inicio) > timeout_absoluto:
                    stderr_diag = ''
                    if proceso.stderr is not None:
                        try:
                            stderr_diag = proceso.stderr.read().strip()
                        except (OSError, ValueError):
                            stderr_diag = ''
                    stderr_breve = stderr_diag.splitlines()[-1][:200] \
                        if stderr_diag else 'sin salida de error'
                    motivo_aborto = (
                        f'FFmpeg ({modo_acel}) excedió el tiempo máximo de '
                        f'{timeout_absoluto / 60:.0f} min. '
                        f'Último progreso: {ultima_fraccion:.1%}. '
                        f'FFmpeg stderr: {stderr_breve}'
                    )
                    break

                if not primera_linea_vista and (ahora - inicio) > timeout_sin_eta:
                    stderr_diag = ''
                    if proceso.stderr is not None:
                        try:
                            stderr_diag = proceso.stderr.read().strip()
                        except (OSError, ValueError):
                            stderr_diag = ''
                    stderr_breve = stderr_diag.splitlines()[-1][:200] \
                        if stderr_diag else 'sin salida de error'
                    motivo_aborto = (
                        f'FFmpeg ({modo_acel}) no reportó avance de tiempo '
                        f'en {timeout_sin_eta:.0f} s. '
                        f'FFmpeg stderr: {stderr_breve}'
                    )
                    break

                if primera_linea_vista and (ahora - ultimo_avance) > timeout_congelado:
                    stderr_diag = ''
                    if proceso.stderr is not None:
                        try:
                            stderr_diag = proceso.stderr.read().strip()
                        except (OSError, ValueError):
                            stderr_diag = ''
                    stderr_breve = stderr_diag.splitlines()[-1][:200] \
                        if stderr_diag else 'sin salida de error'
                    motivo_aborto = (
                        f'FFmpeg ({modo_acel}) se congeló por más de '
                        f'{timeout_congelado:.0f} s sin procesar cuadros. '
                        f'Último progreso: {ultima_fraccion:.1%}. '
                        f'FFmpeg stderr: {stderr_breve}'
                    )
                    break

                try:
                    linea = cola_lineas.get(timeout=0.1)
                except queue.Empty:
                    if proceso.poll() is not None and cola_lineas.empty():
                        break
                    continue

                if linea is None:
                    break

                clave, separador, valor = linea.strip().partition('=')
                if separador and clave in ('out_time_us', 'out_time_ms'):
                    try:
                        segundos = int(valor) / 1_000_000
                    except (ValueError, ZeroDivisionError):
                        segundos = 0.0
                    if segundos > 0.0:
                        if not primera_linea_vista:
                            primera_linea_vista = True
                        ultimo_avance = time.monotonic()
                        fraccion = min(0.99, segundos / duracion)
                        ultima_fraccion = fraccion
                        _notificar(progreso, fase_nombre, fraccion, inicio, duracion)

                if proceso.poll() is not None and cola_lineas.empty():
                    break

            try:
                hilo_lector.join(timeout=1.0)
            except Exception:
                pass

            if proceso.stderr is not None:
                try:
                    stderr_texto = proceso.stderr.read()
                except (OSError, ValueError):
                    stderr_texto = ''

            if motivo_aborto:
                if proceso.poll() is None:
                    _terminar_proceso(proceso)
                ultimo_error = motivo_aborto
                if _al_fallar(modo_acel, ultimo_error, indice_metodo):
                    continue
                return False, modo_acel, ultimo_error

            if proceso.returncode != 0 and not primera_linea_vista:
                detalle = stderr_texto.strip().splitlines()
                if detalle:
                    ultimo_error = f'[{modo_acel}] {detalle[-1][:200]}'
                else:
                    ultimo_error = (
                        f'FFmpeg terminó con código {proceso.returncode} '
                        f'en modo {modo_acel}'
                    )
                if _al_fallar(modo_acel, ultimo_error, indice_metodo):
                    continue
                return False, modo_acel, ultimo_error

            # Validación física: detonar fallback si el hardware escupió basura
            archivos_png = list(Path(patron_salida).parent.glob('f_*.png'))
            if not archivos_png or all(f.stat().st_size == 0 for f in archivos_png):
                ultimo_error = f'[{modo_acel}] El decodificador generó fotogramas vacíos o corruptos.'
                if _al_fallar(modo_acel, ultimo_error, indice_metodo):
                    continue
                return False, modo_acel, ultimo_error

            _notificar(progreso, fase_nombre, 1.0, inicio)
            return True, modo_acel, ''

        finally:
            if proceso.poll() is None:
                _terminar_proceso(proceso)

    return False, 'ninguno', ultimo_error


# ============================================================================
# Extracción de pHash
# ============================================================================
def extraer_frame_hashes(
    ruta: Path,
    duracion: float,
    intervalo: float = 10.0,
    progreso: Progreso = None,
    cancelar: Optional[threading.Event] = None,
    hilos_ffmpeg: int = 2,
    aceleracion: str = 'auto',
    codec_video: str = '',
) -> Tuple[List[str], str, str]:
    """Extrae frames periódicamente con FFmpeg y calcula su pHash.

    Retorna: (hashes, metodo_usado, error_detalle)
    Si un cuadro no se puede calcular, error_detalle guarda la primera
    excepción para que el motivo aparezca en la tabla de errores.
    """
    if duracion <= 0 or intervalo <= 0 or not math.isfinite(intervalo):
        return [], 'software', 'Duración o intervalo no válidos'

    _verificar_cancelacion(cancelar)
    hashes: List[str] = []
    primer_error_hash = ''

    with tempfile.TemporaryDirectory(prefix='vidup_frames_') as tmpdir:
        patron = os.path.join(tmpdir, 'f_%06d.png')

        terminado, metodo_usado, err_detalle = _extraer_frames_ffmpeg(
            ruta=ruta,
            duracion=duracion,
            intervalo=intervalo,
            patron_salida=patron,
            progreso=progreso,
            cancelar=cancelar,
            hilos_ffmpeg=hilos_ffmpeg,
            aceleracion=aceleracion,
            codec_video=codec_video,
        )

        _verificar_cancelacion(cancelar)
        if not terminado:
            return [], metodo_usado, err_detalle

        archivos = sorted(Path(tmpdir).glob('f_*.png'))
        if not archivos:
            return [], metodo_usado, 'No se generaron fotogramas en disco'

        total = len(archivos)
        inicio_hashes = time.monotonic()
        _notificar(progreso, 'pHash', 0.0, inicio_hashes)

        for indice, archivo in enumerate(archivos, start=1):
            _verificar_cancelacion(cancelar)
            try:
                with Image.open(archivo) as imagen:
                    valor = imagehash.phash(imagen, hash_size=8)
                hashes.append(str(valor))
            except Exception as exc:
                hashes.append('!')
                if not primer_error_hash:
                    primer_error_hash = f'{type(exc).__name__}: {exc}'
            _notificar(progreso, 'pHash', indice / total, inicio_hashes)

        _verificar_cancelacion(cancelar)
        _notificar(progreso, 'pHash', 1.0, inicio_hashes)

    return hashes, metodo_usado, primer_error_hash


# ============================================================================
# Huella de audio
# ============================================================================
def extraer_audio_huella(
    ruta: Path,
    duracion: float,
    cancelar: Optional[threading.Event] = None,
    hilos_ffmpeg: int = 2,
) -> Optional[str]:
    """Intenta Chromaprint mediante fpcalc; si no existe, usa energía RMS."""
    _verificar_cancelacion(cancelar)

    try:
        limite = str(min(120, max(30, int(duracion))))
        resultado = _ejecutar_con_cancelacion(
            ['fpcalc', '-raw', '-length', limite, str(ruta)],
            cancelar=cancelar,
            timeout=60,
            capturar_salida=True,
        )
        if resultado is None and _esta_cancelado(cancelar):
            raise ProcesamientoCancelado('Interrumpido por el usuario')
        # fpcalc es opcional: si no está instalado o no puede ejecutarse,
        # continuar al método alternativo de audio sin cancelar el vídeo.
        if resultado is not None and resultado.returncode == 0:
            for linea in resultado.stdout.splitlines():
                if not linea.startswith('FINGERPRINT='):
                    continue
                datos = linea.split('=', 1)[1]
                elementos = datos.split(',')[:60]
                if elementos and elementos[0]:
                    return 'chroma:' + ','.join(elementos)
    except ProcesamientoCancelado:
        raise
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass

    _verificar_cancelacion(cancelar)

    # Fallback: energía RMS con numpy
    try:
        import numpy as np
    except ImportError:
        return None

    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix='vidup_audio_', suffix='.raw', delete=False
        ) as archivo:
            tmp_path = archivo.name

        timeout_audio = max(120, int(min(max(duracion, 0), 600) * 2))
        cmd = [
            'ffmpeg', '-y', '-nostdin', '-hide_banner', '-loglevel', 'error',
            *_opciones_hilos_ffmpeg(hilos_ffmpeg),
            '-i', str(ruta), '-t', '600', '-vn', '-ac', '1', '-ar', '8000',
            '-f', 's16le', tmp_path,
        ]

        resultado = _ejecutar_con_cancelacion(
            cmd, cancelar=cancelar, timeout=timeout_audio, capturar_salida=True)
        if resultado is None:
            if _esta_cancelado(cancelar):
                raise ProcesamientoCancelado('Interrumpido por el usuario')
            return None
        if resultado.returncode != 0:
            return None

        _verificar_cancelacion(cancelar)

        with open(tmp_path, 'rb') as archivo:
            datos = archivo.read(80_000 * 120)
        if not datos:
            return None

        bloque_sz = 80_000
        energia: List[int] = []
        for inicio in range(0, len(datos), bloque_sz):
            _verificar_cancelacion(cancelar)
            bloque = datos[inicio:inicio + bloque_sz]
            n = (len(bloque) // 2) * 2
            if n < 2:
                break
            muestras = np.frombuffer(bloque[:n], dtype='<i2').astype(np.float32)
            rms = int(np.sqrt(np.mean(muestras * muestras)))
            energia.append(min(255, rms // 128))

        if not energia:
            return None
        return 'rms:' + bytes(energia).hex()

    except ProcesamientoCancelado:
        raise
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ============================================================================
# Reutilización de SHA256 registrado
# ============================================================================
def _reutilizar_sha256_previo(
    hash_previo: Optional[dict],
    tamanio: int,
    hash_rapido: str,
) -> Optional[str]:
    """Devuelve el SHA256 guardado si el archivo no cambió; None si hay que recalcularlo.

    Se compara el tamaño y, cuando la base de datos tiene el hash rápido
    real, también ese valor. Si el registro viene de un SHA256 solo
    (hash_rapido = 'sha256_solo'), únicamente se compara el tamaño.
    """
    if not hash_previo:
        return None
    hash_guardado = str(hash_previo.get('hash_archivo') or '').strip().lower()
    if not hash_guardado:
        return None
    if hash_previo.get('tamanio_bytes') != tamanio:
        return None
    rapido_guardado = str(hash_previo.get('hash_rapido') or '').strip()
    if rapido_guardado and rapido_guardado != 'sha256_solo' and rapido_guardado != hash_rapido:
        return None
    return hash_guardado


# ============================================================================
# Orquestación
# ============================================================================
def generar_fingerprint(
    ruta_str: str,
    intervalo: float = 5.0,
    progreso: Progreso = None,
    cancelar: Optional[threading.Event] = None,
    hilos_ffmpeg: int = 2,
    aceleracion: str = 'auto',
    codec_video: str = '',
    hash_previo: Optional[dict] = None,
) -> VideoFingerprint:
    """Genera la huella del video.

    hash_previo (opcional): datos ya guardados en la base de datos para
    este archivo, con las claves 'hash_archivo', 'hash_rapido' y
    'tamanio_bytes'. Si el archivo no cambió, se reutiliza su SHA256 y
    no se vuelve a leer completo.
    """
    from .database import info_dispositivo_actual
    info_disp = info_dispositivo_actual()
    ruta = Path(ruta_str)

    def error(mensaje: str, acel: str = 'software') -> VideoFingerprint:
        return VideoFingerprint(
            ruta=str(ruta), nombre=ruta.name, duracion=0.0,
            resolucion=(0, 0), fps=0.0, codec_video='', codec_audio='',
            hash_archivo='', hash_rapido='', frame_hashes=[],
            frame_intervalo=intervalo, audio_huella=None, tamanio_bytes=0,
            metodo_acel=acel, error=mensaje,
            dispositivo=info_disp['dispositivo'],
            dispositivo_id=info_disp['dispositivo_id'],
        )

    if intervalo <= 0 or not math.isfinite(intervalo):
        return error('El intervalo debe ser un número positivo y finito')

    if hilos_ffmpeg <= 0:
        return error('hilos_ffmpeg debe ser un entero mayor a cero')

    aceleracion = _normalizar_aceleracion(aceleracion)
    aceleradores_conocidos = {
        'software', 'auto', 'amf', 'd3d11va', 'dxva2', 'qsv', 'cuda',
        'vulkan', 'vaapi', 'videotoolbox', 'mediacodec', 'opencl', 'd3d12va',
    }
    if aceleracion not in aceleradores_conocidos:
        return error(f'Modo de aceleración no reconocido: {aceleracion}')

    try:
        _verificar_cancelacion(cancelar)

        if not ruta.exists():
            return error('Archivo no encontrado')
        if not ruta.is_file():
            return error('La ruta no es un archivo')

        valido, razon = _es_video_valido(ruta, cancelar=cancelar)
        if not valido:
            return error(f'Archivo no válido: {razon}')

        info = ffprobe_info(ruta, cancelar=cancelar)
        _verificar_cancelacion(cancelar)
        if not info:
            return error('No se pudieron leer metadatos con ffprobe')

        resolucion = (0, 0)
        fps = 0.0
        codec_video = ''
        codec_audio = ''
        bitrate_video_kbps = 0.0

        for stream in info.get('streams', []):
            _verificar_cancelacion(cancelar)
            tipo = stream.get('codec_type', '')

            if tipo == 'video' and not codec_video:
                codec_video = stream.get('codec_name', '')
                try:
                    tags = stream.get('tags', {}) or {}
                    bitrate_raw = stream.get('bit_rate')
                    if not bitrate_raw or bitrate_raw == 'N/A':
                        bitrate_raw = tags.get('BPS') or tags.get('BPS-eng') or 0
                    bitrate_video_kbps = float(bitrate_raw) / 1000
                    if not math.isfinite(bitrate_video_kbps) or bitrate_video_kbps < 0:
                        bitrate_video_kbps = 0.0
                except (ValueError, TypeError):
                    bitrate_video_kbps = 0.0

                resolucion = (
                    int(stream.get('width', 0) or 0),
                    int(stream.get('height', 0) or 0),
                )

                fps_raw = stream.get('r_frame_rate', '0/1')
                try:
                    num, den = fps_raw.split('/')
                    denominador = float(den)
                    fps = round(float(num) / denominador, 3) if denominador else 0.0
                except (AttributeError, ValueError, ZeroDivisionError):
                    fps = 0.0

            elif tipo == 'audio' and not codec_audio:
                codec_audio = stream.get('codec_name', '')

        if not codec_video:
            return error('No se encontró una pista de video válida')

        try:
            duracion = float(info.get('format', {}).get('duration', 0))
        except (ValueError, TypeError):
            duracion = 0.0

        if duracion <= 0 or not math.isfinite(duracion):
            return error('Duración de video no válida')

        _verificar_cancelacion(cancelar)
        tamanio = ruta.stat().st_size

        hash_rapido = calcular_hash_rapido(ruta, cancelar=cancelar)

        hash_archivo = _reutilizar_sha256_previo(hash_previo, tamanio, hash_rapido)
        if hash_archivo is None:
            hash_archivo = calcular_hash_completo(
                ruta, progreso=progreso, cancelar=cancelar)
        else:
            _notificar(progreso, 'SHA256', 1.0, time.monotonic())

        _verificar_cancelacion(cancelar)

        frame_hashes, metodo_usado, err_extraccion = extraer_frame_hashes(
            ruta=ruta,
            duracion=duracion,
            intervalo=intervalo,
            progreso=progreso,
            cancelar=cancelar,
            hilos_ffmpeg=hilos_ffmpeg,
            aceleracion=aceleracion,
            codec_video=codec_video,
        )

        _verificar_cancelacion(cancelar)

        if not frame_hashes:
            return error(
                f'Error en extracción de cuadros: {err_extraccion}',
                acel=metodo_usado,
            )

        if not any(valor != '!' for valor in frame_hashes):
            detalle = f': {err_extraccion}' if err_extraccion else ''
            return error(
                f'No se pudieron calcular hashes visuales{detalle}',
                acel=metodo_usado,
            )

        inicio_audio = time.monotonic()
        _notificar(progreso, 'Audio', 0.0, inicio_audio)

        audio_huella = extraer_audio_huella(
            ruta=ruta,
            duracion=duracion,
            cancelar=cancelar,
            hilos_ffmpeg=hilos_ffmpeg,
        )

        _verificar_cancelacion(cancelar)
        _notificar(progreso, 'Audio', 1.0, inicio_audio)

        return VideoFingerprint(
            ruta=str(ruta.resolve()),
            nombre=ruta.name,
            duracion=duracion,
            resolucion=resolucion,
            fps=fps,
            codec_video=codec_video,
            codec_audio=codec_audio,
            hash_archivo=hash_archivo,
            hash_rapido=hash_rapido,
            frame_hashes=frame_hashes,
            frame_intervalo=intervalo,
            audio_huella=audio_huella,
            tamanio_bytes=tamanio,
            metodo_acel=metodo_usado,
            bitrate_video_kbps=bitrate_video_kbps,
            dispositivo=info_disp['dispositivo'],
            dispositivo_id=info_disp['dispositivo_id'],
        )

    except ProcesamientoCancelado:
        return error('Interrumpido por el usuario')
    except PermissionError:
        return error('Sin permisos para leer el archivo')
    except FileNotFoundError:
        return error('Archivo no encontrado')
    except OSError as exc:
        return error(f'Error de sistema: {exc}')
    except Exception as exc:
        return error(f'Error no esperado: {exc}')


# ============================================================================
# Logica de escaneo (extraida de vidup.py): trabajos por video y cola acotada
# ============================================================================
from concurrent.futures import wait, FIRST_COMPLETED
from core.plataforma import es_android


def _trabajo_fingerprint(ruta: Path, intervalo: float, dashboard: Any,
                         evento_cancelacion: threading.Event, hilos_ffmpeg: int,
                         aceleracion: str, medidor_recursos=None,
                         hash_esperado: Optional[str] = None,
                         tamanio_esperado: Optional[int] = None,
                         hash_previo: Optional[dict] = None) -> Tuple[Path, dict]:
    from core.reporter import Dashboard, ProgresoFingerprintWorker, _fmt_seg

    tid = threading.get_ident()
    ruta_str = str(ruta.resolve())
    nombre = ruta.name
    inicio = time.monotonic()
    medicion = None

    if evento_cancelacion.is_set():
        return ruta, {'estado': 'cancelado', 'tiempo': 0.0, 'medicion': None}

    if medidor_recursos is not None:
        try:
            medicion = medidor_recursos.iniciar_medicion(ruta_str)
        except Exception:
            medicion = None

    barra = ProgresoFingerprintWorker(dashboard, tid, nombre)
    dashboard.actualizar_worker(tid, f'{nombre[:35]} preparando...')

    try:
        fp = generar_fingerprint(
            ruta_str, intervalo=intervalo, progreso=barra,
            cancelar=evento_cancelacion, hilos_ffmpeg=hilos_ffmpeg,
            aceleracion=aceleracion, hash_previo=hash_previo,
        )
    except KeyboardInterrupt:
        evento_cancelacion.set()
        if medicion is not None and medidor_recursos is not None:
            medicion.error = 'Interrumpido por el usuario'
            medidor_recursos.finalizar_medicion(medicion, completado=False)
        return ruta, {'estado': 'cancelado', 'tiempo': time.monotonic() - inicio, 'medicion': medicion}
    except Exception as exc:
        if medicion is not None and medidor_recursos is not None:
            medicion.error = f'Error no esperado: {exc}'
            medidor_recursos.finalizar_medicion(medicion, completado=False)
        dashboard.actualizar_worker(tid, f'{nombre[:35]} ERROR: {exc}')
        return ruta, {'estado': 'error', 'error': str(exc), 'tiempo': time.monotonic() - inicio, 'medicion': medicion}

    if evento_cancelacion.is_set():
        if medicion is not None and medidor_recursos is not None:
            medicion.error = 'Interrumpido por el usuario'
            medidor_recursos.finalizar_medicion(medicion, completado=False)
        return ruta, {'estado': 'cancelado', 'tiempo': time.monotonic() - inicio, 'medicion': medicion}

    if not fp.error and hash_esperado:
        if fp.hash_archivo != hash_esperado or (tamanio_esperado is not None and fp.tamanio_bytes != tamanio_esperado):
            fp.error = 'El vídeo cambió desde el reporte SHA256; vuelve a comparar antes de escanearlo.'

    if fp.error:
        if medicion is not None and medidor_recursos is not None:
            medicion.rellenar_info_video(fp)
            medicion.error = fp.error
            medidor_recursos.finalizar_medicion(medicion, completado=False)
        dashboard.actualizar_worker(tid, f'{nombre[:35]} ERROR: {fp.error}')
        return ruta, {'estado': 'error', 'error': fp.error, 'tiempo': time.monotonic() - inicio, 'medicion': medicion}

    tiempo = time.monotonic() - inicio
    if medicion is not None and medidor_recursos is not None:
        medicion.rellenar_info_video(fp)
        medidor_recursos.finalizar_medicion(medicion, completado=True)

    acel_lbl = f'[{fp.metodo_acel}]'
    dashboard.actualizar_worker(tid, f'{nombre[:35]} OK {acel_lbl}: {len(fp.frame_hashes)} cuadros; {_fmt_seg(tiempo)}')

    return ruta, {
        'estado': 'ok', 'fingerprint': fp,
        'frames': len(fp.frame_hashes), 'tiempo': tiempo, 'medicion': medicion,
    }


def _ejecutar_escaneo_acotado(executor, videos, trabajadores, evento, preparar, activos):
    """Mantiene ~2 trabajos por trabajador; no encola miles de vídeos de golpe."""
    iterador = iter(videos)
    tope = max(1, trabajadores * 2)

    def rellenar():
        while len(activos) < tope and not evento.is_set():
            try:
                ruta = next(iterador)
            except StopIteration:
                break
            futuros = executor.submit(*preparar(ruta))
            activos[futuros] = ruta

    rellenar()
    while activos and not evento.is_set():
        hechos, _ = wait(tuple(activos), timeout=0.25, return_when=FIRST_COMPLETED)
        for futuro in hechos:
            ruta = activos.pop(futuro, None)
            if ruta is None:
                continue
            yield futuro, ruta
            rellenar()
            if evento.is_set():
                break


def _cpu_threads_recomendados() -> int:
    if es_android():
        return 1
    try:
        count = os.cpu_count() or 4
        return max(1, min(4, int(count * 0.75)))
    except Exception:
        return 1