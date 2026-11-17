#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI para preparar/aplicar la papelera kDrive desde el VPS de OVH."""
import argparse
import json
from pathlib import Path
from .kdrive_remoto import generar_plan_papelera, aplicar_plan_papelera, FRASE_CONFIRMACION

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='operacion', required=True)
    p = sub.add_parser('plan', help='Genera plan SIN tocar la nube')
    p.add_argument('--reporte', type=Path, required=True)
    p.add_argument('--db', type=Path, required=True)
    p.add_argument('--incluir-posibles', action='store_true')
    p.add_argument('--confianza', type=float, default=0.85)
    a = sub.add_parser('aplicar', help='Envía solo archivos verificados a la papelera')
    a.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.operacion == 'plan':
        res = generar_plan_papelera(args.reporte, args.db, incluir_posibles=args.incluir_posibles,
                                    umbral_confianza=args.confianza)
    else:
        print('Esta operación mueve archivos de kDrive a su papelera y modifica SQLite local.')
        frase = input(f'Escribe exactamente {FRASE_CONFIRMACION}: ').strip()
        res = aplicar_plan_papelera(args.plan, confirmar=frase)
    print(json.dumps(res, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
