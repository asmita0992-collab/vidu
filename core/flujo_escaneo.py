from __future__ import annotations
import argparse
import json
import math
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from core.horario import ahora_utc6, fmt_ahora_utc6, id_utc6, ZONA_MEXICO
from core.plataforma import (
    detectar_so, so_legible, distro_linux, es_android,
    arquitectura_maquina, es_windows, es_macos, es_linux
)
from core.presentacion import (
    imprimir, regla, panel, pedir_si_no, pedir_numero, pedir_texto,
    pedir_ruta, pedir_limite_escaneo, RegresarAlMenu, _PALABRAS_ESCAPE, _MAX_INTENTOS,
    linea_dep, linea_opcional, escapar_para_telegram,
    CONSOLA, TIENE_RICH, Panel, Rule,
    NC, NEGRITA, AZUL, AMARILLO, VERDE, ROJO, GRIS, CIAN, BLANCO, MAGENTA,
)
from core.reporter import (
    Dashboard, ProgresoFingerprintWorker, _fmt_seg, _mostrar_desglose_estadisticas,
)
from core.telegram import (
    configurado as telegram_configurado, enviar as telegram_enviar,
    notificar_inicio, notificar_fin, notificar_error, probar as probar_telegram
)
from core.dependencias import verificar_obligatorias, EXTENSIONES_VIDEO
from core.almacenamiento import (
    carpeta_reportes as _carpeta_reportes,
    ruta_errores_jsonl as _ruta_errores_jsonl,
    ruta_errores_txt as _ruta_errores_txt,
    asegurar_estructura_reportes as _asegurar_estructura_reportes,
    base_reciente_por_defecto as _base_reciente_por_defecto,
    carpeta_salida_videos as _carpeta_salida_videos,
    _rutas_con_error_registrado, _registrar_error_video, _quitar_error_registrado,
    _archivos_recientes, elegir_archivo_reciente,
)
from core.plataforma import set_high_priority, _carpetas_android_candidatas
from core.inventario import _es_video, _listar_videos
from core.datos import (
    _guardar_resumen_json,
    _filtrar_videos_pendientes, _cargar_hashes_previos, _consultar_hashes_previos,
    cmd_exportar, cmd_importar, cmd_descargar_neon, cmd_stats,
    cmd_limpiar, cmd_eliminar_por_dispositivo,
)
_ahora_utc6 = ahora_utc6
_fmt_ahora_utc6 = fmt_ahora_utc6
_id_utc6 = id_utc6
_notificar_inicio = notificar_inicio
_notificar_fin = notificar_fin
_notificar_error = notificar_error
DIR_SCRIPT: Path = Path(__file__).resolve().parent.parent
ENV_PATH: Path = DIR_SCRIPT / ".env"
try:
    from dotenv import load_dotenv
    if ENV_PATH.is_file():
        load_dotenv(dotenv_path=ENV_PATH, override=False)
except ImportError:
    pass
try:
    from rich.table import Table
    from rich.markup import escape as rich_escape
    from rich import box as rich_box
    from rich.progress import (
        Progress, SpinnerColumn, BarColumn, TextColumn,
        TimeElapsedColumn, TimeRemainingColumn, MofNCompleteColumn,
    )
except ImportError:
    Table = None  # type: ignore
    rich_box = None  # type: ignore
    rich_escape = lambda texto: str(texto)
    Progress = None  # type: ignore
_PALABRAS_SALIR = {'0', '00', 'salir', 'exit', 'quit', 'q'}


def pedir_con_cuenta_regresiva(mensaje: str, timeout: int, por_defecto: str) -> str:
    if not sys.stdin.isatty():
        return por_defecto

    if os.name == 'nt':
        try:
            import msvcrt
        except ImportError:
            return por_defecto

        inicio = time.time()
        entrada = ''
        while True:
            restante = timeout - int(time.time() - inicio)
            if restante <= 0:
                sys.stdout.write(f'\r\033[2K{mensaje} [0s] -> {por_defecto}\n')
                sys.stdout.flush()
                return por_defecto

            sys.stdout.write(f'\r\033[2K{mensaje} [{restante}s]: {entrada}')
            sys.stdout.flush()

            t_espera = time.time()
            while time.time() - t_espera < 0.2:
                if msvcrt.kbhit():
                    c = msvcrt.getwche()
                    if c in ('\r', '\n'):
                        sys.stdout.write('\n')
                        resultado = entrada.strip()
                        if resultado.lower() in _PALABRAS_ESCAPE:
                            raise KeyboardInterrupt
                        return resultado if resultado else por_defecto
                    elif c == '\b':
                        entrada = entrada[:-1]
                        sys.stdout.write(' \b')
                    else:
                        entrada += c
                time.sleep(0.05)
    else:
        try:
            import select
            import tty
            import termios
        except ImportError:
            return por_defecto

        fd = sys.stdin.fileno()
        try:
            old_settings = termios.tcgetattr(fd)
        except termios.error:
            return por_defecto

        try:
            tty.setcbreak(fd)
            inicio = time.time()
            entrada = ''
            while True:
                restante = timeout - int(time.time() - inicio)
                if restante <= 0:
                    sys.stdout.write(f'\r\033[2K{mensaje} [0s] -> {por_defecto}\n')
                    sys.stdout.flush()
                    return por_defecto

                sys.stdout.write(f'\r\033[2K{mensaje} [{restante}s]: {entrada}')
                sys.stdout.flush()

                r, _, _ = select.select([sys.stdin], [], [], 0.2)
                if r:
                    c = sys.stdin.read(1)
                    if c in ('\n', '\r'):
                        sys.stdout.write('\n')
                        resultado = entrada.strip()
                        if resultado.lower() in _PALABRAS_ESCAPE:
                            raise KeyboardInterrupt
                        return resultado if resultado else por_defecto
                    elif c in ('\x08', '\x7f'):
                        entrada = entrada[:-1]
                    else:
                        entrada += c
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _pausa_entre_lotes(segundos: int) -> bool:
    """Pausa entre lotes para enfriar el equipo. Ctrl+C la interrumpe (devuelve False)."""
    imprimir(f'[cyan]Pausa de {segundos // 60} min para enfriar el equipo. Ctrl+C para detener los lotes.[/]')
    restante = segundos
    try:
        while restante > 0:
            espera = min(30, restante)
            time.sleep(espera)
            restante -= espera
            if restante > 0:
                imprimir(f'[grey37]  faltan {restante // 60} min {restante % 60} s[/]')
    except KeyboardInterrupt:
        return False
    return True


def _escanear_por_lotes(args: argparse.Namespace, videos: List[Path], trabajadores: int, tamanio_lote: int) -> int:
    """Divide los vídeos en lotes; cada lote se escanea y guarda antes de la pausa."""
    lotes = [videos[i:i + tamanio_lote] for i in range(0, len(videos), tamanio_lote)]
    imprimir(f'[cyan]Escaneo por lotes: {len(videos)} vídeo(s) en {len(lotes)} lote(s) de {tamanio_lote}; pausa de 3 min entre lotes.[/]')
    codigo_final = 0
    for n, lote in enumerate(lotes, 1):
        imprimir(f'\n[bold cyan]=== Lote {n}/{len(lotes)} ({len(lote)} vídeo(s)) ===[/]')
        sub = argparse.Namespace(**vars(args))
        sub.lote_videos = None
        sub.lista_videos = lote
        sub.trabajadores = trabajadores
        sub.max_videos = len(lote)
        if getattr(args, 'resumen_json', None):
            sub.resumen_json = args.resumen_json.replace('.json', f'_lote{n:03d}.json')
        codigo = cmd_escanear(sub)
        if codigo == 130:
            return 130
        if codigo:
            codigo_final = codigo
        if n < len(lotes) and not _pausa_entre_lotes(180):
            imprimir('[yellow]Lotes detenidos por el usuario.[/]')
            return 130
    return codigo_final


def _conviene_lotes(cantidad: int, recomendado: int) -> bool:
    """Pregunta por lotes solo con muchos vídeos: 2x trabajadores recomendados, o más de 5 en Android."""
    if es_android():
        return cantidad > 5
    return cantidad >= 2 * max(1, recomendado)


def cmd_escanear(args: argparse.Namespace) -> int:
    from core.fingerprint import _trabajo_fingerprint, _ejecutar_escaneo_acotado, _cpu_threads_recomendados
    from core.database import _usa_neon, cerrar, conectar, guardar_fingerprint, describir_conexion, info_dispositivo_actual
    from core.reporter import imprimir_resumen_escaneo
    from core.recursos import MedidorRecursos

    if not math.isfinite(args.intervalo) or args.intervalo <= 0:
        imprimir('[red][ERROR] El intervalo debe ser positivo y finito.[/]')
        return 2
    if args.hilos_ffmpeg <= 0:
        imprimir('[red][ERROR] Los hilos de FFmpeg deben ser un entero mayor a cero.[/]')
        return 2

    notificar_inicio('Escanear vídeos', f'Directorio: {args.directorio or "(por defecto)"}')

    print()
    try:
        if args.carpetas_sub is None:
            ans_sub = pedir_con_cuenta_regresiva("¿Escanear también subcarpetas? (y/n)", 30, "n")
            recursivo = ans_sub.lower() in ('y', 'si', 'sí', 'yes')
        else:
            recursivo = args.carpetas_sub
    except KeyboardInterrupt:
        imprimir('[yellow]Operación cancelada.[/]')
        return 0

    esperados_sha: Dict[str, dict] = {}
    manifiesto_sha = getattr(args, 'manifiesto_sha', None)
    if manifiesto_sha:
        from core.sha256 import cargar_pendientes
        datos_sha, videos_encontrados, esperados_sha, avisos = cargar_pendientes(manifiesto_sha)
        directorio = datos_sha.get('carpeta') or '(rutas del JSON SHA256)'
        imprimir(f'[deep_sky_blue1]Escaneo limitado a los vídeos nuevos del reporte: {manifiesto_sha}[/]')
        for aviso in avisos:
            imprimir(f'[yellow][AVISO] {aviso}[/]')
        if not videos_encontrados:
            imprimir('[yellow]No hay vídeos nuevos disponibles para escanear en ese JSON.[/]')
            return 0 if not avisos else 1
    else:
        if args.directorio:
            directorio = str(Path(args.directorio).expanduser().resolve())
            videos_encontrados = _listar_videos(directorio, recursivo=recursivo)
        else:
            directorio = str(DIR_SCRIPT)
            imprimir(f'[deep_sky_blue1]Buscando vídeos en: {directorio}[/]')
            videos_encontrados = _listar_videos(directorio, recursivo=recursivo)
            if not videos_encontrados:
                imprimir('[yellow]No se encontraron vídeos en la carpeta del script.[/]')
                directorio = pedir_ruta('Directorio a escanear', existente=True, directorio=True)
                if not directorio:
                    return 1
                videos_encontrados = _listar_videos(directorio, recursivo=recursivo)
        if not videos_encontrados:
            imprimir(f'[red]No se encontraron archivos de vídeo en: {directorio}[/]')
            return 1

    if getattr(args, 'lista_videos', None) is not None:
        videos_encontrados = list(args.lista_videos)

    # Las rutas de un manifiesto JSON también deben respetar exclusiones.
    from core import inventario as _inventario_escaneo
    _exclusiones = _inventario_escaneo.cargar_exclusiones(
        DIR_SCRIPT, info_dispositivo_actual()['dispositivo_id'])
    antes_excluir = len(videos_encontrados)
    videos_encontrados = [p for p in videos_encontrados
                          if not _inventario_escaneo.excluido(str(p), _exclusiones)]
    if antes_excluir != len(videos_encontrados):
        imprimir(f'[yellow]Omitidos por exclusión: {antes_excluir - len(videos_encontrados)}[/]')
    if not videos_encontrados:
        imprimir('[yellow]No hay vídeos elegibles después de aplicar las exclusiones.[/]')
        return 0

    imprimir(f'[cyan]Inventario inicial: {len(videos_encontrados)} vídeo(s) elegibles. Revisando registros anteriores...[/]')
    rutas_con_error = _rutas_con_error_registrado()
    videos_pendientes, omitidos_previos = _filtrar_videos_pendientes(videos_encontrados, args.db, args.reintentar_errores, args.forzar_registrados)

    imprimir(f'[cyan]Pendientes de escaneo: {len(videos_pendientes)}.[/]')
    if not videos_pendientes:
        errores_registrados = sum(1 for ruta in videos_encontrados if str(ruta.resolve()) in rutas_con_error)
        if errores_registrados:
            imprimir(f'[yellow]{errores_registrados} vídeo(s) figuran en el registro de errores. Puedes reintentar su escaneo o depurar los errores en el menú.[/]')
            notificar_fin('Escanear vídeos', False, f'{errores_registrados} vídeo(s) con error registrado')
        if getattr(args, 'resumen_json', None):
            _guardar_resumen_json(args.resumen_json, 0, omitidos_previos, [], 0.0)
        return 0

    pendientes_disponibles = len(videos_pendientes)

    if args.max_videos is None:
        ans_max = pedir_con_cuenta_regresiva(
            f"¿Cuántos vídeos analizar de los {pendientes_disponibles} pendientes? (T = todos)",
            30, "5")
        try:
            if ans_max.strip().lower() in ('t', 'todos'):
                max_v = pendientes_disponibles
            else:
                max_v = int(ans_max)
            if max_v < 1 or max_v > pendientes_disponibles:
                max_v = min(5, pendientes_disponibles)
        except ValueError:
            max_v = min(5, pendientes_disponibles)
    else:
        max_v = args.max_videos
        if max_v <= 0:
            imprimir('[red][ERROR] El límite de vídeos debe ser mayor a cero.[/]')
            return 2

    videos = videos_pendientes[:max_v]
    imprimir(f'[cyan]Iniciando escaneo: {len(videos)} vídeo(s); primero: {videos[0].name}[/]')

    recomendado = _cpu_threads_recomendados()
    if args.trabajadores is None:
        ans_wk = pedir_con_cuenta_regresiva(
            f"¿Cuántos trabajadores quieres utilizar? (Sugerido {recomendado})",
            30, str(recomendado))
        try:
            trabajadores = int(ans_wk)
            if trabajadores <= 0:
                trabajadores = recomendado
        except ValueError:
            trabajadores = recomendado
    else:
        trabajadores = args.trabajadores
        if trabajadores <= 0:
            imprimir('[red][ERROR] La cantidad de trabajadores mínima es 1.[/]')
            return 2

    lote_videos = getattr(args, 'lote_videos', None)
    if (not lote_videos and getattr(args, 'preguntar_lotes', False)
            and getattr(args, 'lista_videos', None) is None
            and _conviene_lotes(len(videos), recomendado)
            and pedir_si_no('¿Trabajar en lotes? (pausa de 3 min entre lotes para guardar y enfriar la PC)', False)):
        lote_videos = pedir_numero('Vídeos por lote', 10, 1, entero=True)
    if lote_videos and getattr(args, 'lista_videos', None) is None:
        return _escanear_por_lotes(args, videos, trabajadores, int(lote_videos))

    if args.aceleracion == 'auto':
        acel_desc = 'auto (detectará aceleradores disponibles)'
    else:
        acel_desc = args.aceleracion

    info_disp = info_dispositivo_actual()

    if CONSOLA is not None and Table is not None and rich_box is not None:
        tabla = Table(box=rich_box.ROUNDED, border_style='cyan', show_header=False)
        tabla.add_column(style='cyan'); tabla.add_column(style='white')
        tabla.add_row('Directorio', str(directorio))
        tabla.add_row('Vídeos', f'{len(videos)} seleccionados')
        tabla.add_row('Base datos', 'Neon PostgreSQL (Nube)' if _usa_neon() else str(args.db))
        tabla.add_row('Modo conexión', describir_conexion())
        tabla.add_row('Intervalo', f'{args.intervalo} s entre cuadros')
        tabla.add_row('Trabajadores', str(trabajadores))
        tabla.add_row('Hilos FFmpeg/trabajador', str(args.hilos_ffmpeg))
        tabla.add_row('Aceleración', acel_desc)
        tabla.add_row('Dispositivo', info_disp['dispositivo'])
        tabla.add_row('ID dispositivo', info_disp['dispositivo_id'])
        CONSOLA.print(tabla)
    else:
        print(f' Directorio        : {directorio}')
        print(f' Vídeos            : {len(videos)} seleccionados')
        print(f' Base datos        : {"Neon PostgreSQL (Nube)" if _usa_neon() else args.db}')
        print(f' Modo de conexión  : {describir_conexion()}')
        print(f' Intervalo         : {args.intervalo} s entre cuadros')
        print(f' Trabajadores      : {trabajadores}')
        print(f' Hilos FFmpeg/trabajador: {args.hilos_ffmpeg}')
        print(f' Aceleración video : {acel_desc}')
        print(f' Dispositivo       : {info_disp["dispositivo"]}')
        print(f' ID dispositivo    : {info_disp["dispositivo_id"]}')

    so_nombre = detectar_so().upper()
    es_recursivo = recursivo
    nota_subcarpetas = "Se analizarán los archivos de esta carpeta y sus subcarpetas." if es_recursivo else "Solo se analizarán archivos ubicados strictly en esta carpeta, ignorando todas las subcarpetas."

    if CONSOLA is not None:
        CONSOLA.print(f"\n[bold white]Sistema operativo detectado:[/] [bold green]{so_nombre}[/]")
        CONSOLA.print(f"[bold yellow]Nota: {nota_subcarpetas}[/]\n")
        CONSOLA.print(f"[bold white]Archivos a procesar:[/] [bold cyan]{len(videos)}[/]\n")
    else:
        print(f"\nSistema operativo detectado: {so_nombre}")
        print(f"Nota: {nota_subcarpetas}\n")
        print(f"Archivos a procesar: {len(videos)}\n")

    try:
        medidor_recursos = MedidorRecursos(intervalo_muestreo=2.0)
    except Exception:
        medidor_recursos = None
    mediciones = []
    fallos_detalle: List[Tuple[str, str, str]] = []

    dashboard = Dashboard(num_workers=trabajadores, tty=sys.stdout.isatty())
    evento_cancelacion = threading.Event()
    interrumpido = False
    inicio_total = time.monotonic()

    procesados = 0
    errores = 0
    omitidos = omitidos_previos

    manejador_anterior = signal.getsignal(signal.SIGINT)

    def manejar_ctrl_c(signum, frame):
        nonlocal interrumpido
        if not evento_cancelacion.is_set():
            interrumpido = True
            evento_cancelacion.set()
            return
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, manejar_ctrl_c)

    try:
        hashes_previos = _cargar_hashes_previos(videos, args.db)
        imprimir(f'[cyan]Cola acotada a {max(1, trabajadores * 2)} tareas; iniciando los primeros vídeos...[/]')
        dashboard.iniciar(len(videos))

        with ThreadPoolExecutor(max_workers=trabajadores, thread_name_prefix='vidup') as executor:
            futuros: Dict[Future, Path] = {}
            def preparar(ruta):
                datos_esperados = esperados_sha.get(str(ruta.resolve()), {})
                return (_trabajo_fingerprint, ruta, args.intervalo, dashboard,
                        evento_cancelacion, args.hilos_ffmpeg, args.aceleracion,
                        medidor_recursos, datos_esperados.get('sha256'),
                        datos_esperados.get('tamanio_bytes'),
                        hashes_previos.get(str(ruta.resolve())))
            
            for futuro, ruta in _ejecutar_escaneo_acotado(
                    executor, videos, trabajadores, evento_cancelacion, preparar, futuros):
                if evento_cancelacion.is_set():
                    interrumpido = True
                    for pendiente in futuros:
                        pendiente.cancel()
                    break

                try:
                    _, resultado = futuro.result()
                except KeyboardInterrupt:
                    interrumpido = True
                    evento_cancelacion.set()
                    for pendiente in futuros:
                        pendiente.cancel()
                    break
                except Exception as exc:
                    errores += 1
                    err_msg = f'Excepción en hilo: {exc}'
                    rr = str(ruta.resolve())
                    fallos_detalle.append((ruta.name, rr, err_msg))
                    _registrar_error_video(ruta.name, rr, err_msg)
                    dashboard.imprimir_mensaje(f'❌ {ruta.name[:40]} | ERROR: {exc}')
                    dashboard.marcar_error()
                    continue

                estado = resultado.get('estado')
                medicion = resultado.get('medicion')
                if medicion is not None and estado == 'ok':
                    mediciones.append(medicion)

                if estado == 'cancelado':
                    interrumpido = True
                    evento_cancelacion.set()
                    break

                if estado == 'error':
                    errores += 1
                    err_msg = resultado.get('error', 'Error no especificado')
                    rr = str(ruta.resolve())
                    fallos_detalle.append((ruta.name, rr, err_msg))
                    _registrar_error_video(ruta.name, rr, err_msg)
                    dashboard.imprimir_mensaje(f'❌ {ruta.name[:40]} | ERROR: {err_msg}')
                    dashboard.marcar_error()
                    continue

                if estado != 'ok':
                    errores += 1
                    err_msg = 'Estado no reconocido'
                    rr = str(ruta.resolve())
                    fallos_detalle.append((ruta.name, rr, err_msg))
                    _registrar_error_video(ruta.name, rr, err_msg)
                    dashboard.imprimir_mensaje(f'❌ {ruta.name[:40]} | ERROR desconocido')
                    dashboard.marcar_error()
                    continue

                if evento_cancelacion.is_set():
                    interrumpido = True
                    break

                fp = resultado['fingerprint']
                conn = conectar(args.db)
                try:
                    video_id = guardar_fingerprint(conn, fp)
                finally:
                    cerrar(conn)

                if video_id is None:
                    errores += 1
                    err_msg = 'Fallo al insertar registros en la base de datos'
                    rr = str(ruta.resolve())
                    fallos_detalle.append((ruta.name, rr, err_msg))
                    _registrar_error_video(ruta.name, rr, err_msg)
                    dashboard.imprimir_mensaje(f'❌ {ruta.name[:40]} | ERROR al guardar en BD')
                    dashboard.marcar_error()
                else:
                    procesados += 1
                    _quitar_error_registrado([str(ruta.resolve())])
                    dashboard.marcar_completado()

    except KeyboardInterrupt:
        interrumpido = True
        evento_cancelacion.set()
    finally:
        signal.signal(signal.SIGINT, manejador_anterior)
        dashboard.detener()

    if interrumpido:
        imprimir('[yellow]Escaneo interrumpido por el usuario.[/]')
        imprimir('[grey37]Los vídeos incompletos no se guardaron. Los ya procesados están en la base.[/]')

    tiempo_total = time.monotonic() - inicio_total
    imprimir_resumen_escaneo(procesados=procesados, errores=errores, omitidos=omitidos, tiempo_seg=tiempo_total, interrumpido=interrumpido)

    if fallos_detalle:
        if CONSOLA is not None and Rule is not None:
            CONSOLA.print(Rule(f'[bold red]Vídeos con error ({len(fallos_detalle)})[/]', style='red'))
            for nom, rta, err in fallos_detalle:
                CONSOLA.print(f'[red]•[/] [bold]{nom}[/]')
                CONSOLA.print(f'  [grey37]{rta}[/]')
                CONSOLA.print(f'  [yellow]{err}[/]')
        else:
            print(f'\n{ROJO}{NEGRITA}=== VÍDEOS CON ERROR ({len(fallos_detalle)}) ==={NC}')
            for nom, rta, err in fallos_detalle:
                print(f'{ROJO}• {NEGRITA}{nom}{NC}')
                print(f'{GRIS}  {rta}{NC}')
                print(f'{AMARILLO}  {err}{NC}\n')

    if mediciones:
        try:
            from core.recursos import guardar_reporte_txt, imprimir_resumen_consola
            nombre = f'recursos_{_id_utc6()}.txt'
            ruta = str(_carpeta_reportes() / 'recursos' / 'txt' / nombre)
            guardar_reporte_txt(mediciones, ruta)
            imprimir_resumen_consola(mediciones)
        except Exception as exc:
            imprimir(f'[yellow][AVISO] No se pudo generar el reporte de recursos: {exc}[/]')

    if getattr(args, 'resumen_json', None):
        _guardar_resumen_json(
            args.resumen_json, procesados, omitidos,
            [{'archivo': n, 'ruta': r, 'causa': c} for n, r, c in fallos_detalle],
            tiempo_total)

    if interrumpido:
        msg_tg = f'Interrumpido con {procesados} procesados y {errores} errores'
    elif procesados == 0 and errores > 0:
        msg_tg = f'0 procesados, {errores} error(es)'
    else:
        msg_tg = f'{procesados} procesados, {errores} error(es)'

    notificar_fin('Escanear vídeos', errores == 0 and not interrumpido and procesados > 0,
                  msg_tg, procesados=procesados, errores=errores)

    return 130 if interrumpido else 0


def _verificar_dependencias_escaneo() -> bool:
    """Requisitos mínimos del análisis visual con mensajes accionables."""
    faltantes = [b for b in ('ffmpeg', 'ffprobe') if not shutil.which(b)]
    try:
        import PIL  # noqa: F401
        import imagehash  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as exc:
        imprimir(f'[red]No se puede iniciar el escaneo visual: falta {exc.name}.[/]')
        imprimir('[yellow]Instala los requisitos de VIDUP (requirements.txt).[/]')
        return False
    if faltantes:
        imprimir(f'[red]No se puede iniciar el escaneo visual: faltan {", ".join(faltantes)} en PATH.[/]')
        return False
    return True


_exclusiones_resueltas = False  # Solo en esta sesión: si el dispositivo no tiene carpetas excluidas, pregunta una sola vez.


def escanear_interactivo(db_defecto: str, reporte_sha: Optional[str] = None) -> str:
    global _exclusiones_resueltas
    from core.database import _usa_neon

    if not _verificar_dependencias_escaneo():
        return db_defecto

    from core.database import info_dispositivo_actual
    from core import inventario
    device_id = info_dispositivo_actual()['dispositivo_id']
    # Si el dispositivo ya tiene al menos una carpeta excluida registrada, no se pregunta.
    # Si no la tiene, se pregunta una sola vez por sesión.
    if not inventario.cargar_exclusiones(DIR_SCRIPT, device_id) and not _exclusiones_resueltas:
        if pedir_si_no('¿Configurar las carpetas excluidas antes de escanear?', False):
            gestionar_exclusiones()
        _exclusiones_resueltas = True

    if reporte_sha:
        from core.sha256 import cargar_pendientes
        datos, candidatos, _, avisos = cargar_pendientes(reporte_sha)
        for aviso in avisos:
            imprimir(f'[yellow][AVISO] {aviso}[/]')
        if not candidatos:
            imprimir('[yellow]Ese reporte no contiene vídeos nuevos disponibles para escanear.[/]')
            return db_defecto
        referencia = datos.get('ruta_db', '')
        sugerida = referencia if referencia and Path(referencia).suffix.lower() != '.json' else db_defecto
        directorio = None
        recursivo = False
        imprimir(f'[cyan]Vídeos nuevos disponibles en el JSON: {len(candidatos)}[/]')
    else:
        candidatas = _carpetas_android_candidatas() if es_android() else []
        from core.almacenamiento import obtener_rutas_historial_escaneo, guardar_ruta_historial_escaneo
        historial = obtener_rutas_historial_escaneo()
        
        # Combinar candidatas sin duplicados
        candidatas_totales: List[Path] = []
        for c in candidatas + historial:
            p = Path(c).resolve()
            if p.is_dir() and p not in candidatas_totales:
                candidatas_totales.append(p)

        if candidatas_totales:
            imprimir('\n[cyan]Carpetas de vídeo candidatas o utilizadas anteriormente:[/]')
            for i, c in enumerate(candidatas_totales, 1):
                imprimir(f'  [gold1]{i}.[/] {rich_escape(str(c))}')
            imprimir(f'  [gold1]{len(candidatas_totales) + 1}.[/] Otra carpeta (ruta manual)')
            opcion = pedir_numero(
                'Elige una carpeta',
                1, 1, len(candidatas_totales) + 1, entero=True,
            )
            if opcion == len(candidatas_totales) + 1:
                directorio = pedir_ruta(
                    'Directorio de vídeos', str(DIR_SCRIPT),
                    existente=True, directorio=True,
                )
            else:
                directorio = str(candidatas_totales[int(opcion) - 1])
            guardar_ruta_historial_escaneo(directorio)
            recursivo = pedir_si_no('¿Incluir subcarpetas?', True)
        else:
            directorio = pedir_ruta(
                'Directorio de vídeos', str(DIR_SCRIPT),
                existente=True, directorio=True,
            )
            guardar_ruta_historial_escaneo(directorio)
            recursivo = pedir_si_no('¿Incluir subcarpetas?', True)
        sugerida = db_defecto  

    if _usa_neon():
        db = 'neon_cloud'
    else:
        if Path(sugerida).suffix.lower() == '.json':
            sugerida = _base_reciente_por_defecto()
        
        db_sug = sugerida

        db = elegir_archivo_reciente(
            'Base de datos SQLite donde guardar las huellas',
            ['*.db'],
            ['respaldos/db'],
            defecto=db_sug,
        )
        if Path(db).suffix.lower() == '.json':
            raise ValueError('El escaneo guarda huellas en SQLite; selecciona una base .db.')

    reintentar_errores = pedir_si_no('¿Reintentar el escaneo de vídeos que previamente generaron error?', True)
    forzar_registrados = pedir_si_no('¿Volver a analizar vídeos que ya están registrados exitosamente?', False)
    limite = pedir_limite_escaneo()
    from core.fingerprint import _cpu_threads_recomendados
    trabajadores = pedir_numero('Vídeos simultáneos (trabajadores)', _cpu_threads_recomendados(), 1, entero=True)
    intervalo = 10.0
    hilos = 2
    aceleracion = 'auto'
    if pedir_si_no('¿Configurar intervalo, hilos y aceleración?', False):
        intervalo = pedir_numero('Segundos entre cuadros', 10, 0.001)
        hilos = pedir_numero('Hilos de FFmpeg por trabajador', 2, 1, entero=True)
        imprimir('[cyan]Aceleración: amf, qsv, d3d11va, dxva2, vaapi, cuda, mediacodec, videotoolbox, vulkan, software, auto[/]')
        permitidos = {'amf', 'qsv', 'd3d11va', 'dxva2', 'vaapi', 'cuda',
                      'mediacodec', 'videotoolbox', 'vulkan', 'software', 'auto'}
        while True:
            aceleracion = pedir_texto('Aceleración', 'auto').lower()
            if aceleracion in permitidos:
                break
            imprimir('[yellow]Elige uno de los modos indicados.[/]')

    resumen_json = _carpeta_reportes() / 'escaneos' / 'json' / f'resumen_escaneo_{_id_utc6()}.json'
    args = argparse.Namespace(
        db=db, directorio=directorio, carpetas_sub=recursivo, 
        reintentar_errores=reintentar_errores, forzar_registrados=forzar_registrados,
        max_videos=limite if limite is not None else 2**63 - 1, trabajadores=trabajadores,
        intervalo=intervalo, hilos_ffmpeg=int(hilos), aceleracion=aceleracion,
        medir_recursos=True, manifiesto_sha=reporte_sha, resumen_json=str(resumen_json),
        lote_videos=None, preguntar_lotes=True, lista_videos=None,
    )
    estado = cmd_escanear(args)
    if estado != 0:
        imprimir(f'[red]El escaneo terminó con código {estado}. Revisa los avisos anteriores y reportes/errores/.[/]')
    return db


def _tras_menu_sha256(salida, db_defecto: str) -> str:
    """Decide la base activa y si escanear tras el menú SHA256 (orquestación)."""
    if salida:
        resultado = salida['resultado']
        ruta_db = salida.get('ruta_db')
        if ruta_db and not str(ruta_db).startswith('N/A') and Path(ruta_db).suffix.lower() != '.json':
            db_defecto = ruta_db
        if resultado.get('modo') == 'comparar' and resultado.get('total_nuevos') and pedir_si_no(
                '¿Escanear ahora únicamente los vídeos sin coincidencias?', False):
            db_defecto = escanear_interactivo(db_defecto, salida['ruta_json'])
    return db_defecto


def gestionar_exclusiones() -> None:
    """Configuración persistente de exclusiones por ID, sin modificar su identidad."""
    from core.database import info_dispositivo_actual
    from core import inventario
    ident = info_dispositivo_actual()
    device_id = ident['dispositivo_id']
    while True:
        actuales = inventario.cargar_exclusiones(DIR_SCRIPT, device_id)
        imprimir(f'\n[cyan]Carpetas excluidas en {rich_escape(ident["dispositivo"])} '
                 f'(ID {rich_escape(device_id)}):[/]')
        for i, ruta in enumerate(actuales, 1):
            imprimir(f'  [gold1]{i}.[/] {rich_escape(ruta)}')
        if not actuales:
            imprimir('  [grey70](ninguna)[/]')
        imprimir('  [green]A.[/] Añadir carpeta  [yellow]Q.[/] Quitar carpeta  [cyan]00.[/] Volver')
        opcion = pedir_texto('Elegir operación', '00').lower()
        if opcion in ('00', '0'):
            return
        if opcion == 'a':
            ruta = pedir_texto('Carpeta a excluir (ruta absoluta)', obligatorio=True)
            # No normalizar con Path.resolve(): puede tratarse de una ruta Windows.
            if not (Path(ruta).is_absolute() or inventario.es_windows(ruta)):
                imprimir('[red]Escribe una ruta absoluta, no relativa.[/]')
                continue
            if inventario.normalizar(ruta) in {inventario.normalizar(x) for x in actuales}:
                imprimir('[yellow]Esa carpeta ya está excluida.[/]')
                continue
            inventario.guardar_exclusiones(DIR_SCRIPT, device_id, [*actuales, ruta])
            imprimir('[green]Exclusión guardada (también se aplica a SHA256).[/]')
        elif opcion == 'q' and actuales:
            valor = int(pedir_numero('Número de exclusión a quitar', 1, 1, len(actuales), entero=True))
            if pedir_si_no(f'¿Quitar la exclusión {actuales[valor - 1]}?', False):
                inventario.guardar_exclusiones(DIR_SCRIPT, device_id,
                                             [r for i, r in enumerate(actuales) if i != valor - 1])
                imprimir('[green]Exclusión retirada.[/]')
