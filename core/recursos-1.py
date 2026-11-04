"""
Medición de recursos por video para VIDUP.

Mide CPU, RAM y operaciones de lectura/escritura de disco durante
la generación de huellas de cada video. Incluye el proceso principal
de Python y procesos hijos activos, como FFmpeg, FFprobe y fpcalc.

La medición y los reportes se adaptan al sistema operativo y tipo de
dispositivo donde se ejecuta (Windows, Linux, macOS, Android/Termux).

Rutas de salida (gestionadas por vidup.py):
  reportes/recursos/txt/

psutil es dependencia obligatoria: si no está instalado, vidup.py lo
detecta al arrancar y no permite continuar. Aquí se importa directo.
"""

from __future__ import annotations

import os
import platform
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
ZONA_MEXICO = timezone(timedelta(hours=-6))
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psutil

# ============================================================================
# Rich (opcional)
# ============================================================================
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.rule import Rule
    from rich import box as rich_box
    TIENE_RICH = True
except ImportError:
    Console = None      # type: ignore
    Panel = None        # type: ignore
    Table = None        # type: ignore
    Rule = None         # type: ignore
    rich_box = None     # type: ignore
    TIENE_RICH = False

CONSOLA = Console(highlight=False, soft_wrap=True) if TIENE_RICH else None

# ============================================================================
# Colores ANSI (fallback)
# ============================================================================
NC = '\033[0m'
NEGRITA = '\033[1m'
AZUL = '\033[94m'
AMARILLO = '\033[93m'
VERDE = '\033[92m'
ROJO = '\033[91m'
GRIS = '\033[90m'
CIAN = '\033[96m'


def _imprimir(mensaje: str = '', estilo: str = '') -> None:
    if CONSOLA is not None:
        CONSOLA.print(mensaje, style=estilo or None)
    else:
        prefijo = ''
        sufijo = ''
        mapa = {
            'red': (ROJO, NC), 'yellow': (AMARILLO, NC), 'green': (VERDE, NC),
            'cyan': (CIAN, NC), 'grey': (GRIS, NC), 'bold': (NEGRITA, NC),
        }
        if estilo in mapa:
            prefijo, sufijo = mapa[estilo]
        print(f'{prefijo}{mensaje}{sufijo}')


_MB = 1024 * 1024


# ============================================================================
# Detección de plataforma
# ============================================================================
def _es_android() -> bool:
    return (
        'ANDROID_ROOT' in os.environ
        or 'ANDROID_DATA' in os.environ
        or 'com.termux' in os.environ.get('PREFIX', '')
        or 'com.termux' in os.environ.get('PATH', '')
    )


def _plataforma() -> str:
    if os.name == 'nt':
        return 'windows'
    if platform.system() == 'Darwin':
        return 'macos'
    if _es_android():
        return 'android'
    return 'linux'


def _info_dispositivo() -> Dict[str, str]:
    """Información del hardware concreto donde se ejecuta VIDUP."""
    info: Dict[str, str] = {}
    plat = _plataforma()
    info['plataforma'] = plat

    try:
        info['hostname'] = platform.node()
    except Exception:
        info['hostname'] = 'desconocido'

    try:
        info['sistema'] = platform.system()
    except Exception:
        info['sistema'] = 'desconocido'

    try:
        info['release'] = platform.release()
    except Exception:
        info['release'] = ''

    try:
        info['arquitectura'] = platform.machine()
    except Exception:
        info['arquitectura'] = ''

    try:
        info['nucleos'] = str(os.cpu_count() or '?')
    except Exception:
        info['nucleos'] = '?'

    # CPU modelo
    try:
        if plat in ('linux', 'android'):
            with open('/proc/cpuinfo', 'r', errors='replace') as f:
                for linea in f:
                    if linea.lower().startswith('model name'):
                        info['cpu_modelo'] = linea.split(':', 1)[1].strip()
                        break
            if 'cpu_modelo' not in info:
                with open('/proc/cpuinfo', 'r', errors='replace') as f:
                    for linea in f:
                        if linea.lower().startswith('hardware'):
                            info['cpu_modelo'] = linea.split(':', 1)[1].strip()
                            break
        elif plat == 'windows':
            info['cpu_modelo'] = 'CPU Windows'
            try:
                import subprocess
                r = subprocess.run(['wmic', 'cpu', 'get', 'Name'],
                                   capture_output=True, text=True, timeout=5)
                for l in r.stdout.strip().splitlines():
                    l = l.strip()
                    if l and l.lower() != 'name':
                        info['cpu_modelo'] = l
                        break
            except Exception:
                pass
        elif plat == 'macos':
            info['cpu_modelo'] = 'Apple Silicon / Intel'
            try:
                import subprocess
                r = subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'],
                                   capture_output=True, text=True, timeout=5)
                if r.stdout.strip():
                    info['cpu_modelo'] = r.stdout.strip()
            except Exception:
                pass
    except Exception:
        pass
    info.setdefault('cpu_modelo', 'desconocido')

    # RAM total
    try:
        vm = psutil.virtual_memory()
        info['ram_total_mb'] = str(int(vm.total / _MB))
    except Exception:
        info['ram_total_mb'] = '?'

    # Android: marca y modelo
    if plat == 'android':
        marca = ''
        modelo = ''
        try:
            with open('/system/build.prop', 'r', errors='replace') as f:
                for linea in f:
                    if linea.startswith('ro.product.brand='):
                        marca = linea.split('=', 1)[1].strip()
                    elif linea.startswith('ro.product.model='):
                        modelo = linea.split('=', 1)[1].strip()
        except OSError:
            pass
        info['marca'] = marca or 'desconocida'
        info['modelo'] = modelo or 'desconocido'

    return info


# ============================================================================
# Estructuras
# ============================================================================
@dataclass
class MuestraRecursos:
    """Una muestra agregada de los recursos usados por VIDUP y sus hijos."""
    marca_tiempo: float
    cpu_porcentaje: float
    ram_mb: float
    procesos_hijos: int
    lectura_mb: float
    escritura_mb: float


@dataclass
class MedicionVideo:
    """Medición asociada a un video."""
    ruta: str
    nombre: str
    inicio_epoch: float
    inicio_texto: str
    fin_epoch: Optional[float] = None
    fin_texto: Optional[str] = None
    duracion_seg: float = 0.0
    muestras: List[MuestraRecursos] = field(default_factory=list)
    error: Optional[str] = None
    completado: bool = False

    # Información del video (se rellena al terminar el análisis)
    tamanio_bytes: int = 0
    duracion_video: float = 0.0
    resolucion: Tuple[int, int] = (0, 0)
    fps: float = 0.0
    codec_video: str = ''
    codec_audio: str = ''
    frame_intervalo: float = 0.0
    frames_muestreados: int = 0
    metodo_acel: str = ''

    _detener: threading.Event = field(default_factory=threading.Event, repr=False)
    _hilo: Optional[threading.Thread] = field(default=None, repr=False)

    def rellenar_info_video(self, fp) -> None:
        """Copia la información del VideoFingerprint a la medición."""
        self.tamanio_bytes = getattr(fp, 'tamanio_bytes', 0) or 0
        self.duracion_video = getattr(fp, 'duracion', 0.0) or 0.0
        self.resolucion = getattr(fp, 'resolucion', (0, 0)) or (0, 0)
        self.fps = getattr(fp, 'fps', 0.0) or 0.0
        self.codec_video = getattr(fp, 'codec_video', '') or ''
        self.codec_audio = getattr(fp, 'codec_audio', '') or ''
        self.frame_intervalo = getattr(fp, 'frame_intervalo', 0.0) or 0.0
        self.frames_muestreados = len(getattr(fp, 'frame_hashes', []) or [])
        self.metodo_acel = getattr(fp, 'metodo_acel', '') or ''


# ============================================================================
# Medidor de recursos
# ============================================================================
class MedidorRecursos:
    """Mide los recursos usados por el proceso actual y sus hijos.

    Cada instancia puede medir varios videos. Cada video obtiene un hilo
    propio de muestreo; por ello es apto para el escaneo concurrente.
    """

    def __init__(self, intervalo_muestreo: float = 2.0):
        if intervalo_muestreo <= 0:
            raise ValueError('El intervalo de muestreo debe ser mayor a cero.')
        self.intervalo_muestreo = float(intervalo_muestreo)
        self._proceso_principal = psutil.Process(os.getpid())
        self._lock = threading.Lock()
        self._info_disp = _info_dispositivo()
        self._reiniciar_referencias_cpu()

    def _obtener_procesos(self) -> List['psutil.Process']:
        procesos: List['psutil.Process'] = [self._proceso_principal]
        try:
            hijos = self._proceso_principal.children(recursive=True)
        except (psutil.Error, OSError):
            hijos = []
        for hijo in hijos:
            try:
                if hijo.is_running():
                    procesos.append(hijo)
            except (psutil.Error, OSError):
                continue
        return procesos

    def _reiniciar_referencias_cpu(self) -> None:
        for proceso in self._obtener_procesos():
            try:
                proceso.cpu_percent(interval=None)
            except (psutil.Error, OSError):
                continue

    def _tomar_muestra(self) -> MuestraRecursos:
        procesos = self._obtener_procesos()
        cpu_total = 0.0
        ram_total = 0
        lectura_total = 0
        escritura_total = 0
        hijos_activos = 0

        for proceso in procesos:
            try:
                if proceso.pid != self._proceso_principal.pid:
                    hijos_activos += 1
                cpu_total += proceso.cpu_percent(interval=None)
                memoria = proceso.memory_info()
                ram_total += int(memoria.rss)
                try:
                    io = proceso.io_counters()
                    lectura_total += int(io.read_bytes)
                    escritura_total += int(io.write_bytes)
                except (psutil.Error, AttributeError, OSError):
                    pass
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue

        return MuestraRecursos(
            marca_tiempo=time.time(),
            cpu_porcentaje=cpu_total,
            ram_mb=ram_total / _MB,
            procesos_hijos=hijos_activos,
            lectura_mb=lectura_total / _MB,
            escritura_mb=escritura_total / _MB,
        )

    def _bucle_muestreo(self, medicion: MedicionVideo) -> None:
        self._reiniciar_referencias_cpu()
        while not medicion._detener.is_set():
            muestra = self._tomar_muestra()
            with self._lock:
                medicion.muestras.append(muestra)
            medicion._detener.wait(self.intervalo_muestreo)
        muestra_final = self._tomar_muestra()
        with self._lock:
            medicion.muestras.append(muestra_final)

    def iniciar_medicion(self, ruta: str) -> MedicionVideo:
        ahora = time.time()
        medicion = MedicionVideo(
            ruta=ruta,
            nombre=Path(ruta).name,
            inicio_epoch=ahora,
            inicio_texto=datetime.now(ZONA_MEXICO).strftime('%Y-%m-%d %H:%M:%S'),
        )
        hilo = threading.Thread(
            target=self._bucle_muestreo,
            args=(medicion,),
            name=f'MedidorRecursos-{medicion.nombre[:100]}',
            daemon=True,
        )
        medicion._hilo = hilo
        hilo.start()
        return medicion

    def tomar_muestra_para(self, medicion: MedicionVideo) -> None:
        muestra = self._tomar_muestra()
        with self._lock:
            medicion.muestras.append(muestra)

    def finalizar_medicion(self, medicion: MedicionVideo, completado: bool = True) -> None:
        if medicion.fin_epoch is not None:
            return
        medicion._detener.set()
        hilo = medicion._hilo
        if hilo is not None and hilo.is_alive():
            hilo.join(timeout=max(2.0, self.intervalo_muestreo + 1.0))
        fin = time.time()
        medicion.fin_epoch = fin
        medicion.fin_texto = datetime.now(ZONA_MEXICO).strftime('%Y-%m-%d %H:%M:%S')
        medicion.duracion_seg = max(0.0, fin - medicion.inicio_epoch)
        medicion.completado = bool(completado)


# ============================================================================
# Resumen numérico
# ============================================================================
def _delta_contadores(muestras: List[MuestraRecursos]) -> Tuple[float, float]:
    if len(muestras) < 2:
        return 0.0, 0.0
    lectura = 0.0
    escritura = 0.0
    anterior = muestras[0]
    for actual in muestras[1:]:
        d_lect = actual.lectura_mb - anterior.lectura_mb
        d_esc = actual.escritura_mb - anterior.escritura_mb
        if d_lect > 0:
            lectura += d_lect
        if d_esc > 0:
            escritura += d_esc
        anterior = actual
    return lectura, escritura


def resumen_medicion(medicion: MedicionVideo) -> Dict[str, float]:
    muestras = medicion.muestras
    if not muestras:
        return {
            'muestras': 0.0, 'cpu_max': 0.0, 'cpu_prom': 0.0,
            'ram_max': 0.0, 'ram_prom': 0.0, 'hijos_max': 0.0,
            'lectura_total': 0.0, 'escritura_total': 0.0,
        }
    cpu = [m.cpu_porcentaje for m in muestras]
    ram = [m.ram_mb for m in muestras]
    hijos = [m.procesos_hijos for m in muestras]
    lectura, escritura = _delta_contadores(muestras)
    return {
        'muestras': float(len(muestras)),
        'cpu_max': max(cpu),
        'cpu_prom': sum(cpu) / len(cpu),
        'ram_max': max(ram),
        'ram_prom': sum(ram) / len(ram),
        'hijos_max': float(max(hijos)),
        'lectura_total': lectura,
        'escritura_total': escritura,
    }


# ============================================================================
# Formato
# ============================================================================
def _formato_bytes(cantidad: int) -> str:
    if cantidad <= 0:
        return '0 B'
    if cantidad < 1024:
        return f'{cantidad} B'
    if cantidad < 1024 ** 2:
        return f'{cantidad / 1024:.1f} KB'
    if cantidad < 1024 ** 3:
        return f'{cantidad / (1024 ** 2):.1f} MB'
    return f'{cantidad / (1024 ** 3):.2f} GB'


def _formato_duracion(segundos: float) -> str:
    segundos = max(0.0, segundos)
    horas = int(segundos // 3600)
    minutos = int((segundos % 3600) // 60)
    seg = int(segundos % 60)
    return f'{horas:02d}:{minutos:02d}:{seg:02d}'


def _bitrate_kbps(medicion: MedicionVideo) -> float:
    if medicion.duracion_video <= 0:
        return 0.0
    return (medicion.tamanio_bytes * 8) / medicion.duracion_video / 1000.0


def _linea(titulo: str, valor: str) -> str:
    return f'  {titulo:<30}: {valor}'


def _resumen_por_tamano(mediciones: List[MedicionVideo]) -> List[str]:
    validas = [m for m in mediciones if m.tamanio_bytes > 0]
    if not validas:
        return []
    validas.sort(key=lambda m: m.tamanio_bytes, reverse=True)
    lineas = [
        '=' * 78,
        'RESUMEN ORDENADO POR TAMAÑO DE VIDEO',
        '=' * 78,
        f'{"Nombre":<32} {"Tamaño":>10} {"Duración":>10} '
        f'{"CPU pico":>10} {"Análisis":>10}',
        '-' * 78,
    ]
    for m in validas:
        datos = resumen_medicion(m)
        nombre = m.nombre[:30] + ('…' if len(m.nombre) > 30 else '')
        lineas.append(
            f'{nombre:<32} '
            f'{_formato_bytes(m.tamanio_bytes):>10} '
            f'{_formato_duracion(m.duracion_video):>10} '
            f'{datos["cpu_max"]:>9.1f}% '
            f'{_formato_duracion(m.duracion_seg):>10}'
        )
    lineas.append('')
    return lineas


def _nota_plataforma() -> List[str]:
    plat = _plataforma()
    if plat == 'windows':
        return [
            'IMPORTANTE (Windows):',
            '  CPU puede superar 100 % porque Windows/psutil lo expresa por núcleo.',
            '  Ejemplo: 250 % equivale aproximadamente a 2.5 núcleos ocupados.',
            '  Con varios trabajadores, CPU/RAM de FFmpeg puede traslaparse entre',
            '  videos porque todos son hijos del mismo proceso Python.',
            '  Para medir con exactitud individual cada video, selecciona 1 trabajador.',
        ]
    if plat == 'android':
        return [
            'IMPORTANTE (Android / Termux):',
            '  CPU puede superar 100 % si se usan varios núcleos.',
            '  En Android la lectura de /proc puede ser limitada; los valores',
            '  de CPU y disco son aproximados.',
            '  Los contadores de io_counters() pueden no estar disponibles',
            '  en Termux; en ese caso lectura/escritura se reportan como 0.',
            '  La RAM medida es el RSS del proceso Python y sus hijos.',
            '  Con varios trabajadores, CPU/RAM de FFmpeg puede traslaparse entre',
            '  videos porque todos son hijos del mismo proceso Python.',
            '  Para medir con exactitud individual cada video, selecciona 1 trabajador.',
        ]
    if plat == 'macos':
        return [
            'IMPORTANTE (macOS):',
            '  CPU puede superar 100 % si se usan varios núcleos.',
            '  macOS gestiona la CPU con su propio scheduler (XNU) y puede',
            '  limitar procesos hijos según el estado térmico (thermal throttling).',
            '  Con varios trabajadores, CPU/RAM de FFmpeg puede traslaparse entre',
            '  videos porque todos son hijos del mismo proceso Python.',
            '  Para medir con exactitud individual cada video, selecciona 1 trabajador.',
        ]
    return [
        'IMPORTANTE (Linux):',
        '  CPU puede superar 100 % si se usan varios núcleos.',
        '  Ejemplo: 250 % equivale aproximadamente a 2.5 núcleos ocupados.',
        '  Linux gestiona la CPU con su scheduler CFS; los valores son precisos.',
        '  Con varios trabajadores, CPU/RAM de FFmpeg puede traslaparse entre',
        '  videos porque todos son hijos del mismo proceso Python.',
        '  Para medir con exactitud individual cada video, selecciona 1 trabajador.',
    ]


# ============================================================================
# Reporte TXT
# ============================================================================
def guardar_reporte_txt(mediciones: List[MedicionVideo], ruta_salida: str) -> None:
    """Escribe un TXT legible por humanos con una sección por video.

    El reporte está pensado para revisarse con Bloc de notas, VS Code,
    Notepad++ o cualquier editor de texto, sin depender de JSON.
    """
    destino = Path(ruta_salida)
    destino.parent.mkdir(parents=True, exist_ok=True)

    ancho = 78
    separador = '=' * ancho
    sub_separador = '-' * ancho

    info = _info_dispositivo()
    plat = info.get('plataforma', 'linux')

    lineas: List[str] = [
        separador,
        'REPORTE DE CONSUMO DE RECURSOS - VIDUP',
        separador,
        f'Generado                 : {datetime.now(ZONA_MEXICO).strftime("%Y-%m-%d %H:%M:%S")}',
        f'Plataforma               : {plat.upper()}',
        f'Hostname                 : {info.get("hostname", "?")}',
        f'Sistema operativo        : {info.get("sistema", "?")} {info.get("release", "")}'.strip(),
        f'CPU                      : {info.get("cpu_modelo", "?")} '
        f'({info.get("nucleos", "?")} núcleos, {info.get("arquitectura", "?")})',
        f'RAM total                : {info.get("ram_total_mb", "?")} MB',
    ]
    if plat == 'android':
        lineas.append(f'Marca/Modelo             : '
                      f'{info.get("marca", "?")} {info.get("modelo", "?")}')

    lineas.extend([
        f'Videos con medición       : {len(mediciones)}',
        f'Videos completados        : {sum(1 for m in mediciones if m.completado)}',
        f'Videos con error          : {sum(1 for m in mediciones if m.error)}',
        'Intervalo de muestreo    : 2.0 segundos',
        '',
    ])

    lineas.extend(_nota_plataforma())
    lineas.append('')

    for indice, medicion in enumerate(mediciones, start=1):
        datos = resumen_medicion(medicion)
        estado = 'COMPLETADO' if medicion.completado else 'NO COMPLETADO'
        if medicion.error:
            estado += f' — {medicion.error}'

        lineas.extend([
            sub_separador,
            f'VIDEO {indice}: {medicion.nombre}',
            sub_separador,
            _linea('Estado', estado),
            _linea('Ruta', medicion.ruta),
            _linea('Inicio de análisis', medicion.inicio_texto),
            _linea('Fin de análisis', medicion.fin_texto or 'No registrado'),
            _linea('Duración del análisis', _formato_duracion(medicion.duracion_seg)),
            _linea('Muestras tomadas', str(int(datos['muestras']))),
            '',
        ])

        if medicion.tamanio_bytes > 0:
            ancho_v, alto_v = medicion.resolucion
            resolucion_txt = f'{ancho_v}x{alto_v}' if ancho_v and alto_v else 'N/D'
            bitrate = _bitrate_kbps(medicion)
            bitrate_txt = f'{bitrate:.0f} kbps' if bitrate > 0 else 'N/D'
            fps_txt = f'{medicion.fps:.3f}' if medicion.fps > 0 else 'N/D'
            metodo_txt = medicion.metodo_acel or 'N/D'
            intervalo_txt = f'{medicion.frame_intervalo:.1f} s' \
                if medicion.frame_intervalo > 0 else 'N/D'

            lineas.extend([
                'INFORMACIÓN DEL VIDEO',
                _linea('Tamaño', _formato_bytes(medicion.tamanio_bytes)),
                _linea('Duración', _formato_duracion(medicion.duracion_video)),
                _linea('Resolución', resolucion_txt),
                _linea('FPS', fps_txt),
                _linea('Códec de vídeo', medicion.codec_video or 'N/D'),
                _linea('Códec de audio', medicion.codec_audio or 'N/D'),
                _linea('Bitrate aproximado', bitrate_txt),
                _linea('Método de decodificación', metodo_txt),
                _linea('Intervalo entre cuadros', intervalo_txt),
                _linea('Cuadros muestreados', str(medicion.frames_muestreados)),
                '',
            ])

        lineas.extend([
            'CPU DEL PROCESO Y SUS HIJOS',
            _linea('Pico de CPU', f'{datos["cpu_max"]:.1f} %'),
            _linea('CPU promedio', f'{datos["cpu_prom"]:.1f} %'),
            _linea('Máximo procesos hijos', str(int(datos['hijos_max']))),
            '',
            'MEMORIA RAM DEL PROCESO Y SUS HIJOS',
            _linea('Pico de RAM', f'{datos["ram_max"]:.1f} MB'),
            _linea('RAM promedio', f'{datos["ram_prom"]:.1f} MB'),
            '',
            'OPERACIONES DE DISCO APROXIMADAS',
            _linea('Lectura detectada', f'{datos["lectura_total"]:.1f} MB'),
            _linea('Escritura detectada', f'{datos["escritura_total"]:.1f} MB'),
            '',
        ])

    lineas.extend(_resumen_por_tamano(mediciones))
    lineas.extend([separador, 'FIN DEL REPORTE', separador, ''])

    with destino.open('w', encoding='utf-8') as archivo:
        archivo.write('\n'.join(lineas) + '\n')

    _imprimir(f'\nReporte de recursos guardado en: {destino}', 'green')


# ============================================================================
# Resumen por consola
# ============================================================================
def imprimir_resumen_consola(mediciones: List[MedicionVideo]) -> None:
    """Muestra un resumen breve de las mediciones en la consola."""
    if not mediciones:
        _imprimir('\nNo se registraron mediciones de recursos.', 'yellow')
        return

    datos_validos = [resumen_medicion(m) for m in mediciones if m.muestras]
    if not datos_validos:
        _imprimir('\nNo se tomaron muestras de recursos.', 'yellow')
        return

    pico_cpu = max(d['cpu_max'] for d in datos_validos)
    pico_ram = max(d['ram_max'] for d in datos_validos)
    tiempo_total = sum(m.duracion_seg for m in mediciones)
    plat = _plataforma()

    if CONSOLA is not None:
        tabla = Table(title='Resumen de consumo de recursos',
                      box=rich_box.ROUNDED, border_style='cyan', show_header=False)
        tabla.add_column('Campo', style='cyan')
        tabla.add_column('Valor', style='white')
        tabla.add_row('Plataforma', plat.upper())
        tabla.add_row('Videos medidos', str(len(mediciones)))
        tabla.add_row('Tiempo acumulado', _formato_duracion(tiempo_total))
        tabla.add_row('Pico máximo de CPU', f'{pico_cpu:.1f} %')
        tabla.add_row('Pico máximo de RAM', f'{pico_ram:.1f} MB')
        CONSOLA.print(tabla)
    else:
        print()
        print('=' * 72)
        print(' RESUMEN DE CONSUMO DE RECURSOS')
        print('=' * 72)
        print(f' Plataforma           : {plat.upper()}')
        print(f' Videos medidos            : {len(mediciones)}')
        print(f' Tiempo acumulado de videos : {_formato_duracion(tiempo_total)}')
        print(f' Pico máximo de CPU         : {pico_cpu:.1f} %')
        print(f' Pico máximo de RAM         : {pico_ram:.1f} MB')
        print('=' * 72)