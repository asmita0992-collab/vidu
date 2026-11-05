# vidup/core/__init__.py

"""
Paquete core de VIDUP.

Contiene los módulos internos del detector de vídeos duplicados y fragmentos:

  - fingerprint:  Extracción de huellas (SHA256, pHash, audio) con FFmpeg.
  - comparator:   Comparación de huellas (duplicados exactos/visuales/fragmentos).
  - database:     Almacenamiento en Neon PostgreSQL (nube) o SQLite local.
  - reporter:     Reportes en terminal y exportación a JSON/TXT/CSV.
  - mover:        Movimiento verificable de vídeos con preservación de fechas.
  - recursos:     Medición de CPU/RAM/disco por vídeo durante el escaneo.
  - sha256:       Comparación y generación de SHA256 de carpetas MP4.

Los módulos se importan explícitamente desde vidup.py y no exponen
una API pública unificada; cada uno gestiona su propio dominio.
"""

__version__ = "2.2.3"
