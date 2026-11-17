#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sube explícitamente resultados de kDrive a Neon, sin activar sync general."""
import argparse
import json
from pathlib import Path
from .kdrive_neon import planificar_publicacion, aplicar_publicacion, FRASE

def main():
    p=argparse.ArgumentParser()
    sp=p.add_subparsers(dest='accion',required=True)
    a=sp.add_parser('plan')
    a.add_argument('--db',required=True,type=Path)
    b=sp.add_parser('aplicar')
    b.add_argument('--plan',required=True,type=Path)
    args=p.parse_args()
    if args.accion=='plan':
        print(json.dumps(planificar_publicacion(args.db),ensure_ascii=False,indent=2))
    else:
        frase=input(f'Para publicar, escribe {FRASE}: ').strip()
        print(json.dumps(aplicar_publicacion(args.plan,confirmacion=frase),ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
