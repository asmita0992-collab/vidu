"""Módulo para sincronizar únicamente archivos Python (.py) desde la carpeta de trabajo hacia el respaldo secundario (E:\\Haggy\\VIDUP)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Tuple, List

DIR_ORIGEN_DEFECTO: Path = Path(__file__).resolve().parent.parent
DIR_DESTINO_DEFECTO: Path = Path(r"E:\Haggy\VIDUP")


def sincronizar_archivos_python(
    origen: Path | None = None,
    destino: Path | None = None,
) -> Tuple[int, List[str]]:
    """Copia los archivos .py desde el origen al destino si el destino existe.

    Ignora cualquier archivo que no termine en .py (como .env, .db, reportes, etc.).
    Retorna la cantidad de archivos copiados y la lista de rutas relativas actualizadas.
    """
    ruta_origen = (origen or DIR_ORIGEN_DEFECTO).resolve()
    ruta_destino = (destino or DIR_DESTINO_DEFECTO).resolve()

    if not ruta_destino.is_dir():
        return 0, []

    actualizados: List[str] = []
    copiados = 0

    for archivo_py in ruta_origen.rglob("*.py"):
        if not archivo_py.is_file():
            continue

        # Calcular ruta relativa respecto al origen
        try:
            relativa = archivo_py.relative_to(ruta_origen)
        except ValueError:
            continue

        # Evitar carpetas ocultas o de entorno virtual/cache si fuera necesario
        partes = relativa.parts
        if any(part.startswith(".") or part in ("__pycache__", "build", "dist") for part in partes):
            continue

        destino_archivo = ruta_destino / relativa

        # Verificar si el archivo requiere actualización (no existe o difiere en contenido/tamaño)
        requiere_copia = False
        if not destino_archivo.is_file():
            requiere_copia = True
        else:
            try:
                if archivo_py.stat().st_size != destino_archivo.stat().st_size:
                    requiere_copia = True
                else:
                    with archivo_py.open("rb") as f1, destino_archivo.open("rb") as f2:
                        if f1.read() != f2.read():
                            requiere_copia = True
            except OSError:
                requiere_copia = True

        if requiere_copia:
            try:
                destino_archivo.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(archivo_py, destino_archivo)
                copiados += 1
                actualizados.append(str(relativa))
            except OSError:
                pass

    return copiados, actualizados
