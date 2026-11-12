"""Gestión de almacenamiento, carpetas y estructura de reportes."""

from __future__ import annotations

import json
from core.horario import fmt_ahora_utc6 as _fmt_ahora_utc6
from core.presentacion import imprimir, pedir_ruta, pedir_texto, RegresarAlMenu

from pathlib import Path
from typing import Any, List, Tuple

# Directorio del script principal (vidup.py)
DIR_SCRIPT: Path = Path(__file__).resolve().parent.parent


def directorio_script() -> Path:
    """Retorna la ruta del directorio donde está vidup.py."""
    return DIR_SCRIPT


def carpeta_reportes() -> Path:
    """Retorna la ruta de la carpeta de reportes."""
    return DIR_SCRIPT / 'reportes'


def carpeta_salida_videos() -> Path:
    """Retorna la ruta de la carpeta donde se guardan vídeos movidos."""
    return DIR_SCRIPT / 'movidos'


def ruta_errores_jsonl() -> Path:
    """Retorna la ruta del archivo de errores en formato JSONL."""
    return carpeta_reportes() / 'errores' / 'jsonl' / 'errores_videos.jsonl'


def ruta_errores_txt() -> Path:
    """Retorna la ruta del archivo de errores en formato TXT."""
    return carpeta_reportes() / 'errores' / 'txt' / 'errores_videos.txt'


def carpeta_errores_detalle() -> Path:
    """Retorna la carpeta donde se guardan los TXT detallados por vídeo."""
    return carpeta_reportes() / 'errores' / 'detalle_txt'


def ruta_errores_detalle_txt() -> Path:
    """Retorna la ruta del TXT detallado del vídeo fallido más reciente."""
    return carpeta_errores_detalle() / 'ultimo_video_fallido.txt'


_carpeta_reportes = carpeta_reportes
_ruta_errores_jsonl = ruta_errores_jsonl
_ruta_errores_txt = ruta_errores_txt


def asegurar_estructura_reportes() -> None:
    """Crea la estructura de carpetas de reportes si no existe."""
    base = carpeta_reportes()
    for sub in (
        'comparativas/json', 'comparativas/csv', 'comparativas/txt',
        'recursos/txt',
        'errores/jsonl', 'errores/txt',
        'exportaciones/json', 'exportaciones/txt',
        'movimientos/json', 'movimientos/txt', 'movimientos/jsonl',
        'sha256/json', 'sha256/csv', 'sha256/txt', 'sha256/jsonl',
        'respaldos/db',
        'escaneos/json', 'escaneos/txt',
        'estadisticas/json', 'estadisticas/txt', 'estadisticas/csv',
        'verificaciones/json', 'verificaciones/txt', 'verificaciones/csv',
        'depuraciones/json', 'depuraciones/txt', 'depuraciones/csv',
        'otros',
        'launcher',
    ):
        (base / sub).mkdir(parents=True, exist_ok=True)


def base_reciente_por_defecto() -> str:
    """Retorna la ruta de la base de datos SQLite por defecto del dispositivo o la más reciente en respaldos."""
    try:
        from core.sincronizacion import obtener_ruta_db_local_dispositivo
        db_local = obtener_ruta_db_local_dispositivo()
        if db_local:
            return str(db_local)
    except Exception:
        pass

    carpeta = carpeta_reportes() / 'respaldos' / 'db'
    candidatas: List[Tuple[int, Path]] = []
    if carpeta.is_dir():
        for ruta in carpeta.glob('*.db'):
            if not ruta.is_file():
                continue
            try:
                candidatas.append((ruta.stat().st_mtime_ns, ruta))
            except OSError:
                continue
    if candidatas:
        candidatas.sort(reverse=True)
        return str(candidatas[0][1])
    return str(DIR_SCRIPT / 'vidup.db')


def obtener_rutas_historial_escaneo() -> List[Path]:
    """Retorna las rutas guardadas en el historial de escaneos anteriores."""
    archivo = DIR_SCRIPT / '.vidup_rutas_historial.json'
    if not archivo.is_file():
        return []
    try:
        with archivo.open('r', encoding='utf-8') as f:
            datos = json.load(f)
            rutas_str = datos.get('rutas', [])
            rutas_validas = []
            for r in rutas_str:
                p = Path(r)
                if p.is_dir():
                    rutas_validas.append(p)
            return rutas_validas
    except Exception:
        return []


def guardar_ruta_historial_escaneo(ruta: Any) -> None:
    """Guarda una ruta de directorio en el historial permanente de escaneos."""
    if not ruta:
        return
    p = Path(ruta).resolve()
    if not p.is_dir():
        return
    archivo = DIR_SCRIPT / '.vidup_rutas_historial.json'
    rutas = []
    if archivo.is_file():
        try:
            with archivo.open('r', encoding='utf-8') as f:
                datos = json.load(f)
                rutas = datos.get('rutas', [])
        except Exception:
            pass
    ruta_str = str(p)
    if ruta_str in rutas:
        rutas.remove(ruta_str)
    rutas.insert(0, ruta_str)
    rutas = rutas[:15]  # Mantener hasta 15 rutas recientes
    try:
        with archivo.open('w', encoding='utf-8') as f:
            json.dump({'rutas': rutas}, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _rutas_con_error_registrado() -> set:
    rutas: set = set()
    for log in (_ruta_errores_jsonl(), _carpeta_reportes() / 'errores_videos.jsonl'):
        if not log.is_file():
            continue
        try:
            with log.open('r', encoding='utf-8') as archivo:
                for linea in archivo:
                    try:
                        registro = json.loads(linea)
                        ruta = registro.get('ruta')
                        if ruta:
                            rutas.add(str(Path(ruta).resolve()))
                    except (json.JSONDecodeError, AttributeError):
                        continue
        except OSError:
            pass
    return rutas

def _quitar_error_registrado(rutas) -> None:
    """Quita del registro de errores (JSONL y TXT) las rutas que ya tienen huella completa."""
    objetivo = {str(Path(r).resolve()) for r in rutas}
    if not objetivo:
        return
    for log in [_ruta_errores_jsonl(), _carpeta_reportes() / 'errores_videos.jsonl']:
        if not log.is_file():
            continue
        try:
            conservar = []
            for linea in log.read_text(encoding='utf-8').splitlines(keepends=True):
                try:
                    ruta = json.loads(linea).get('ruta')
                except (json.JSONDecodeError, AttributeError):
                    conservar.append(linea)
                    continue
                if ruta and str(Path(ruta).resolve()) in objetivo:
                    continue
                conservar.append(linea)
            log.write_text(''.join(conservar), encoding='utf-8')
        except OSError:
            pass
    txt = _ruta_errores_txt()
    if txt.is_file():
        try:
            bloques = txt.read_text(encoding='utf-8').split('\n\n')
            conservar = []
            for bloque in bloques:
                quitar = False
                for linea in bloque.split('\n'):
                    if linea.startswith('Ruta: '):
                        try:
                            quitar = str(Path(linea[6:].strip()).resolve()) in objetivo
                        except OSError:
                            quitar = False
                if not quitar:
                    conservar.append(bloque)
            txt.write_text('\n\n'.join(conservar), encoding='utf-8')
        except OSError:
            pass


def _registrar_error_video(nombre: str, ruta: str, causa: str) -> None:
    log_jsonl = _ruta_errores_jsonl()
    log_txt = _ruta_errores_txt()
    try:
        from core.database import info_dispositivo_actual
        info_disp = info_dispositivo_actual()
    except Exception:
        info_disp = {}

    registro = {
        'fecha': _fmt_ahora_utc6(),
        'archivo': nombre,
        'ruta': str(Path(ruta).resolve()),
        'causa': causa,
        'dispositivo': info_disp.get('dispositivo', ''),
        'dispositivo_id': info_disp.get('dispositivo_id', ''),
    }

    try:
        log_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with log_jsonl.open('a', encoding='utf-8') as archivo:
            archivo.write(json.dumps(registro, ensure_ascii=False) + '\n')
        log_txt.parent.mkdir(parents=True, exist_ok=True)
        with log_txt.open('a', encoding='utf-8') as archivo:
            archivo.write(
                f'Fecha: {registro["fecha"]}\n'
                f'Dispositivo: {registro["dispositivo"]} (ID {registro["dispositivo_id"]})\n'
                f'Archivo: {nombre}\nRuta: {registro["ruta"]}\nCausa: {causa}\n\n'
            )
    except OSError as exc:
        imprimir(f'[yellow][AVISO] No se pudo guardar el log de errores: {exc}[/]')


def _interpretar_error_ffmpeg(lineas: List[str]) -> str:
    """Traduce las líneas literales de FFmpeg a una explicación legible."""
    texto = '\n'.join(lineas).lower()
    if 'moov atom not found' in texto:
        return (
            'El archivo está dañado o incompleto: FFmpeg no encuentra el índice '
            '"moov" que guarda las pistas del vídeo. Suele ocurrir cuando la '
            'descarga o la copia se cortó a la mitad.'
        )
    if 'invalid data found when processing input' in texto:
        return (
            'El archivo no es un vídeo válido o está corrupto: FFmpeg no reconoce '
            'el formato. Puede que tenga la extensión ".mp4" pero contenga otro '
            'tipo de datos.'
        )
    if 'permission denied' in texto:
        return 'FFmpeg no tiene permiso para leer el archivo.'
    if 'no such file or directory' in texto:
        return 'El archivo ya no existe o fue movido mientras se analizaba.'
    if 'unknown encoder' in texto or 'unknown decoder' in texto:
        return (
            'La versión instalada de FFmpeg no reconoce un códec usado por el '
            'vídeo. Se necesita una build más reciente.'
        )
    if 'decoder' in texto and 'not found' in texto:
        return 'Falta un decodificador en la instalación de FFmpeg para este vídeo.'
    if 'does not contain any stream' in texto:
        return 'El archivo no contiene pistas de vídeo ni de audio analizables.'
    if 'invalid argument' in texto:
        return 'FFmpeg recibió un parámetro o un valor no válido para este vídeo.'
    if 'timed out' in texto or 'timeout' in texto:
        return 'FFmpeg tardó demasiado y se canceló. El disco puede estar lento.'
    if 'resource temporarily unavailable' in texto:
        return 'El archivo estaba ocupado por otro proceso al momento de leerlo.'
    return 'FFmpeg no pudo procesar el archivo. El detalle literal está más abajo.'


def _registrar_error_video_detallado(
    nombre: str,
    ruta: str,
    causa: str,
    detalle: str = '',
) -> None:
    """Registra el fallo y escribe un TXT en lenguaje humano por vídeo.

    El archivo .jsonl queda como registro técnico para máquinas; este TXT
    es el que una persona puede leer para saber qué pasó y qué hacer.
    """
    _registrar_error_video(nombre, ruta, causa)

    if not detalle or not detalle.strip():
        return

    try:
        from core.database import info_dispositivo_actual
        info_disp = info_dispositivo_actual()
    except Exception:
        info_disp = {}

    lineas = [linea for linea in detalle.splitlines() if linea.strip()]
    explicacion = _interpretar_error_ffmpeg(lineas)

    carpeta = carpeta_errores_detalle()
    archivo = carpeta / f'{Path(nombre).stem}.txt'
    try:
        carpeta.mkdir(parents=True, exist_ok=True)
        with archivo.open('w', encoding='utf-8') as manejador:
            manejador.write(
                'REPORTE DE ERROR DE ANÁLISIS DE VÍDEO\n'
                '=====================================\n\n'
                f'Fecha: {_fmt_ahora_utc6()}\n'
                f'Vídeo: {nombre}\n'
                f'Ruta completa: {ruta}\n'
                f'Dispositivo: {info_disp.get("dispositivo", "")} '
                f'(ID {info_disp.get("dispositivo_id", "")})\n\n'
                'QUÉ PASÓ\n'
                '---------\n'
                f'{causa}\n\n'
                'QUÉ SIGNIFICA\n'
                '--------------\n'
                f'{explicacion}\n\n'
                'QUÉ DICE EXACTAMENTE FFmpeg\n'
                '----------------------------\n'
                'Estas son las líneas literales que devolvió la herramienta:\n\n'
            )
            for linea in lineas:
                manejador.write(f'  {linea}\n')
            manejador.write(
                '\n\nCÓMO PROCEDER\n'
                '--------------\n'
                '1. Abre el vídeo con VLC o un reproductor para confirmar si se '
                'reproduce con normalidad.\n'
                '2. Si no se reproduce, vuelve a copiarlo o descargarlo desde su '
                'origen: probablemente esté incompleto.\n'
                '3. Si sí se reproduce, prueba con:\n'
                '   ffmpeg -i "RUTA" -f null -\n'
                '   y compara lo que aparece con el detalle de arriba.\n'
            )
        # Copia del último fallo, para consulta rápida.
        try:
            with ruta_errores_detalle_txt().open('w', encoding='utf-8') as manejador:
                manejador.write(archivo.read_text(encoding='utf-8'))
        except OSError:
            pass
    except OSError as exc:
        imprimir(f'[yellow][AVISO] No se pudo escribir el detalle del error: {exc}[/]')

def _archivos_recientes(patrones: List[str], subcarpetas: List[str]) -> List[Path]:
    base = _carpeta_reportes()
    encontrados: List[Tuple[int, Path]] = []
    for sub in subcarpetas:
        carpeta = base / sub
        if not carpeta.is_dir():
            continue
        for patron in patrones:
            for ruta in carpeta.glob(patron):
                if ruta.is_file():
                    try:
                        encontrados.append((ruta.stat().st_mtime_ns, ruta))
                    except OSError:
                        continue
    encontrados.sort(reverse=True)
    return [r for _, r in encontrados]

def elegir_archivo_reciente(
    descripcion: str,
    patrones: List[str],
    subcarpetas: List[str],
    defecto: str = '',
    obligatorio: bool = True,
) -> str:
    # En operaciones normales se propone primero la SQLite del dispositivo,
    # nunca un respaldo reciente que pueda acabar siendo modificado por error.
    if defecto and Path(str(defecto)).suffix.lower() in ('.db', '.sqlite', '.sqlite3'):
        return pedir_ruta(descripcion or 'Ruta de la base SQLite',
                         str(defecto), existente=False)

    recientes = _archivos_recientes(patrones, subcarpetas)

    if recientes:
        reciente = recientes[0]
        imprimir(f'\n[deep_sky_blue1]{descripcion}[/]')
        imprimir(f'  [gold1]Más reciente:[/] [bright_white]{reciente}[/]')
        if len(recientes) > 1:
            imprimir(f'  [grey37]({len(recientes) - 1} archivo(s) más antiguo(s) disponibles)[/]')

        try:
            respuesta = pedir_texto('¿Usar el más reciente? (s/n)', 's').lower()
        except RegresarAlMenu:
            raise

        if respuesta in ('s', 'si', 'sí', 'y', 'yes', ''):
            return str(reciente)

    return pedir_ruta(descripcion or 'Ruta del archivo', defecto, existente=False)
