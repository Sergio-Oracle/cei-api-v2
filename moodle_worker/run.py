#!/usr/bin/env python3
"""Point d'entrée — service cei-moodle-sync.

Surveillance des changements Moodle et synchronisation automatique, dans un
processus à part (comme l'agent de surveillance) : l'API web et le service
de notifications ne font plus aucun travail de fond pour Moodle. Voir
services/moodle_auto.py pour le détail et le rythme des lectures.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.moodle_auto import run_forever

if __name__ == "__main__":
    run_forever()
