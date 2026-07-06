#!/usr/bin/env python3
"""Point d'entrée — Lance le service Agent Proctor CEI."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_proctor.monitor import run

if __name__ == "__main__":
    run()
