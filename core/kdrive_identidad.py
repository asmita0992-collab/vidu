# -*- coding: utf-8 -*-
"""Identidad lógica estable de kDrive, independiente de Windows/Linux/Vast.ai."""
import hashlib
NOMBRE_DISPOSITIVO = 'kDrive (nube)|Nube|rclone'
ID_DISPOSITIVO = hashlib.sha256(b'vidup:kdrive:nube:catalogo:v1').hexdigest()[:13]
PREFIJO_RUTA = 'kdrive:/Definitivos'
MARCADOR_BD = 'kdrive_aislado_sin_neon'
