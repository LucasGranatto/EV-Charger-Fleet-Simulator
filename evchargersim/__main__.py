"""
Ponto de entrada de `python -m evchargersim ...` — ver o docstring de
evchargersim/__init__.py para exemplos de uso (modo único e modo frota).

Encerramento gracioso: SIGINT (Ctrl+C) e SIGTERM (ex: `docker stop`,
`systemctl stop`, `kill` sem -9) cancelam a task principal em vez de
deixar o processo simplesmente morrer no meio — main() (ver
orchestrator.py) reage a esse cancelamento fechando as conexões
WebSocket de cada charger de forma limpa (frame de close de verdade,
não uma conexão só derrubada) e parando o painel web, antes do processo
sair. Um SEGUNDO sinal enquanto isso está em andamento força saída
imediata, pro caso de algo travar durante o shutdown.
"""

import asyncio
import logging
import os
import signal

from .orchestrator import main

_shutdown_requested = False


def _handle_signal(sig: signal.Signals, main_task: asyncio.Task, logger: logging.Logger):
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning(f"{sig.name} recebido de novo — forçando saída imediata (sem cleanup).")
        os._exit(1)
    _shutdown_requested = True
    logger.info(
        f"{sig.name} recebido — encerrando graciosamente "
        f"(Ctrl+C de novo força saída imediata)..."
    )
    main_task.cancel()


async def _run():
    logger = logging.getLogger("evchargersim")
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig, main_task, logger)
        except NotImplementedError:
            # Windows não suporta add_signal_handler pra todo sinal —
            # SIGINT ainda funciona via KeyboardInterrupt (ver except
            # abaixo), só o shutdown gracioso em SIGTERM fica indisponível.
            pass

    try:
        await main()
    except asyncio.CancelledError:
        # Cancelamento disparado por _handle_signal acima — main() já
        # fez seu próprio cleanup (fechar conexões, parar painel) antes
        # de deixar isso propagar até aqui; só engolimos pra não gerar
        # um traceback de "erro" no que é, na verdade, um shutdown normal.
        pass


if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        # Só alcançável no Windows (sem add_signal_handler) ou numa
        # janela bem estreita antes do handler ser instalado.
        pass
    logging.getLogger("evchargersim").info("Simulador encerrado.")
