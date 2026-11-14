"""Orquesta comparaciones de vídeos y fragmentos entre catálogos VIDUP.

Presenta preguntas y resultados con Rich, usa reporter.py para el avance
y delega los algoritmos de comparación a core/comparator.py.
"""

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
from core.menus import (_cabecera_submenu, _pedir_opcion)



def _validar_umbrales(args: argparse.Namespace) -> bool:
    if not 0 <= args.umbral_phash <= 64:
        imprimir('[red][ERROR] La distancia pHash debe estar entre 0 y 64.[/]')
        return False
    porcentajes = {
        'El umbral visual': args.umbral_visual,
        'El umbral de fragmento': args.umbral_fragmento,
        'El umbral de posible fragmento': args.umbral_posible_fragmento,
        'El margen de duración': args.margen_duracion,
        'La proporción mínima': args.ratio_min,
    }
    for nombre, valor in porcentajes.items():
        if not math.isfinite(valor) or valor < 0 or valor > 100:
            imprimir(f'[red][ERROR] {nombre} debe estar entre 0 y 100.[/]')
            return False
    if args.umbral_posible_fragmento > args.umbral_fragmento:
        imprimir('[red][ERROR] El umbral de posible fragmento no puede superar el de fragmento confirmado.[/]')
        return False
    return True


def _cargar_registros_para_sha256(fuente: str, neon: bool = False) -> List[Any]:
    """Carga SOLO campos de video, nunca frame_hashes ni imágenes.

    Acepta SQLite, exportaciones JSON de VIDUP y Neon en la base principal.
    Las bases adicionales se leen expresamente de su archivo local.
    """
    import sqlite3
    from types import SimpleNamespace
    from core.database import cerrar, conectar

    campos = ('ruta', 'nombre', 'hash_archivo', 'tamanio_bytes',
              'dispositivo', 'dispositivo_id', 'duracion', 'ancho', 'alto',
              'fps', 'codec_video', 'codec_audio', 'bitrate_video_kbps')
    if neon:
        conn = conectar('neon_cloud')
        try:
            if not hasattr(conn, 'cursor'):
                raise RuntimeError('No se pudo abrir la base Neon.')
            with conn.cursor() as cursor:
                cursor.execute('SELECT ' + ', '.join(campos) +
                               ' FROM videos WHERE error IS NULL')
                filas = [dict(zip(campos, row)) for row in cursor.fetchall()]
        finally:
            cerrar(conn)
    else:
        ruta = Path(fuente).expanduser().resolve()
        if not ruta.is_file():
            raise FileNotFoundError(f'No existe la base: {ruta}')
        if ruta.suffix.lower() == '.json':
            with ruta.open('r', encoding='utf-8-sig') as f:
                datos = json.load(f)
            if not isinstance(datos, dict) or not isinstance(datos.get('videos'), list):
                raise ValueError('Se requiere un JSON de huellas VIDUP con campo videos.')
            filas = [r for r in datos['videos'] if isinstance(r, dict) and not r.get('error')]
        else:
            conn = sqlite3.connect(ruta.as_uri() + '?mode=ro', uri=True)
            try:
                cursor = conn.execute('SELECT ' + ', '.join(campos) +
                                      ' FROM videos WHERE error IS NULL')
                filas = [dict(zip(campos, row)) for row in cursor]
            finally:
                conn.close()

    resultados = []
    for fila in filas:
        if not fila.get('ruta'):
            continue
        try:
            resultados.append(SimpleNamespace(
                ruta=str(fila['ruta']),
                nombre=str(fila.get('nombre') or Path(fila['ruta']).name),
                hash_archivo=str(fila.get('hash_archivo') or ''),
                tamanio_bytes=int(fila.get('tamanio_bytes') or 0),
                dispositivo=str(fila.get('dispositivo') or ''),
                dispositivo_id=str(fila.get('dispositivo_id') or ''),
                duracion=float(fila.get('duracion') or 0),
                resolucion=(int(fila.get('ancho') or 0), int(fila.get('alto') or 0)),
                fps=float(fila.get('fps') or 0),
                codec_video=str(fila.get('codec_video') or ''),
                codec_audio=str(fila.get('codec_audio') or ''),
                bitrate_video_kbps=float(fila.get('bitrate_video_kbps') or 0),
                frame_hashes=[],
                frame_intervalo=0.0,
            ))
        except (TypeError, ValueError):
            continue
    return resultados


def _comparar_rapido_sha256(videos: Sequence[Any]) -> Tuple[List[Any], Dict[str, Any]]:
    """Detecta copias binarias agrupando por SHA256, sin evaluar todos los pares.

    Para un grupo de N archivos, informa N-1 asociaciones con su primer archivo,
    en vez de generar los N*(N-1)/2 pares redundantes. Así cada archivo
    repetido queda identificado y el resultado crece linealmente.
    """
    from collections import defaultdict
    import re
    from core.comparator import ResultadoComparacion, _info_video, _decidir_cual_eliminar

    agrupados: Dict[str, List[Any]] = defaultdict(list)
    omitidos = 0
    for fp in videos:
        sha = str(getattr(fp, 'hash_archivo', '') or '').strip().lower()
        if not re.fullmatch(r'[0-9a-f]{64}', sha):
            omitidos += 1
            continue
        agrupados[sha].append(fp)

    resultados = []
    grupos = 0
    for sha, miembros in agrupados.items():
        if len(miembros) < 2:
            continue
        # No volver a tratar una entrada repetida del mismo dispositivo y ruta
        # como otra copia. Distintos dispositivos sí pueden tener rutas iguales.
        distintos = []
        claves = set()
        for fp in miembros:
            clave = (str(getattr(fp, 'dispositivo_id', '') or ''), str(fp.ruta))
            if clave not in claves:
                claves.add(clave)
                distintos.append(fp)
        if len(distintos) < 2:
            continue
        grupos += 1
        origen = distintos[0]
        for fp in distintos[1:]:
            # La igualdad del hash guardado es evidencia histórica, no rehash
            # del archivo físico. No hacemos ninguna recomendación de borrado.
            info_a, info_b = _info_video(origen), _info_video(fp)
            info_a['dispositivo_id'] = getattr(origen, 'dispositivo_id', '')
            info_b['dispositivo_id'] = getattr(fp, 'dispositivo_id', '')
            resultados.append(ResultadoComparacion(
                tipo='duplicado_exacto',
                ruta_a=origen.ruta, nombre_a=origen.nombre,
                ruta_b=fp.ruta, nombre_b=fp.nombre,
                similitud_visual=1.0, similitud_audio=-1.0,
                confianza=1.0,
                notas=f'SHA256 registrado idéntico: {sha}. Contenido no releído durante esta comparación.',
                info_a=info_a, info_b=info_b,
                recomendacion=_decidir_cual_eliminar(origen, fp),
            ))

    resumen = {
        'metodo': 'solo_sha256',
        'total_videos': len(videos),
        'con_sha256_valido': len(videos) - omitidos,
        'sin_sha256_valido': omitidos,
        'hashes_unicos': len(agrupados),
        'grupos_con_coincidencias': grupos,
        'asociaciones_reportadas': len(resultados),
        'estructura': 'Cada grupo se representa mediante N-1 asociaciones, no todos los pares.',
        'advertencia': 'Se comparan hashes almacenados; no se recalcula SHA256 ni se recomienda eliminar archivos.',
    }
    return resultados, resumen


def cmd_comparar(args: argparse.Namespace) -> int:
    from core.comparator import comparar_todos
    from core.database import _usa_neon, cerrar, conectar, importar_json, obtener_todos, info_dispositivo_actual, describir_conexion
    from core.reporter import BarraProgreso, construir_informe, exportar_csv, exportar_json, exportar_txt, exportar_candidatos_a_eliminar, imprimir_reporte

    if not _validar_umbrales(args):
        return 2

    notificar_inicio('Comparar bases de datos', f'Base principal: {args.db}')

    if args.db != 'neon_cloud' and not Path(args.db).is_file():
        imprimir(f'[red][ERROR] No existe la base de datos: {args.db}[/]')
        return 1

    solo_sha256 = bool(getattr(args, 'solo_sha256', False))
    if solo_sha256:
        # Ruta ligera: no consultar frame_hashes ni abrir vídeos físicos.
        videos = _cargar_registros_para_sha256(args.db, neon=(args.db == 'neon_cloud'))
        for adicional in (args.db_extra or []):
            registros = _cargar_registros_para_sha256(adicional)
            videos.extend(registros)
            imprimir(f'[cyan]{adicional}: {len(registros)} registros cargados (solo metadatos).[/]')
    else:
        es_json_principal = args.db != 'neon_cloud' and Path(args.db).suffix.lower() == '.json'
        conn = conectar(':memory:' if es_json_principal else args.db)
        try:
            if es_json_principal:
                conteo = importar_json(conn, args.db)
                if conteo['errores']:
                    imprimir('[red][ERROR] La base JSON tiene registros inválidos.[/]')
                    return 1
            videos = obtener_todos(conn)
        finally:
            cerrar(conn)

        if args.db_extra:
            for archivo_extra in args.db_extra:
                if not Path(archivo_extra).is_file():
                    imprimir(f'[red][ERROR] No existe: {archivo_extra}[/]')
                    return 1
                es_json = Path(archivo_extra).suffix.lower() == '.json'
                destino_extra = ':memory:' if es_json else archivo_extra
                conn_extra = conectar(destino_extra)
                try:
                    if es_json:
                        conteo = importar_json(conn_extra, archivo_extra, omitir_existentes=True)
                        imprimir(f'[cyan]{archivo_extra}: {conteo["importados"]} importados; {conteo["errores"]} errores[/]')
                        if conteo['errores']:
                            imprimir('[red][ERROR] La base JSON contiene registros inválidos.[/]')
                            return 1
                    adicionales = obtener_todos(conn_extra)
                finally:
                    cerrar(conn_extra)
                videos.extend(adicionales)
                imprimir(f'[cyan]Cargados de {archivo_extra}: {len(adicionales)} vídeos[/]')

        unicos, claves = [], set()
        for fp in videos:
            # Una ruta idéntica de dos equipos diferentes representa dos vídeos.
            try:
                st = Path(fp.ruta).stat()
                clave = ('archivo', fp.dispositivo_id, st.st_dev, st.st_ino) if st.st_ino else ('ruta', fp.dispositivo_id, fp.ruta)
            except (OSError, ValueError):
                clave = ('ruta', fp.dispositivo_id, fp.ruta)
            if clave not in claves:
                claves.add(clave)
                unicos.append(fp)
        videos = unicos

    if len(videos) < 2:
        imprimir('[red]Se necesitan al menos dos vídeos con huella.[/]')
        return 1

    intervalos = {fp.frame_intervalo for fp in videos} if not solo_sha256 else set()
    if len(intervalos) > 1:
        imprimir(f'[yellow][AVISO] Huellas con diferentes intervalos: {sorted(intervalos)}. Solo comparables mediante SHA256 exacto.[/]')

    total_pares = len(videos) * (len(videos) - 1) // 2
    info_disp = info_dispositivo_actual()

    if CONSOLA is not None and Table is not None and rich_box is not None:
        tabla = Table(box=rich_box.ROUNDED, border_style='cyan', show_header=False)
        tabla.add_column(style='cyan'); tabla.add_column(style='white')
        tabla.add_row('Dispositivo', f'{info_disp["dispositivo"]} (ID {info_disp["dispositivo_id"]})')
        tabla.add_row('Modo conexión', describir_conexion())
        tabla.add_row('Vídeos a comparar', str(len(videos)))
        tabla.add_row('Método', 'SHA256 rápido (agrupación)' if solo_sha256 else 'SHA256 + visual + fragmentos')
        tabla.add_row('Pares a evaluar', 'Sin enumerar todos los pares' if solo_sha256 else str(total_pares))
        CONSOLA.print(tabla)
    else:
        print(f'\n Dispositivo actual: {info_disp["dispositivo"]} (ID {info_disp["dispositivo_id"]})')
        print(f' Modo de conexión  : {describir_conexion()}')
        print(f' Vídeos a comparar: {len(videos)}')
        print(f' Método           : {"Solo SHA256 (rápido)" if solo_sha256 else "Completo"}')
        print(f' Pares a evaluar : {"Sin enumerar todos" if solo_sha256 else total_pares}\n')

    resumen_comparativas = {}
    inicio = time.monotonic()
    if solo_sha256:
        resultados, resumen_comparativas = _comparar_rapido_sha256(videos)
        imprimir(f'[cyan]SHA256 válidos: {resumen_comparativas["con_sha256_valido"]:,} | '
                f'Grupos coincidentes: {resumen_comparativas["grupos_con_coincidencias"]:,} | '
                f'Asociaciones: {len(resultados):,}[/]')
    else:
        barra = BarraProgreso(total=total_pares, total_videos=len(videos))
        contador_hallazgos = 0

        # Imprime cada hallazgo como un bloque persistente de varias líneas.
        # La barra se limpia antes del bloque y se vuelve a dibujar debajo,
        # evitando que los mensajes o las rutas largas dejen residuos visuales.
        def manejar_hallazgo_en_vivo(hallazgo):
            nonlocal contador_hallazgos
            contador_hallazgos += 1
            tipo_str = hallazgo.tipo.upper().replace('_', ' ')
            info_a = hallazgo.info_a or {}
            info_b = hallazgo.info_b or {}

            dispositivo_a = info_a.get('dispositivo') or 'No identificado'
            dispositivo_b = info_b.get('dispositivo') or 'No identificado'
            dispositivo_id_a = info_a.get('dispositivo_id') or 'N/D'
            dispositivo_id_b = info_b.get('dispositivo_id') or 'N/D'

            ruta_a = hallazgo.ruta_a or 'N/D'
            ruta_b = hallazgo.ruta_b or 'N/D'

            lineas_rich = [
                f'[bold red]⚡ ¡HALLAZGO {contador_hallazgos} DETECTADO! [{rich_escape(tipo_str)}][/] '
                f'Confianza: [bold gold1]{hallazgo.confianza:.1%}[/]',
                f'   [green]A:[/] [bold]{rich_escape(hallazgo.nombre_a)}[/]',
                f'      [grey70]Ruta:[/] {rich_escape(ruta_a)}',
                f'      [bright_cyan]Dispositivo:[/] {rich_escape(dispositivo_a)} '
                f'[grey70](ID: {rich_escape(dispositivo_id_a)})[/]',
                f'   [yellow]B:[/] [bold]{rich_escape(hallazgo.nombre_b)}[/]',
                f'      [grey70]Ruta:[/] {rich_escape(ruta_b)}',
                f'      [bright_cyan]Dispositivo:[/] {rich_escape(dispositivo_b)} '
                f'[grey70](ID: {rich_escape(dispositivo_id_b)})[/]',
            ]

            lineas_ansi = [
                f'{ROJO}{NEGRITA}⚡ ¡HALLAZGO {contador_hallazgos} DETECTADO! [{tipo_str}]{NC} '
                f'{AMARILLO}{NEGRITA}Confianza: {hallazgo.confianza:.1%}{NC}',
                f'   {VERDE}A:{NC} {NEGRITA}{hallazgo.nombre_a}{NC}',
                f'      {GRIS}Ruta:{NC} {ruta_a}',
                f'      {CIAN}Dispositivo:{NC} {dispositivo_a} (ID: {dispositivo_id_a})',
                f'   {AMARILLO}B:{NC} {NEGRITA}{hallazgo.nombre_b}{NC}',
                f'      {GRIS}Ruta:{NC} {ruta_b}',
                f'      {CIAN}Dispositivo:{NC} {dispositivo_b} (ID: {dispositivo_id_b})',
            ]
            barra.mostrar_mensaje(lineas_rich, lineas_ansi)

        try:
            resultados = comparar_todos(
                videos,
                callback_progreso=barra.actualizar,
                callback_videos=barra.actualizar_videos,
                callback_hallazgo=manejar_hallazgo_en_vivo,
                resumen=resumen_comparativas,
                umbral_phash=args.umbral_phash,
                umbral_visual=args.umbral_visual / 100,
                umbral_fragmento=args.umbral_fragmento / 100,
                umbral_posible_fragmento=args.umbral_posible_fragmento / 100,
                margen_dur=args.margen_duracion / 100,
                ratio_min=args.ratio_min / 100,
            )
        finally:
            barra.limpiar()

    duracion = time.monotonic() - inicio
    carpeta = _carpeta_reportes() / 'comparativas'
    for sub in ('json', 'csv', 'txt'):
        (carpeta / sub).mkdir(parents=True, exist_ok=True)
    
    identificador = _id_utc6()
    base_nombre = f'{"comparacion_sha256" if solo_sha256 else "comparacion"}_{identificador}'

    informe = construir_informe(resultados, len(videos), duracion, metadatos={
        'bases_de_datos': [args.db] + list(args.db_extra or []),
        # El reporte tradicional exige métricas de cada par, que no se aplican
        # al modo hash. Conservar aquí sus resultados en una clave propia.
        'comparativas': {} if solo_sha256 else resumen_comparativas,
        'resumen_sha256': resumen_comparativas if solo_sha256 else {},
        'modo_comparacion': 'solo_sha256' if solo_sha256 else 'completa',
        'umbrales': {k: getattr(args, k) for k in (
            'umbral_phash', 'umbral_visual', 'umbral_fragmento',
            'umbral_posible_fragmento', 'margen_duracion', 'ratio_min')},
        'dispositivo': info_disp,
        'conexion': describir_conexion(),
    })

    ruta_json = carpeta / 'json' / f'{base_nombre}.json'
    ruta_csv = carpeta / 'csv' / f'{base_nombre}.csv'
    ruta_txt = carpeta / 'txt' / f'{base_nombre}.txt'

    exportar_txt(informe, str(ruta_txt))
    exportar_json(resultados, str(ruta_json), informe=informe)
    exportar_csv(resultados, str(ruta_csv), informe=informe)
    exportar_candidatos_a_eliminar(informe, carpeta, identificador)
    args.ultimo_reporte = str(ruta_json)
    imprimir(f'[spring_green2]Reportes de comparación y candidatos a eliminar guardados en: {carpeta}[/]')

    notificar_fin('Comparar bases de datos', True,
                  f'{len(resultados)} hallazgos en {_fmt_seg(duracion)}')
    return 0


def opciones_comparacion() -> dict:
    valores = dict(umbral_phash=10, umbral_visual=80.0, umbral_fragmento=75.0,
                   umbral_posible_fragmento=25.0, margen_duracion=5.0, ratio_min=5.0)
    if pedir_si_no('¿Personalizar los umbrales de comparación?', False):
        valores['umbral_phash'] = pedir_numero('Distancia máxima pHash', 10, 0, 64, entero=True)
        for clave, titulo in [
            ('umbral_visual', 'Similitud visual mínima (%)'),
            ('umbral_fragmento', 'Similitud de fragmento mínima (%)'),
            ('umbral_posible_fragmento', 'Similitud de posible fragmento mínima (%)'),
            ('margen_duracion', 'Margen de duración (%)'),
            ('ratio_min', 'Proporción mínima de duración del fragmento (%)'),
        ]:
            limite = valores['umbral_fragmento'] if clave == 'umbral_posible_fragmento' else 100
            valores[clave] = pedir_numero(titulo, min(valores[clave], limite), 0, limite)
    return valores


def comparar_interactivo(db_defecto: str) -> str:
    from core.database import _usa_neon
    
    while True:
        _cabecera_submenu('Comparar bases de datos')
        from core.interfaz import menu_rich
        menu_rich('Modo de comparación', [
            ('1', 'Comparar un catálogo o base SQLite'),
            ('2', 'Comparar dos o más bases de datos'),
            ('00', 'Volver al menú principal'),
        ], subtitulo='Los vídeos originales no se modificarán.')
        
        opcion = _pedir_opcion('¿Qué quieres hacer? (1-2 / 00)', ('1', '2', '00', '0'), '1')
        if opcion in ('00', '0'):
            return db_defecto
            
        db = elegir_archivo_reciente(
            'Base principal SQLite o JSON de huellas',
            ['*.db', '*.json'],
            ['respaldos/db', 'exportaciones/json'],
            defecto=db_defecto,
        )
        
        extras: List[str] = []
        if opcion == '2':
            while True:
                extra = pedir_ruta('Base adicional (.db o .json)', existente=True)
                if extra not in [db] + extras:
                    extras.append(extra)
                else:
                    imprimir('[yellow]Esa base ya está seleccionada.[/]')
                if not pedir_si_no('¿Agregar otra base?', False):
                    break
                    
        menu_rich('Método de comparación', [
            ('1', 'Completa · SHA256, visual y fragmentos'),
            ('2', 'Rápida · SHA256 para duplicados exactos'),
        ])
        metodo = _pedir_opcion('Método (1-2; 0 volver; 00 inicio)', ('1','2','0','00'), '2')
        if metodo in ('0','00'):
            return db

        solo_sha256 = (metodo == '2')
        parametros = (dict(umbral_phash=10, umbral_visual=80.0, umbral_fragmento=75.0,
                           umbral_posible_fragmento=25.0, margen_duracion=5.0, ratio_min=5.0)
                      if solo_sha256 else opciones_comparacion())
                      
        args = argparse.Namespace(db=db, db_extra=extras, solo_sha256=solo_sha256, **parametros)
        cmd_comparar(args)
        return db
