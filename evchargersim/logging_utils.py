"""
evchargersim.logging_utils — formatter colorido (_ColorFormatter) e
build_logger(), o logger deste pacote inteiro (namespace "evchargersim").
"""

import logging
import sys

class _ColorFormatter(logging.Formatter):
    """
    Formatter com cores ANSI — timestamp, charge point ID e nível de log
    cada um com sua própria cor, e a MENSAGEM em si na cor padrão do
    terminal (sem tingir). Antes a linha inteira saía na cor do nível,
    o que deixava o texto real (a parte que importa ler) tão colorido
    quanto os metadados ao redor dele; separar as cores deixa mais fácil
    escanear "quando / de qual charger / que tipo de evento" de relance
    e ainda ler o conteúdo da mensagem sem esforço extra.

    use_color desliga tudo automaticamente quando a saída não é um
    terminal real (ex: `python evchargersim.py > log.txt` ou quando um
    outro processo captura o stdout) — sem isso, o arquivo/pipe ficaria
    cheio de códigos de escape ilegíveis em vez de texto limpo.
    """
    _LEVEL_COLORS = {
        logging.DEBUG:    "\033[2m",     # cinza (dim)
        logging.INFO:     "\033[36m",    # ciano
        logging.WARNING:  "\033[33m",    # amarelo
        logging.ERROR:    "\033[31m",    # vermelho
        logging.CRITICAL: "\033[1;31m",  # vermelho negrito
    }
    _TIME_COLOR = "\033[2m"    # cinza (dim) — timestamp é o metadado menos importante
    _ID_COLOR = "\033[1;34m"   # azul negrito — destaca o charge point ID
    _RESET = "\033[0m"

    def __init__(self, datefmt, charge_point_id, use_color):
        super().__init__(datefmt=datefmt)
        self._tag = f"[{charge_point_id}]"
        self._use_color = use_color

    def format(self, record):
        timestamp = self.formatTime(record, self.datefmt)
        level = f"{record.levelname:<7}"
        message = record.getMessage()

        # Preserva o comportamento padrão do logging para exceções: se o
        # log veio de logger.exception(...)/exc_info=True, anexa o
        # traceback formatado depois da mensagem (senão o traceback
        # inteiro seria descartado silenciosamente por este formatter
        # customizado, ao contrário do logging.Formatter padrão).
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            message = f"{message}\n{record.exc_text}"

        if not self._use_color:
            return f"{timestamp} {self._tag} {level} {message}"

        level_color = self._LEVEL_COLORS.get(record.levelno, "")
        return (
            f"{self._TIME_COLOR}{timestamp}{self._RESET} "
            f"{self._ID_COLOR}{self._tag}{self._RESET} "
            f"{level_color}{level}{self._RESET} "
            f"{message}"
        )


def build_logger(charge_point_id: str, verbose: bool) -> logging.Logger:
    """
    Cria/retorna o logger DESTE charge point específico.

    Corrigido para não usar mais logging.basicConfig() + getLogger fixo
    ("evchargersim") — os dois são singletons de processo, então em modo
    frota (N chamadas de build_logger, uma por charger) TODOS os
    chargers acabavam compartilhando o mesmo objeto de logger: só o
    handler/formatter da PRIMEIRA chamada era anexado (basicConfig() é
    no-op nas chamadas seguintes sem force=True), e como o
    _ColorFormatter grava o charge_point_id dentro de si mesmo na
    construção, todo mundo logava com a tag do primeiro charger. Em modo
    único isso nunca dava as caras (só existe uma chamada), mas quebrava
    silenciosamente em modo frota. Cada charger agora tem seu próprio
    logger nomeado ("evchargersim.<id>") com seu próprio handler.
    """
    use_color = sys.stdout.isatty()
    logger = logging.getLogger(f"evchargersim.{charge_point_id}")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_ColorFormatter(
            datefmt="%H:%M:%S",
            charge_point_id=charge_point_id,
            use_color=use_color,
        ))
        logger.addHandler(handler)
    # Não propaga pro root logger — evita linha duplicada caso algo
    # externo (app hospedeiro, outro basicConfig) configure um handler
    # no root depois.
    logger.propagate = False

    # A biblioteca ocpp loga CADA mensagem OCPP crua (send/receive, JSON
    # completo) no logger "ocpp" em nível INFO — é isso que produz aqueles
    # blocos gigantes de JSON quebrados em várias linhas no terminal,
    # atropelando os logs legíveis deste script (ex: as linhas verdes de
    # MeterValues). Subindo para WARNING, só erros/CALLError da lib
    # aparecem; o tráfego OCPP completo continua sendo processado
    # normalmente, só não é mais IMPRESSO. (Idempotente — seguro chamar
    # de novo a cada charger em modo frota.)
    logging.getLogger("ocpp").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    return logger


