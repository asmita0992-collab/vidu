# -*- coding: utf-8 -*-
"""Índice invertido persistente de pHash para preselección de comparaciones.

No sustituye ``comparar_par``. El filtro con 4 bandas de 16 bits y radio 2
recupera al menos una banda para cualquier par de hashes de Hamming <= 11.
Dos coincidencias ordenadas siguen siendo una *heurística*: para máxima
sensibilidad usar el comparador exhaustivo anterior.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
import itertools
import sqlite3

VERSION_INDICE = 1
FINGERPRINT_VERSION = 'phash64_v1'
MINIMO_HUELLAS_CANDIDATO = 2
MINIMO_RACHA_CONSECUTIVA = 2


def tiempo_huella(posicion: int, total: int, intervalo: float, duracion: float) -> tuple[float, float]:
    """Límites semánticos del intervalo; el último termina con el vídeo."""
    if posicion < 0 or total < 1 or posicion >= total or intervalo <= 0:
        raise ValueError('Índice, número de huellas o intervalo inválido')
    inicio = float(posicion) * float(intervalo)
    if duracion > 0:
        inicio = min(inicio, float(duracion))
    if posicion == total - 1 and duracion > inicio:
        fin = float(duracion)
    else:
        fin = inicio + float(intervalo)
        if duracion > 0:
            fin = min(fin, float(duracion))
    return inicio, max(inicio, fin)


def clave_video(fp: Any) -> str:
    return str(getattr(fp, 'dispositivo_id', '') or '') + '|' + str(fp.ruta)


def es_phash64(hash_valor: str) -> bool:
    return isinstance(hash_valor, str) and len(hash_valor) == 16 and all(c in '0123456789abcdefABCDEF' for c in hash_valor)


def _firmar(fp: Any) -> str:
    h = sha256()
    for fragmento in (clave_video(fp), str(fp.duracion), str(fp.frame_intervalo),
                      str(fp.hash_archivo or ''), getattr(fp,'fingerprint_version',FINGERPRINT_VERSION), *fp.frame_hashes):
        h.update(str(fragmento).encode('utf-8'))
        h.update(b'\0')
    return h.hexdigest()


@lru_cache(maxsize=4096)
def _vecinos16(valor: int, radio: int = 2) -> tuple[int, ...]:
    numeros = {valor}
    for k in range(1, radio + 1):
        for bits in itertools.combinations(range(16), k):
            cambio = 0
            for bit in bits:
                cambio |= 1 << bit
            numeros.add(valor ^ cambio)
    return tuple(sorted(numeros))


def _bandas(huella: str) -> tuple[int, int, int, int]:
    numero = int(huella, 16)
    return tuple((numero >> (i * 16)) & 0xffff for i in range(4))


class IndiceInvertido:
    """Índice externo: nunca altera ni se conecta a Neon ni a la SQLite origen."""

    def __init__(self, ruta: str | Path):
        self.ruta = Path(ruta).expanduser().resolve()
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.ruta), timeout=45)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.execute('PRAGMA busy_timeout=45000')
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS meta (clave TEXT PRIMARY KEY, valor TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS videos_indice (
            id INTEGER PRIMARY KEY, clave TEXT NOT NULL UNIQUE,
            firma TEXT NOT NULL, duracion REAL NOT NULL, intervalo REAL NOT NULL,
            fingerprint_version TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS apariciones (
            video_id INTEGER NOT NULL REFERENCES videos_indice(id) ON DELETE CASCADE,
            indice_huella INTEGER NOT NULL, hash_valor TEXT NOT NULL,
            tiempo_inicio REAL NOT NULL, tiempo_fin REAL NOT NULL,
            fingerprint_version TEXT NOT NULL,
            PRIMARY KEY (video_id, indice_huella));
        CREATE INDEX IF NOT EXISTS idx_apariciones_hash ON apariciones(hash_valor);
        CREATE TABLE IF NOT EXISTS bandas (
            video_id INTEGER NOT NULL REFERENCES videos_indice(id) ON DELETE CASCADE,
            indice_huella INTEGER NOT NULL, banda INTEGER NOT NULL, valor INTEGER NOT NULL,
            PRIMARY KEY(video_id, indice_huella, banda));
        CREATE INDEX IF NOT EXISTS idx_bandas_buscar ON bandas(banda,valor,video_id);
        ''')
        version = self.conn.execute("SELECT valor FROM meta WHERE clave='version'").fetchone()
        if version and version[0] != str(VERSION_INDICE):
            self.close()
            raise ValueError('La versión del índice es incompatible: reconstruye el índice separado.')
        self.conn.execute('INSERT OR REPLACE INTO meta(clave,valor) VALUES (?,?)', ('version', str(VERSION_INDICE)))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, typ, val, tb):
        self.close()

    def actualizar(self, videos: list[Any], progreso: Optional[Callable[[int, int], None]] = None) -> dict:
        """Incremental: inserta, actualiza y elimina huellas obsoletas de este índice."""
        vistos = set()
        nuevos = actualizados = intactos = 0
        try:
            for i, fp in enumerate(videos, 1):
                clave = clave_video(fp)
                if clave in vistos:
                    raise ValueError(f'Identidad duplicada entre bases: {clave}')
                vistos.add(clave)
                firma = _firmar(fp)
                existente = self.conn.execute('SELECT id,firma FROM videos_indice WHERE clave=?', (clave,)).fetchone()
                if existente and existente['firma'] == firma:
                    intactos += 1
                else:
                    if existente:
                        vid = int(existente['id'])
                        self.conn.execute('DELETE FROM bandas WHERE video_id=?', (vid,))
                        self.conn.execute('DELETE FROM apariciones WHERE video_id=?', (vid,))
                        self.conn.execute('UPDATE videos_indice SET firma=?,duracion=?,intervalo=?,fingerprint_version=? WHERE id=?',
                                          (firma, fp.duracion, fp.frame_intervalo,
                                           getattr(fp,'fingerprint_version',FINGERPRINT_VERSION), vid))
                        actualizados += 1
                    else:
                        vid = self.conn.execute(
                            'INSERT INTO videos_indice(clave,firma,duracion,intervalo,fingerprint_version) VALUES (?,?,?,?,?)',
                            (clave, firma, fp.duracion, fp.frame_intervalo,
                             getattr(fp,'fingerprint_version',FINGERPRINT_VERSION))).lastrowid
                        nuevos += 1
                    valores = []
                    bandas = []
                    for j, h in enumerate(fp.frame_hashes):
                        if not es_phash64(h):
                            continue
                        t0, t1 = tiempo_huella(j, len(fp.frame_hashes), float(fp.frame_intervalo), float(fp.duracion))
                        valores.append((vid, j, h.lower(), t0, t1, getattr(fp, 'fingerprint_version', FINGERPRINT_VERSION)))
                        bandas.extend((vid, j, n, valor) for n, valor in enumerate(_bandas(h)))
                    self.conn.executemany('INSERT INTO apariciones VALUES (?,?,?,?,?,?)', valores)
                    self.conn.executemany('INSERT INTO bandas VALUES (?,?,?,?)', bandas)
                if progreso and (i % 100 == 0 or i == len(videos)):
                    progreso(i, len(videos))
                if i % 100 == 0:
                    self.conn.commit()
            obsoletos = [r[0] for r in self.conn.execute('SELECT clave FROM videos_indice') if r[0] not in vistos]
            self.conn.executemany('DELETE FROM videos_indice WHERE clave=?', ((v,) for v in obsoletos))
            self.conn.commit()
            return {'nuevos': nuevos, 'actualizados': actualizados,
                    'sin_cambios': intactos, 'retirados': len(obsoletos)}
        except Exception:
            self.conn.rollback()
            raise

    def identificadores(self) -> dict[str, int]:
        return {r['clave']: int(r['id']) for r in self.conn.execute('SELECT id,clave FROM videos_indice')}

    def apariciones_de(self, hash_valor: str) -> list[dict]:
        if not es_phash64(hash_valor):
            return []
        return [dict(r) for r in self.conn.execute('''
            SELECT i.clave AS video, a.indice_huella,a.tiempo_inicio,a.tiempo_fin
            FROM apariciones a JOIN videos_indice i ON i.id=a.video_id
            WHERE a.hash_valor=?''', (hash_valor.lower(),))]

    def coincidencias(self, video_id: int, fp: Any, *, umbral: int = 10,
                      modo: str = 'hamming') -> dict[int, set[tuple[int, int]]]:
        """Devuelve coincidencias reales Hamming; la DB filtra por 4 bandas.

        Modo 'exacto' solo busca hashes hexadecimales iguales y puede omitir
        resultados visuales válidos. Modo 'hamming' cubre <=11 en 64 bits.
        """
        if modo not in ('exacto', 'hamming'):
            raise ValueError('Modo de búsqueda no admitido.')
        if modo == 'hamming' and not (0 <= umbral <= 11):
            raise ValueError('El índice de 4 bandas solo cubre Hamming hasta 11; utiliza comparación exhaustiva.')
        aciertos: dict[int, set[tuple[int, int]]] = defaultdict(set)
        for idx, h in enumerate(fp.frame_hashes):
            if not es_phash64(h):
                continue
            x = int(h, 16)
            if modo == 'exacto':
                filas = self.conn.execute('''SELECT video_id,indice_huella,hash_valor FROM apariciones
                                              WHERE hash_valor=?''', (h.lower(),))
                for r in filas:
                    aciertos[int(r['video_id'])].add((idx, int(r['indice_huella'])))
                continue
            # Distancia <=10 en 64 bits => al dividir en 4 bandas de 16
            # existe al menos una banda con distancia <=2 (principio del palomar).
            for num_banda, valor in enumerate(_bandas(h)):
                vecinos = _vecinos16(valor)
                marcas = ','.join('?' for _ in vecinos)
                filas = self.conn.execute(f'''SELECT b.video_id,b.indice_huella,a.hash_valor
                    FROM bandas b JOIN apariciones a ON a.video_id=b.video_id
                         AND a.indice_huella=b.indice_huella
                    WHERE b.banda=? AND b.valor IN ({marcas})''',
                    (num_banda, *vecinos))
                for r in filas:
                    if (x ^ int(r['hash_valor'], 16)).bit_count() <= umbral:
                        aciertos[int(r['video_id'])].add((idx, int(r['indice_huella'])))
        return aciertos


def racha_coherente(pares: set[tuple[int, int]], minimo: int = MINIMO_RACHA_CONSECUTIVA,
                     tolerancia_salto: int = 1) -> bool:
    """Agrupa desfases estables; permite una muestra intermedia ausente."""
    if len(pares) < minimo:
        return False
    grupos: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for a, b in pares:
        grupos[b - a].append((a, b))
    for alineados in grupos.values():
        orden = sorted(alineados)
        consecutivos = 1
        for (prev_a, prev_b), (a, b) in zip(orden, orden[1:]):
            if 0 < a - prev_a <= 1 + tolerancia_salto and 0 < b - prev_b <= 1 + tolerancia_salto:
                consecutivos += 1
                if consecutivos >= minimo:
                    return True
            else:
                consecutivos = 1
    return False


def comparar_con_indice(
    videos: list[Any], *, ruta_indice: str | Path, modo: str = 'hamming',
    minimo_huellas: int = MINIMO_HUELLAS_CANDIDATO,
    minimo_racha: int = MINIMO_RACHA_CONSECUTIVA,
    callback_progreso=None, callback_videos=None, callback_hallazgo=None,
    resumen=None, **kwargs,
):
    """Preselecciona y delega SIEMPRE el resultado final a ``comparar_par``.

    Los mínimos de dos coincidencias constituyen un filtro heurístico, por tanto
    no existe equivalencia plena con el modo exhaustivo, que sigue disponible.
    """
    from .comparator import comparar_par
    if minimo_huellas < 2 or minimo_racha < 2:
        raise ValueError('Los mínimos del índice deben ser al menos dos.')
    umbral = int(kwargs.get('umbral_phash', 10))
    if modo == 'hamming' and umbral > 11:
        raise ValueError('Para umbral pHash mayor de 11 elige comparación exhaustiva.')
    total = len(videos) * (len(videos) - 1) // 2
    indices = {clave_video(fp): i for i, fp in enumerate(videos)}
    sha_index: dict[str, list[int]] = defaultdict(list)
    for idx, fp in enumerate(videos):
        if fp.hash_archivo:
            sha_index[str(fp.hash_archivo)].append(idx)
    pareselegidos: set[tuple[int, int]] = set()
    with IndiceInvertido(ruta_indice) as db:
        estado = db.actualizar(videos)
        mapa = db.identificadores()
        inverso = {id_: indices[clave] for clave, id_ in mapa.items() if clave in indices}
        for idx, fp in enumerate(videos):
            # Primero exactos SHA256, incluso si aún faltan huellas visuales.
            if fp.hash_archivo:
                pareselegidos.update((idx, j) for j in sha_index[str(fp.hash_archivo)] if j > idx)
            if not fp.frame_hashes or fp.frame_intervalo <= 0:
                continue
            vid = mapa[clave_video(fp)]
            matches = db.coincidencias(vid, fp, umbral=umbral, modo=modo)
            for otro_id, alineadas in matches.items():
                j = inverso.get(otro_id)
                if j is None or j <= idx:
                    continue
                otro = videos[j]
                if (not otro.frame_hashes or abs(fp.frame_intervalo - otro.frame_intervalo) > 1e-6
                        or getattr(fp,'fingerprint_version',FINGERPRINT_VERSION) !=
                           getattr(otro,'fingerprint_version',FINGERPRINT_VERSION)):
                    continue
                if len(alineadas) >= minimo_huellas and racha_coherente(alineadas, minimo_racha):
                    pareselegidos.add((idx, j))
            if callback_videos:
                callback_videos(idx + 1, len(videos))
    elegidos = sorted(pareselegidos)
    resultados = []
    conteos = [0] * len(videos)
    for n, (i, j) in enumerate(elegidos, 1):
        fp1, fp2 = videos[i], videos[j]
        if callback_progreso:
            callback_progreso(n-1, len(elegidos), fp1.nombre, fp2.nombre)
        hallazgo = comparar_par(fp1, fp2, **kwargs)
        conteos[i] += 1
        conteos[j] += 1
        if hallazgo is not None:
            resultados.append(hallazgo)
            if callback_hallazgo:
                callback_hallazgo(hallazgo)
        if callback_progreso:
            callback_progreso(n, len(elegidos), fp1.nombre, fp2.nombre)
    resultados.sort(key=lambda r: r.confianza, reverse=True)
    if resumen is not None:
        resumen.update(total_videos=len(videos), total_pares=total,
                       pares_evaluados=len(elegidos), pares_preseleccionados=len(elegidos),
                       pares_descartados=total-len(elegidos), modo_indice=modo,
                       indice_estado=estado, cobertura_completa=(len(elegidos) == total),
                       filtro_heuristico=True, huellas_minimas=minimo_huellas,
                       racha_minima=minimo_racha,
                       por_video=[{'ruta': fp.ruta, 'nombre': fp.nombre,
                                   'pares_evaluados': conteos[i]} for i, fp in enumerate(videos)])
    return resultados
