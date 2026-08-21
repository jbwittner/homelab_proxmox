"""Outillage spécifique au cluster PostgreSQL mutualisé.

Ce paquet connaît PostgreSQL ; `core` et `proxmox` ne le connaissent pas. Il
est poussé dans le conteneur avec `core`, jamais avec `proxmox` — d'où les
imports paresseux dans `cli`.
"""
