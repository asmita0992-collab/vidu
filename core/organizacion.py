# -*- coding: utf-8 -*-
"""Rutas funcionales del menú 2.6.0, sin reimplementar operaciones antiguas.

Se agrupan comparación, movimiento/ubicación, depuración y conciliación. Las
operaciones siguen en sus módulos de origen para evitar migraciones de datos.
Una salida 0 vuelve a esta sección y 00 salta al menú principal.
"""
from __future__ import annotations

import json
from pathlib import Path

from core.interfaz import menu_rich
from core.presentacion import (imprimir, pedir_texto, pedir_si_no,
                               IrAlMenuPrincipal, RegresarAlMenu, rich_escape)
from core.almacenamiento import carpeta_reportes, elegir_archivo_reciente


def _eleccion(titulo: str, opciones: list[tuple[str,str]]) -> str:
    menu_rich(titulo, opciones + [('0','Volver al menú anterior'),
                                ('00','Regresar al menú principal')])
    permitidas = {n for n,_ in opciones} | {'0','00'}
    while True:
        valor = pedir_texto('Selecciona una opción','0',permitir_escape=False).strip().lower()
        if valor in permitidas:
            if valor == '00':
                raise IrAlMenuPrincipal()
            return valor
        imprimir('[yellow]Selecciona una de las opciones mostradas.[/]')


def _sha(db: str, permitidas: tuple[str,...]) -> str:
    from core.sha256 import menu
    from core.flujo_escaneo import _tras_menu_sha256
    salida = menu(opciones_permitidas=permitidas, db_defecto=db, anidado=True)
    return _tras_menu_sha256(salida, db)


def submenu_comparacion(db: str) -> str:
    """Agrupa el comparador audiovisual y los dos tipos de cotejo SHA256."""
    from core.flujo_comparacion import comparar_interactivo
    while True:
        op = _eleccion('Comparación de vídeos y bases', [
            ('1','Comparación audiovisual y búsqueda de duplicados'),
            ('2','Comparar carpeta con bases de datos · SHA256'),
            ('3','Comparar catálogos JSON SHA256 fuera de línea'),
        ])
        if op == '0':
            return db
        if op == '1':
            nuevo = comparar_interactivo(db)
            db = nuevo or db
        elif op == '2':
            db = _sha(db, ('1',))
        elif op == '3':
            db = _sha(db, ('3',))


def submenu_archivos(db: str) -> str:
    """Movimientos, búsquedas, verificación y actualización de rutas."""
    from core.menus import submenu_mover, submenu_verificacion
    from core.utilidades_interactivas import cambiar_rutas_interactivo
    while True:
        op = _eleccion('Gestión y ubicación de archivos', [
            ('1','Mover vídeos, duplicados y carpeta de eliminación'),
            ('2','Comprobar disco, buscar faltantes y aplicar reubicaciones'),
            ('3','Cambiar rutas manualmente en una SQLite'),
            ('4','kDrive (nube) · enviar duplicados verificados a la papelera'),
        ])
        if op == '0':
            return db
        if op == '1':
            db = submenu_mover(db, anidado=True)
        elif op == '2':
            db = submenu_verificacion(db, anidado=True)
        elif op == '3':
            from core.almacenamiento import elegir_archivo_reciente
            origen = elegir_archivo_reciente('SQLite para el cambio de rutas',
                     ['*.db'], ['respaldos/db'], defecto=db)
            cambiar_rutas_interactivo(origen)
            db = origen
        elif op == '4':
            from core.kdrive_remoto import papelera_interactiva
            papelera_interactiva()


def submenu_depuracion(db: str) -> str:
    """Depuración por registros e importación explícita de huellas mínimas."""
    from core.menus import submenu_depurar
    while True:
        op = _eleccion('Depuración de bases y huellas', [
            ('1','Depuraciones por ruta, dispositivo o carpeta'),
            ('2','Importar catálogos SHA256 offline a SQLite'),
        ])
        if op == '0':
            return db
        if op == '1':
            db = submenu_depurar(db, anidado=True)
        elif op == '2':
            db = _sha(db, ('4',))


def _seleccionar_json_plan() -> Path:
    ruta = elegir_archivo_reciente('Plan de sincronización JSON guardado',
         ['plan_*.json'], ['sincronizacion/json'])
    seleccionado = Path(ruta).resolve()
    if not seleccionado.is_file() or not seleccionado.name.startswith('plan_'):
        raise ValueError('Debes seleccionar un plan JSON producido por VIDUP.')
    return seleccionado


def _mostrar_procedencia(db: str) -> None:
    """Consulta metadatos locales sin asignar automáticamente autoridad."""
    from core.database import conectar_sqlite, cerrar
    from core.control_sincronizacion import estado_generacion
    conexion = conectar_sqlite(db)
    try:
        generacion = estado_generacion(conexion)
        def estado(clave):
            fila=conexion.execute('SELECT valor FROM vidup_sync_estado WHERE clave=?',(clave,)).fetchone()
            return fila[0] if fila else None
        imprimir(f'[cyan]SQLite:[/] {rich_escape(db)}')
        imprimir(f'[cyan]Referencia reconocida:[/] {rich_escape(generacion or "No verificada")}')
        imprimir(f'[cyan]Origen del respaldo:[/] {rich_escape(estado("respaldo_origen") or "No declarado")}')
        imprimir(f'[cyan]Fecha del respaldo:[/] {rich_escape(estado("respaldo_fecha") or "No declarada")}')
        pendientes = conexion.execute('SELECT COUNT(*) FROM vidup_cambios_pendientes').fetchone()[0]
        imprimir(f'[gold1]Cambios locales pendientes:[/] {pendientes}')
    finally:
        cerrar(conexion)
    imprimir('[yellow]Este diagnóstico no concede autoridad ni modifica datos de Neon.[/]')


def submenu_sincronizacion(db: str) -> str:
    """Separa diagnóstico, consulta y ejecución; protege planes caducados.

    Un plan anterior solo se ejecuta cuando su firma criptográfica del catálogo
    y la referencia de generación siguen siendo idénticas.
    """
    from core.sincronizacion import sincronizar_local_y_nube
    while True:
        op = _eleccion('Sincronización y conciliación con Neon', [
            ('1','Sincronizar SQLite local con Neon · proponer operaciones'),
            ('2','Analizar diferencias y guardar plan verificable'),
            ('3','Consultar TXT / JSON de un plan existente'),
            ('4','Ejecutar un plan guardado, previa revalidación estricta'),
            ('5','Reconciliar eliminaciones verificadas · plan y revisión'),
            ('6','Bandeja de análisis externos pendientes'),
            ('7','Consultar generación y procedencia de SQLite'),
            ('8','Publicar explícitamente SQLite de kDrive en Neon'),
        ])
        if op == '0':
            return db
        try:
            if op == '2':
                sincronizar_local_y_nube(ruta_local=db, solo_plan=True,
                                         confirmar=False, permitir_eliminaciones=True)
            elif op == '3':
                ruta = _seleccionar_json_plan()
                datos = json.loads(ruta.read_text(encoding='utf-8'))
                if not datos.get('firma_catalogos'):
                    imprimir('[yellow]Plan antiguo: no admite reproducción segura; úsalo solo para consulta.[/]')
                imprimir('[turquoise2]Fecha:[/] ' + rich_escape(str(datos.get('fecha','desconocida'))))
                imprimir('[turquoise2]SQLite:[/] ' + rich_escape(str(datos.get('sqlite','desconocida'))))
                for campo in ('subir','bajar','videos_borrar_en_neon','videos_borrar_en_sqlite',
                              'conflictos_no_resueltos'):
                    imprimir(f'[cyan]{campo}:[/] {len(datos.get(campo,[]))}')
                texto = ruta.parent.parent/'txt'/(ruta.stem+'.txt')
                if texto.is_file():
                    imprimir('[cyan]Informe legible:[/] ' + rich_escape(str(texto)))
                    if pedir_si_no('¿Mostrar el TXT ahora?',False):
                        imprimir(texto.read_text(encoding='utf-8'), markup=False)
            elif op in ('1','4','5'):
                plan = str(_seleccionar_json_plan()) if op == '4' else None
                if op == '5':
                    imprimir('[yellow]Las bajas requieren reporte y diario de cambios propios; '
                            'no basta con que el registro falte de una base.[/]')
                if plan and not pedir_si_no('¿Ejecutar si el plan conserva exactamente el mismo estado?',False):
                    continue
                permitir = pedir_si_no('¿Permitir proponer bajas en los catálogos (NO archivos físicos)?',False)
                sincronizar_local_y_nube(ruta_local=db, confirmar=True,
                                         permitir_eliminaciones=permitir,
                                         archivo_plan_existente=plan)
            elif op == '6':
                from core.escaneo_externo import menu_bandeja_externa
                menu_bandeja_externa(db)
            elif op == '7':
                _mostrar_procedencia(db)
            elif op == '8':
                from core.kdrive_neon import publicar_interactivo
                publicar_interactivo()
        except IrAlMenuPrincipal:
            raise
        except (KeyboardInterrupt, EOFError):
            imprimir('[yellow]Operación interrumpida. Ningún éxito se presupone.[/]')
        except Exception as exc:
            imprimir('[red]Operación no aplicada/completada:[/] '+rich_escape(str(exc)))
