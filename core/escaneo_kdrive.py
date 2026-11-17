# -*- coding: utf-8 -*-
"""Escanea kDrive montado con rclone y reutiliza SHA256 de un catálogo externo.

El equipo Vast.ai solo procesa: los registros pertenecen al dispositivo lógico
estable «kDrive (nube)». Los resultados van a una SQLite VIDUP aislada; no se
conectan a Neon ni se reescriben las rutas de los demás dispositivos.

La correspondencia nombre+tamaño es una identificación presuntiva, NO valida
criptográficamente el contenido remoto. Las claves ambiguas se descartan.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from contextlib import closing
from typing import Any

from core.horario import fmt_ahora_utc6, id_utc6
from core.presentacion import imprimir, pedir_numero, pedir_si_no, pedir_texto, rich_escape
from core.reporter import Dashboard, ProgresoFingerprintWorker
from core.dependencias import EXTENSIONES_VIDEO
from core.fingerprint import generar_fingerprint
from core import database

RAIZ_PROYECTO = Path(__file__).resolve().parent.parent
CATALOGO_PREDETERMINADO = RAIZ_PROYECTO / 'vidup_kdrive.db'
BD_PREDETERMINADA = RAIZ_PROYECTO / 'vidup_kdrive_resultados.db'
MONTAJE_PREDETERMINADO = Path('/mnt/kdrive/Definitivos')
from core.kdrive_identidad import (NOMBRE_DISPOSITIVO, ID_DISPOSITIVO,
                                   PREFIJO_RUTA, MARCADOR_BD)
SHA_VALIDO = re.compile(r'^[a-f0-9]{64}$')


@dataclass(frozen=True)
class Entrada:
    ruta: Path
    ruta_logica: str
    tamanio: int
    sha256: str


def cargar_catalogo(ruta: Path) -> dict[tuple[str, int], str]:
    """Carga SHA256 desde el catálogo intermedio o la SQLite creada por el importador.

    Formatos admitidos (ambos se abren con mode=ro):
    - catalogo(nombre, tamanio_bytes, sha256): archivo de hashes de origen.
    - videos(nombre, tamanio_bytes, hash_archivo): SQLite VIDUP con SHA importados.

    Nunca calcula nuevos hashes, cambia la identidad del dispositivo ni edita la
    base de entrada. Las coincidencias ambiguas de nombre+tamaño se omiten.
    """
    ruta = Path(ruta).expanduser().resolve()
    if not ruta.is_file():
        raise FileNotFoundError(f'No existe el archivo de SHA256: {ruta}')
    conn = sqlite3.connect(ruta.as_uri() + '?mode=ro', uri=True)
    try:
        tablas = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        catalogos = []
        tablas_vidup = []
        for tabla in tablas:
            seguro = tabla.replace('"', '""')
            cols = {str(c[1]).lower() for c in conn.execute(f'PRAGMA table_info("{seguro}")')}
            if {'nombre', 'tamanio_bytes', 'sha256'} <= cols:
                catalogos.append(seguro)
            if tabla == 'videos' and {'nombre', 'tamanio_bytes', 'hash_archivo'} <= cols:
                tablas_vidup.append(seguro)
        if len(catalogos) == 1:
            filas = conn.execute(
                f'SELECT nombre, tamanio_bytes, sha256 FROM "{catalogos[0]}"').fetchall()
        elif len(catalogos) > 1:
            raise ValueError('Hay varias tablas de SHA256; indique una SQLite inequívoca.')
        elif len(tablas_vidup) == 1:
            filas = conn.execute(
                f'SELECT nombre, tamanio_bytes, hash_archivo FROM "{tablas_vidup[0]}"').fetchall()
        else:
            raise ValueError(
                'No se encontró una tabla de SHA256 compatible. Se acepta '
                'catalogo(nombre, tamanio_bytes, sha256) o la base creada por '
                'migrar_sha256_kdrive.py con '
                'videos(nombre, tamanio_bytes, hash_archivo). '
                f'Tablas encontradas: {", ".join(tablas) or "ninguna"}.')
    finally:
        conn.close()
    if not filas:
        raise ValueError('El archivo de entrada no contiene SHA256 aprovechables.')
    indice: dict[tuple[str, int], str] = {}
    ambiguas: set[tuple[str, int]] = set()
    for nombre, tamanio, sha in filas:
        try:
            clave = (str(nombre).strip().casefold(), int(tamanio))
            valor = str(sha).strip().lower()
            if not clave[0] or clave[1] <= 0 or not SHA_VALIDO.fullmatch(valor):
                raise ValueError('Campo inválido')
        except (ValueError, TypeError) as exc:
            raise ValueError(f'Entrada de SHA256 no válida: {nombre!r}') from exc
        if clave in indice:
            # No se presupone que nombres y tamaños iguales prueben mismo hash.
            ambiguas.add(clave)
        indice[clave] = valor
    for clave in ambiguas:
        indice.pop(clave, None)
    if ambiguas:
        imprimir(f'[orange1]Aviso: {len(ambiguas)} claves duplicadas en el '
                 'origen; se omitirán para no asignar SHA256 incorrectos.[/]')
    return indice


def ruta_logica(ruta: Path, montaje: Path) -> str:
    """Obtiene una ruta remota independiente del punto de montaje de Vast.ai."""
    relativa = ruta.relative_to(montaje)
    if not relativa.parts or any(p in ('.', '..') for p in relativa.parts):
        raise ValueError('La ruta debe estar dentro del montaje de Definitivos.')
    return PREFIJO_RUTA + '/' + relativa.as_posix()


def resolver_coincidencias(montaje: Path, catalogo: dict[tuple[str, int], str]):
    """Indexa por nombre+tamaño sin leer bytes del vídeo ni calcular SHA256."""
    if not montaje.is_dir():
        raise FileNotFoundError(f'No se puede leer el montaje kDrive: {montaje}')
    coincidencias: dict[tuple[str, int], list[Path]] = defaultdict(list)
    encontrados = 0
    errores: list[dict[str, str]] = []
    def error_walk(exc):
        errores.append({'ruta': str(getattr(exc, 'filename', '')), 'motivo': str(exc)})

    for raiz, subdirs, archivos in os.walk(montaje, followlinks=False, onerror=error_walk):
        subdirs.sort()
        for nombre in sorted(archivos):
            if Path(nombre).suffix.lower() not in EXTENSIONES_VIDEO:
                continue
            archivo = Path(raiz) / nombre
            try:
                tamanio = archivo.stat().st_size
            except OSError as exc:
                errores.append({'ruta': str(archivo), 'motivo': f'Sin acceso a tamaño: {exc}'})
                continue
            encontrados += 1
            clave = (nombre.casefold(), int(tamanio))
            if clave in catalogo:
                coincidencias[clave].append(archivo)
            if encontrados % 300 == 0:
                imprimir(f'[turquoise2]Indexando kDrive: {encontrados} vídeos revisados.[/]')
    entradas: list[Entrada] = []
    ambiguos: list[dict[str, Any]] = []
    for clave, rutas in coincidencias.items():
        if len(rutas) != 1:
            ambiguos.append({'nombre': clave[0], 'tamanio_bytes': clave[1],
                             'rutas': [str(x) for x in rutas]})
            continue
        archivo = rutas[0]
        entradas.append(Entrada(archivo, ruta_logica(archivo, montaje), clave[1], catalogo[clave]))
    entradas.sort(key=lambda e: e.ruta_logica.casefold())
    return entradas, {
        'archivos_recorridos': encontrados,
        'sin_correspondencia': encontrados - sum(map(len, coincidencias.values())),
        'catalogo_sin_encontrar': len(catalogo) - len(coincidencias),
        'ambiguedades': ambiguos,
        'errores_acceso': errores,
    }


def preparar_bd_resultados(ruta_db: Path) -> None:
    """Crea solo una BD local independiente; prohíbe su sincronización automática."""
    if ruta_db.resolve() == CATALOGO_PREDETERMINADO.resolve():
        raise ValueError('El destino nunca puede ser el catálogo SHA256 original.')
    conn = database.conectar_sqlite(str(ruta_db))
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS vidup_catalogo_aislado ('
                     'tipo TEXT PRIMARY KEY, descripcion TEXT NOT NULL)')
        conn.execute('INSERT OR IGNORE INTO vidup_catalogo_aislado(tipo,descripcion) VALUES (?,?)',
                     (MARCADOR_BD, 'Resultados de kDrive: no sincronizar con Neon'))
        conn.commit()
    finally:
        conn.close()


def _copiar_video_a_ram(e: Entrada, destino_ram: Path) -> None:
    """Descarga temporal rclone; no calcula SHA256 sobre los archivos remotos."""
    remoto = e.ruta_logica.replace('kdrive:/', 'kdrive:', 1)
    cmd = ['rclone', 'copyto', remoto, str(destino_ram), '--retries', '2']
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if proc.returncode != 0 or not destino_ram.is_file():
        raise RuntimeError('Error descargando a RAM: ' + proc.stderr[-500:])


def _fingerprint_un_video(e: Entrada, intervalo: float, hilos: int, aceleracion: str,
                           dashboard: Dashboard, cancelacion: threading.Event,
                           ffprobe_timeout: float = 180.0, medidor=None,
                           registro_mediciones=None, bloqueo_mediciones=None):
    """Mide rclone + FFmpeg; deja constancia de errores o cancelaciones."""
    worker = threading.get_ident()
    callback = ProgresoFingerprintWorker(dashboard, worker, e.ruta.name)
    medicion = medidor.iniciar_medicion(e.ruta_logica) if medidor else None
    if medicion:
        # El informe mantiene la ruta lógica, pero monitoriza los argumentos
        # reales de rclone copyto y FFmpeg sobre el archivo temporal.
        medicion.rutas_procesos.append(e.ruta_logica.replace('kdrive:/', 'kdrive:', 1))
    completado = False
    try:
        base_tmp = '/dev/shm' if os.path.isdir('/dev/shm') else None
        with tempfile.TemporaryDirectory(prefix='vidup_ram_', dir=base_tmp) as tmpdir:
            destino_ram = Path(tmpdir) / e.ruta.name
            if medicion:
                medicion.rutas_procesos.append(str(destino_ram))
            dashboard.actualizar_worker(worker, f'{e.ruta.name[:35]} descargando a RAM...')
            _copiar_video_a_ram(e, destino_ram)
            fp = generar_fingerprint(str(destino_ram), intervalo=intervalo, progreso=callback,
                                     cancelar=cancelacion, hilos_ffmpeg=hilos,
                                     aceleracion=aceleracion, sha256_importado=e.sha256,
                                     ffprobe_timeout=ffprobe_timeout,
                                     omitir_validacion_rapida=True)
            if medicion and hasattr(fp, 'frame_hashes'):
                medicion.rellenar_info_video(fp)
            completado = not getattr(fp, 'error', None)
            return fp
    except Exception as exc:
        if medicion:
            medicion.error = str(exc)
        raise
    finally:
        if medicion:
            medidor.finalizar_medicion(medicion, completado=completado)
            if registro_mediciones is not None:
                if bloqueo_mediciones is not None:
                    with bloqueo_mediciones:
                        registro_mediciones.append(medicion)
                else:
                    registro_mediciones.append(medicion)


def escanear_kdrive(catalogo: Path, montaje: Path, destino: Path, *,
                    intervalo: float = 10., trabajadores: int = 2, hilos: int = 2,
                    aceleracion: str = 'cuda', limite: int = 0,
                    solo_plan: bool = False,
                    ffprobe_timeout: float = 180.0) -> dict[str, Any]:
    """Procesa candidatos sin releer el SHA completo ni tocar Neon.

    Si nombre+tamaño apunta a más de un archivo, se informa ambigüedad y no
    se analiza ni guarda ninguno de ellos, salvo que se resuelva por separado.
    """
    catalogo = Path(catalogo).expanduser().resolve()
    montaje = Path(montaje).expanduser().resolve()
    destino = Path(destino).expanduser().resolve()
    if catalogo == destino:
        raise ValueError('Catálogo SHA256 y SQLite de resultados deben ser archivos diferentes.')
    if not (intervalo > 0 and trabajadores >= 1 and hilos >= 1 and limite >= 0):
        raise ValueError('Intervalo, trabajadores e hilos deben ser positivos.')
    if not (15.0 <= ffprobe_timeout <= 1800.0):
        raise ValueError('El tiempo de ffprobe debe estar entre 15 y 1800 segundos.')
    hashes = cargar_catalogo(catalogo)
    entradas, diagnostico = resolver_coincidencias(montaje, hashes)
    if destino.exists():
        with closing(sqlite3.connect(str(destino))) as con:
            tablas = {x[0] for x in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tablas and ('videos' not in tablas or 'vidup_catalogo_aislado' not in tablas):
                raise ValueError('El destino contiene otra base. Elija un archivo nuevo para resultados kDrive.')
            if 'videos' in tablas and not con.execute(
                'SELECT 1 FROM vidup_catalogo_aislado WHERE tipo=?', (MARCADOR_BD,)).fetchone():
                raise ValueError('El destino es un catálogo VIDUP ajeno: operación bloqueada.')
    previa = {}
    if destino.is_file():
        with closing(sqlite3.connect(str(destino))) as con:
            con.row_factory = sqlite3.Row
            for fila in con.execute('SELECT v.ruta, v.hash_archivo, v.tamanio_bytes, '
                      'v.audio_huella, v.frame_intervalo, COUNT(fh.id) AS frames FROM videos v '
                      'LEFT JOIN frame_hashes fh ON fh.video_id=v.id '
                      'GROUP BY v.id'):
                previa[str(fila['ruta'])] = dict(fila)
    pendientes, ya_completos, conflictos = [], 0, []
    for e in entradas:
        fila = previa.get(e.ruta_logica)
        if fila:
            if str(fila['hash_archivo']).lower() != e.sha256 or int(fila['tamanio_bytes']) != e.tamanio:
                conflictos.append({'ruta': e.ruta_logica, 'motivo': 'SHA256 o tamaño no coincide con la BD existente'})
                continue
            if int(fila['frames']) > 0 and fila['audio_huella']:
                ya_completos += 1
                continue
        pendientes.append(e)
    if limite:
        pendientes = pendientes[:limite]
    resultado: dict[str, Any] = {
        'fecha_utc6': fmt_ahora_utc6(), 'origen': str(catalogo),
        'montaje': str(montaje), 'dispositivo': NOMBRE_DISPOSITIVO,
        'dispositivo_id': ID_DISPOSITIVO, 'destino': str(destino),
        'sha256_catalogados': len(hashes), 'coincidencias_unicas': len(entradas),
        'ya_completos': ya_completos, 'pendientes': len(pendientes),
        'procesados': 0, 'errores': [], 'conflictos': conflictos,
        'diagnostico': diagnostico, 'neon': 'NO SE CONSULTA NI SE MODIFICA',
        'sha256_importado': 'sin calcular SHA256 completo ni rápido del archivo',
        'ffprobe_timeout_s': ffprobe_timeout,
        'ffprobe_consultas_por_video': 1,
        'advertencia': 'Nombre+tamaño no prueba identidad criptográfica del archivo remoto.',
    }
    if solo_plan or not pendientes:
        return resultado
    preparar_bd_resultados(destino)
    from concurrent.futures import ThreadPoolExecutor
    try:
        from core.recursos import MedidorRecursos
        medidor = MedidorRecursos(intervalo_muestreo=2.0)
    except Exception:
        medidor = None
    mediciones = []
    bloqueo_mediciones = threading.Lock()
    inicio_fecha = fmt_ahora_utc6()
    dashboard = Dashboard(num_workers=min(trabajadores, len(pendientes)))
    cancelacion = threading.Event()
    dashboard.iniciar(len(pendientes))
    inicio = time.monotonic()
    try:
        # Cola acotada: no abrir miles de archivos simultáneamente en FUSE.
        with ThreadPoolExecutor(max_workers=trabajadores) as executor:
            it = iter(pendientes)
            futuros = {}
            def enviar_siguiente():
                try:
                    entrada = next(it)
                except StopIteration:
                    return
                futuro = executor.submit(_fingerprint_un_video, entrada, intervalo,
                                         hilos, aceleracion, dashboard, cancelacion,
                                         ffprobe_timeout, medidor,
                                         mediciones, bloqueo_mediciones)
                futuros[futuro] = entrada
            for _ in range(min(trabajadores, len(pendientes))):
                enviar_siguiente()
            while futuros:
                terminados, _ = wait(futuros, timeout=.3, return_when=FIRST_COMPLETED)
                for futuro in terminados:
                    e = futuros.pop(futuro)
                    try:
                        fp = futuro.result()
                    except Exception as exc:
                        resultado['errores'].append({'ruta': e.ruta_logica, 'motivo': str(exc)})
                        try:
                            from core.almacenamiento import _registrar_error_video_detallado
                            _registrar_error_video_detallado(e.ruta.name,e.ruta_logica,str(exc),str(exc))
                        except Exception:
                            pass
                        dashboard.marcar_error()
                        enviar_siguiente()
                        continue
                    if fp.error:
                        detalle = (getattr(fp,'detalle_tecnico','') or '')[-1500:]
                        resultado['errores'].append({'ruta': e.ruta_logica, 'motivo': fp.error,
                                                    'detalle': detalle})
                        try:
                            from core.almacenamiento import _registrar_error_video_detallado
                            _registrar_error_video_detallado(e.ruta.name,e.ruta_logica,fp.error,detalle)
                        except Exception:
                            pass
                        dashboard.marcar_error()
                    elif fp.tamanio_bytes != e.tamanio or fp.hash_archivo != e.sha256:
                        resultado['errores'].append({'ruta': e.ruta_logica,
                                                    'motivo': 'El tamaño o SHA importado no coincide'})
                        dashboard.marcar_error()
                    else:
                        # No retirar huellas previamente obtenidas durante un
                        # reintento parcial; únicamente enriquecer lo ausente.
                        anterior = previa.get(e.ruta_logica)
                        if anterior and int(anterior['frames']) > 0:
                            with closing(sqlite3.connect(str(destino))) as lectura:
                                fila_anterior = lectura.execute(
                                    'SELECT id FROM videos WHERE ruta=? AND dispositivo_id=?',
                                    (e.ruta_logica, ID_DISPOSITIVO)).fetchone()
                                if fila_anterior:
                                    fp.frame_hashes = [v[0] for v in lectura.execute(
                                        'SELECT hash_valor FROM frame_hashes WHERE video_id=? ORDER BY posicion',
                                        (fila_anterior[0],))]
                                    fp.frame_intervalo = float(anterior['frame_intervalo'] or intervalo)
                        if anterior and anterior.get('audio_huella') and not fp.audio_huella:
                            fp.audio_huella = str(anterior['audio_huella'])
                        # IMPORTANTE: se guarda solo el identificador remoto estable.
                        fp.ruta = e.ruta_logica
                        fp.dispositivo = NOMBRE_DISPOSITIVO
                        fp.dispositivo_id = ID_DISPOSITIVO
                        conn = database.conectar_sqlite(str(destino))
                        try:
                            guardado = database.guardar_fingerprint(
                                conn, fp, dispositivo=NOMBRE_DISPOSITIVO,
                                dispositivo_id=ID_DISPOSITIVO)
                        finally:
                            conn.close()
                        if guardado is None:
                            resultado['errores'].append({'ruta': e.ruta_logica,
                                                        'motivo': 'No se pudo guardar en SQLite'})
                            dashboard.marcar_error()
                        else:
                            resultado['procesados'] += 1
                            dashboard.marcar_completado()
                    enviar_siguiente()
    except KeyboardInterrupt:
        cancelacion.set()
        resultado['interrumpido'] = True
        dashboard.imprimir_mensaje('Cancelado por usuario; resultados guardados hasta ahora.')
    finally:
        dashboard.detener()
        resultado['segundos'] = round(time.monotonic() - inicio, 2)
        carpeta = RAIZ_PROYECTO / 'reportes' / 'kdrive'
        carpeta.mkdir(parents=True, exist_ok=True)
        salida = carpeta / f'escaneo_kdrive_{id_utc6()}.json'
        salida.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding='utf-8')
        resultado['reporte'] = str(salida)
        try:
            from core.kdrive_reportes import guardar_reportes_escaneo_kdrive
            resultado['informes'] = guardar_reportes_escaneo_kdrive(
                resultado, mediciones, inicio_fecha=inicio_fecha, carpeta=RAIZ_PROYECTO/'reportes')
        except Exception as exc:
            resultado['error_reportes'] = str(exc)
            imprimir('[red]ERROR generando reportes de kDrive:[/] '+rich_escape(str(exc)))
        salida.write_text(json.dumps(resultado, ensure_ascii=False, indent=2)+'\n',encoding='utf-8')
    return resultado


def escaneo_kdrive_interactivo() -> None:
    """Opción exclusiva del menú de escaneo; no cambia la BD activa del usuario."""
    from core.interfaz import menu_rich
    menu_rich('Escaneo kDrive · GPU en Vast.ai', [
        ('1', 'Analizar huellas visuales y audio usando SHA256 importados'),
        ('2', 'Simular emparejamiento y revisar conflictos sin procesar'),
        ('0', 'Volver al escaneo'), ('00', 'Volver al principal'),
    ], subtitulo='Resultados en SQLite separada; Neon desactivado para este catálogo.')
    op = pedir_texto('Selecciona operación', '2', permitir_escape=False).strip()
    if op in ('0', '00'):
        if op == '00':
            from core.presentacion import IrAlMenuPrincipal
            raise IrAlMenuPrincipal()
        return
    if op not in ('1','2'):
        imprimir('[orange1]Opción no válida.[/]')
        return
    cat = Path(pedir_texto('SQLite de SHA256 importados', str(CATALOGO_PREDETERMINADO),
                          permitir_escape=False)).expanduser()
    mont = Path(pedir_texto('Montaje de kDrive', str(MONTAJE_PREDETERMINADO),
                           permitir_escape=False)).expanduser()
    dst = Path(pedir_texto('SQLite VIDUP de resultados', str(BD_PREDETERMINADA),
                          permitir_escape=False)).expanduser()
    kwargs = {}
    if op == '1':
        kwargs['intervalo'] = float(pedir_numero('Segundos entre fotogramas', 10.0, 0.1))
        kwargs['trabajadores'] = int(pedir_numero('Trabajadores simultáneos', 2, 1, entero=True))
        kwargs['hilos'] = int(pedir_numero('Hilos FFmpeg por trabajador', 2, 1, entero=True))
        acel = pedir_texto('Aceleración (cuda/software/auto)', 'cuda', permitir_escape=False).strip().lower()
        kwargs['aceleracion'] = acel if acel in ('cuda','software','auto') else 'cuda'
        kwargs['limite'] = int(pedir_numero('Límite de vídeos (0 = todos)', 3, 0, entero=True))
        kwargs['ffprobe_timeout'] = float(pedir_numero(
            'Espera máxima de ffprobe por vídeo en montaje remoto (segundos)',
            180, 15, entero=True))
    try:
        res = escanear_kdrive(cat, mont, dst, solo_plan=(op=='2'), **kwargs)
    except (ValueError, FileNotFoundError, OSError) as exc:
        imprimir(f'[indian_red1]kDrive: {rich_escape(str(exc))}[/]')
        return
    imprimir(f'[spring_green2]Emparejamientos únicos: {res["coincidencias_unicas"]}; '
             f'procesados: {res["procesados"]}; pendientes: {res["pendientes"]}.[/]')
    imprimir(f'[gold1]Conflictos: {len(res["conflictos"])}; '
             f'ambigüedades: {len(res["diagnostico"]["ambiguedades"])}.[/]')
    imprimir('[turquoise2]Base de resultados:[/] ' + rich_escape(str(dst)))
    if res.get('reporte'):
        imprimir('[turquoise2]Reporte:[/] ' + rich_escape(res['reporte']))
    imprimir('[orange1]No se subió ningún registro a Neon. '
             'La base de kDrive se puede comparar desde el menú 2.[/]')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='VIDUP: analizar kDrive sin recalcular SHA256')
    parser.add_argument('--catalogo', type=Path, default=CATALOGO_PREDETERMINADO)
    parser.add_argument('--montaje', type=Path, default=MONTAJE_PREDETERMINADO)
    parser.add_argument('--destino', type=Path, default=BD_PREDETERMINADA)
    parser.add_argument('--intervalo', type=float, default=10.0)
    parser.add_argument('--trabajadores', type=int, default=2)
    parser.add_argument('--hilos', type=int, default=2)
    parser.add_argument('--aceleracion', choices=['cuda', 'software', 'auto'], default='cuda')
    parser.add_argument('--limite', type=int, default=0)
    parser.add_argument('--ffprobe-timeout', type=float, default=180.0,
                        help='Límite para un único ffprobe por vídeo remoto (15-1800 s; 180 por defecto).')
    parser.add_argument('--solo-plan', action='store_true')
    args = parser.parse_args(argv)
    res = escanear_kdrive(args.catalogo, args.montaje, args.destino,
                         intervalo=args.intervalo, trabajadores=args.trabajadores,
                         hilos=args.hilos, aceleracion=args.aceleracion,
                         limite=args.limite, solo_plan=args.solo_plan,
                         ffprobe_timeout=args.ffprobe_timeout)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if not res['errores'] and not res['conflictos'] else 2


if __name__ == '__main__':
    raise SystemExit(main())