#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Utilitarios de zona horaria para Ciudad de México (UTC-6)."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


# ============================================================================
# Zona horaria fija UTC-6 Ciudad de México
# ============================================================================
ZONA_MEXICO = timezone(timedelta(hours=-6))


def ahora_utc6() -> datetime:
    """Retorna la hora actual en zona horaria de México (UTC-6)."""
    return datetime.now(ZONA_MEXICO)


def fmt_ahora_utc6() -> str:
    """Retorna la hora actual formateada: 'YYYY-MM-DD HH:MM:SS'."""
    return ahora_utc6().strftime("%Y-%m-%d %H:%M:%S")


def id_utc6() -> str:
    """Retorna identificador de hora actual: 'YYYYMMDDhhmms'."""
    return ahora_utc6().strftime("%Y%m%d_%H%M%S")
