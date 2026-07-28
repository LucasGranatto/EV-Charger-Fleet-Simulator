"""
evchargersim.control_panel — painel web de controle (modo frota):
serve o frontend dedicado (evchargersim/frontend/index.html, style.css,
app.js — arquivos estáticos de verdade, sem build step) e expõe a API
que ele consome, /api/state + /api/command (_make_control_handler), via
start_control_server(), que sobe tudo numa ThreadingHTTPServer em thread
separada.

Só biblioteca padrão de propósito. nada de aiohttp ou outra dependência
nova só pra isso. A ponte entre a thread do HTTP server e o event loop
asyncio principal (onde vivem as instâncias de EVChargerSim) é feita via
asyncio.run_coroutine_threadsafe(...).result(timeout=...) em cada
requisição.

/api/command/all executa um comando (connect/disconnect/pause/resume/
etc.) em TODOS os chargers registrados de uma vez — usado pelas ações
"todos" do painel. Ver broadcast_command() em orchestrator.main().
"""

import asyncio
import json
import logging
import mimetypes
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Frontend dedicado: HTML/CSS/JS em arquivos separados (sem build step),
# ao lado deste módulo. Antes era uma única string CONTROL_PANEL_HTML
# embutida aqui no Python — virou seu próprio diretório pra poder ser
# editado como um frontend de verdade (syntax highlighting, lint, etc.)
# em vez de texto dentro de uma string Python.
FRONTEND_DIR = Path(__file__).parent / "frontend"

# Whitelist explícita de arquivos servíveis — evita expor o diretório
# inteiro (ou qualquer coisa fora dele via "..") por engano; só estes
# três arquivos, nada mais, é intencional.
_STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/style.css": "style.css",
    "/app.js": "app.js",
}


def _make_control_handler(registry: dict, loop: asyncio.AbstractEventLoop, logger: logging.Logger,
                           spawn, remove, broadcast):
    """
    Fábrica de classe do handler HTTP do painel de controle — precisa ser
    uma fábrica (em vez de uma classe direta) porque BaseHTTPRequestHandler
    não tem como receber argumentos extras no __init__ (o ThreadingHTTPServer
    o instancia sozinho por requisição); fechar `registry`/`loop`/`logger`/
    `spawn`/`remove`/`broadcast` no escopo aqui é o jeito de fazer todos
    chegarem até o handler.

    `spawn`/`remove` são corotinas `async def (charge_point_id) -> str`
    (retornam mensagem de resultado, levantam ValueError em erro de uso —
    ex: ID duplicado ou inexistente) definidas em orchestrator.main() —
    passadas por aqui em vez de importadas, pra não criar um import
    circular (orchestrator já importa start_control_server deste módulo).

    `broadcast` é a corotina `async def (cmd, args) -> dict` (também de
    orchestrator.main()) que executa um comando em todos os chargers do
    registry de uma vez — usada por POST /api/command/all.
    """

    class ControlPanelHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # BaseHTTPRequestHandler por padrão escreve direto em stderr —
            # sem isso, cada GET /api/state (a cada 1.5s) poluiria o
            # terminal por cima dos logs coloridos de cada charger.
            logger.debug("[PAINEL] " + fmt % args)

        def _send_json(self, payload, status=200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            filename = _STATIC_FILES.get(self.path)
            if filename is not None:
                file_path = FRONTEND_DIR / filename
                try:
                    body = file_path.read_bytes()
                except OSError:
                    self._send_json(
                        {"error": f"frontend file missing: {filename}"}, status=500
                    )
                    return
                content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/state":
                # snapshot de todos os chargers já registrados — um
                # charger que ainda não completou a 1ª conexão (cp ==
                # None) simplesmente ainda não aparece na lista.
                snapshot = [cp.get_status_dict() for cp in registry.values()]
                self._send_json(snapshot)
            else:
                self._send_json({"error": "not found"}, status=404)

        def do_POST(self):
            if self.path == "/api/command":
                self._handle_command()
            elif self.path == "/api/command/all":
                self._handle_command_all()
            elif self.path == "/api/chargers":
                self._handle_add_charger()
            else:
                self._send_json({"error": "not found"}, status=404)

        def do_DELETE(self):
            prefix = "/api/chargers/"
            if not self.path.startswith(prefix):
                self._send_json({"error": "not found"}, status=404)
                return
            charge_point_id = urllib.parse.unquote(self.path[len(prefix):])
            try:
                future = asyncio.run_coroutine_threadsafe(remove(charge_point_id), loop)
                message = future.result(timeout=15)
                self._send_json({"ok": True, "message": message})
            except ValueError as exc:
                self._send_json({"ok": False, "message": str(exc)}, status=404)
            except Exception as exc:
                logger.exception(f"[PAINEL] erro removendo charger '{charge_point_id}'")
                self._send_json({"ok": False, "message": f"erro: {exc!r}"}, status=500)

        def _read_json_body(self):
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")

        def _handle_add_charger(self):
            try:
                payload = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "message": "corpo inválido"}, status=400)
                return
            charge_point_id = payload.get("charge_point_id")
            try:
                future = asyncio.run_coroutine_threadsafe(spawn(charge_point_id), loop)
                message = future.result(timeout=15)
                self._send_json({"ok": True, "message": message})
            except ValueError as exc:
                self._send_json({"ok": False, "message": str(exc)}, status=400)
            except Exception as exc:
                logger.exception(f"[PAINEL] erro adicionando charger '{charge_point_id}'")
                self._send_json({"ok": False, "message": f"erro: {exc!r}"}, status=500)

        def _handle_command(self):
            try:
                payload = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "message": "corpo inválido"}, status=400)
                return

            charge_point_id = payload.get("charge_point_id")
            cmd = (payload.get("cmd") or "").lower()
            args = payload.get("args") or []
            cp = registry.get(charge_point_id)
            if cp is None:
                self._send_json(
                    {"ok": False, "message": f"charger '{charge_point_id}' não conectado ainda"},
                    status=404,
                )
                return

            # A instância EVChargerSim vive no event loop principal
            # (thread diferente desta ThreadingHTTPServer) — precisa
            # atravessar pra lá via run_coroutine_threadsafe e esperar o
            # resultado de forma síncrona (.result()) antes de responder.
            try:
                future = asyncio.run_coroutine_threadsafe(
                    cp.execute_command(cmd, args), loop
                )
                message = future.result(timeout=15)
                self._send_json({"ok": True, "message": message})
            except Exception as exc:
                logger.exception(f"[PAINEL] erro executando comando '{cmd}' em '{charge_point_id}'")
                self._send_json({"ok": False, "message": f"erro: {exc!r}"}, status=500)

        def _handle_command_all(self):
            """
            POST /api/command/all — {cmd, args} sem charge_point_id:
            executa o mesmo comando em TODOS os chargers do registry de
            uma vez (ações "conectar todos"/"desconectar todos"/"pausar
            todos"/"retomar todos" do painel). Cada charger responde
            (ou falha) independente dos demais — ver broadcast_command()
            em orchestrator.main().
            """
            try:
                payload = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json({"ok": False, "message": "corpo inválido"}, status=400)
                return

            cmd = (payload.get("cmd") or "").lower()
            args = payload.get("args") or []
            if not cmd:
                self._send_json({"ok": False, "message": "cmd vazio"}, status=400)
                return

            try:
                future = asyncio.run_coroutine_threadsafe(broadcast(cmd, args), loop)
                results = future.result(timeout=20)
            except Exception as exc:
                logger.exception(f"[PAINEL] erro executando comando em massa '{cmd}'")
                self._send_json({"ok": False, "message": f"erro: {exc!r}"}, status=500)
                return

            if not results:
                self._send_json({"ok": True, "message": "nenhum charger conectado ainda", "results": {}})
                return
            self._send_json({
                "ok": True,
                "message": f"'{cmd}' executado em {len(results)} charger(s).",
                "results": results,
            })

    return ControlPanelHandler



def start_control_server(registry: dict, port: int, loop: asyncio.AbstractEventLoop,
                          logger: logging.Logger, spawn, remove, broadcast) -> ThreadingHTTPServer:
    """
    Sobe o painel web de controle numa ThreadingHTTPServer rodando em
    thread separada e mantém o servidor de controle fora do event loop
    principal (onde rodam os N charge points) usando apenas a
    biblioteca padrão, sem puxar aiohttp ou outra dependência nova só
    pra isso.

    Ponte entre a thread do HTTP server e o event loop asyncio principal
    (onde vivem as instâncias de EVChargerSim): cada requisição usa
    asyncio.run_coroutine_threadsafe(...).result(timeout=...) pra
    executar a corotina certa no loop principal e esperar o resultado
    de forma síncrona antes de responder ao browser.

    `spawn`/`remove`: corotinas `async def (charge_point_id) -> str`
    que o painel usa pra adicionar/remover chargers em tempo real (ver
    orchestrator.main()).
    `broadcast`: corotina `async def (cmd, args) -> dict` que executa
    um comando em todos os chargers do registry de uma vez (ações
    "todos" do painel — ver orchestrator.main()).
    """
    handler_cls = _make_control_handler(registry, loop, logger, spawn, remove, broadcast)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="control-panel")
    thread.start()
    logger.info(f"[PAINEL] painel de controle web em http://localhost:{port}")
    return server
