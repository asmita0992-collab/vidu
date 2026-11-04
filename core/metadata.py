"""Metadatos ligeros de medios mediante ffprobe: no decodifica fotogramas."""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path


def _numero(valor, defecto=0.0):
    try:
        numero = float(valor)
        return numero if math.isfinite(numero) and numero > 0 else defecto
    except (ValueError, TypeError):
        return defecto


def extraer_metadatos(ruta: Path) -> dict:
    """Extrae datos sin exigir ffprobe: campos desconocidos quedan en 0 / ''."""
    datos = dict(duracion=0.0, ancho=0, alto=0, fps=0.0,
                 codec_video='', codec_audio='', bitrate_video_kbps=0.0,
                 bitrate_kbps=0.0)
    programa = shutil.which('ffprobe')
    if not programa:
        return datos
    try:
        salida = subprocess.run(
            [programa, '-v', 'error', '-print_format', 'json', '-show_streams',
             '-show_format', str(ruta)], capture_output=True, text=True,
            errors='replace', timeout=40, check=False)
        if salida.returncode != 0 or not salida.stdout.strip():
            return datos
        info = json.loads(salida.stdout)
        pistas = info.get('streams') or []
        video = next((p for p in pistas if p.get('codec_type') == 'video'), {})
        audio = next((p for p in pistas if p.get('codec_type') == 'audio'), {})
        fmt = info.get('format') or {}
        datos.update(
            duracion=_numero(fmt.get('duration') or video.get('duration')),
            ancho=int(_numero(video.get('width'))),
            alto=int(_numero(video.get('height'))),
            codec_video=video.get('codec_name') or '',
            codec_audio=audio.get('codec_name') or '',
            bitrate_video_kbps=_numero(video.get('bit_rate')) / 1000,
            bitrate_kbps=_numero(fmt.get('bit_rate')) / 1000,
        )
        fps = str(video.get('avg_frame_rate') or video.get('r_frame_rate') or '')
        if '/' in fps:
            num, den = fps.split('/', 1)
            datos['fps'] = _numero(num) / _numero(den, 1.0) if _numero(den) else 0.0
        else:
            datos['fps'] = _numero(fps)
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        pass
    return datos
