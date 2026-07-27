"""
Ponto de entrada de `python -m evchargersim ...` — ver o docstring de
evchargersim/__init__.py para exemplos de uso (modo único e modo frota).
"""

import asyncio
import logging

from .orchestrator import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger("evchargersim").info("Simulador encerrado manualmente.")
