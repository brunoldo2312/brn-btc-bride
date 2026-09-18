import asyncio, json, os, sys, time, logging
from aiohttp import web, WSMsgType

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("brn-p2p")

RATE_JANELA = 10
RATE_MAX = 60
NGROK_TOKEN_FILE  = "ngrok_token.txt"
NGROK_DOMAIN_FILE = "ngrok_domain.txt"

DIR = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))

MURAL: dict = {}
CLIENTES_WS: set = set()
LOCK = asyncio.Lock()
RATE_LIMIT: dict = {}
TUNEL = None


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as ex:
            resp = ex
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


async def index_handler(request):
    return web.FileResponse(os.path.join(DIR, "index.html"))

async def app_js_handler(request):
    return web.FileResponse(os.path.join(DIR, "app.js"),
        headers={"Content-Type": "application/javascript; charset=utf-8"})

async def config_js_handler(request):
    return web.FileResponse(os.path.join(DIR, "config.js"),
        headers={"Content-Type": "application/javascript; charset=utf-8"})

async def mural_rest_handler(request):
    agora = int(time.time())
    ativas = [o for o in MURAL.values() if o.get("expiracao", 0) > agora]
    return web.json_response({"ordens": ativas, "total": len(ativas)})

async def health_handler(request):
    return web.json_response({
        "status": "ok",
        "clientes_ws": len(CLIENTES_WS),
        "ordens_ativas": len(MURAL),
        "public_url": TUNEL.public_url if TUNEL else None,
        "timestamp": int(time.time()),
    })


def checar_rate_limit(ip: str) -> bool:
    agora = time.time()
    hist = RATE_LIMIT.setdefault(ip, [])
    hist[:] = [t for t in hist if agora - t < RATE_JANELA]
    if len(hist) >= RATE_MAX:
        return False
    hist.append(agora)
    return True


CAMPOS = ["hash","criador","contratoAddress","valorOferecido","valorDesejado","expiracao"]

def validar_ordem(o: dict):
    if not isinstance(o, dict):
        return False, "Ordem invalida"
    for c in CAMPOS:
        if c not in o or o[c] in (None, ""):
            return False, f"Campo ausente: {c}"
    end = str(o.get("contratoAddress",""))
    if not (end.startswith("0x") and len(end) == 42):
        return False, "contratoAddress invalido"
    try:
        if int(o["expiracao"]) <= int(time.time()):
            return False, "Ordem expirada"
    except (ValueError, TypeError):
        return False, "expiracao invalida"
    try:
        if float(o["valorOferecido"]) <= 0 or float(o["valorDesejado"]) <= 0:
            return False, "Valores devem ser positivos"
    except (ValueError, TypeError):
        return False, "Valores invalidos"
    return True, ""


async def broadcast(msg: dict):
    if not CLIENTES_WS: return
    payload = json.dumps(msg)
    async with LOCK:
        clientes = list(CLIENTES_WS)
    mortos = []
    for ws in clientes:
        try: await ws.send_str(payload)
        except Exception: mortos.append(ws)
    if mortos:
        async with LOCK:
            for ws in mortos: CLIENTES_WS.discard(ws)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    ip = request.remote or "unknown"
    async with LOCK: CLIENTES_WS.add(ws)
    log.info(f"[WS] + {ip} (total={len(CLIENTES_WS)})")

    agora = int(time.time())
    snap = [o for o in MURAL.values() if o.get("expiracao", 0) > agora]
    await ws.send_json({"tipo": "snapshot", "ordens": snap})

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                if msg.type == WSMsgType.ERROR:
                    log.error(f"[WS] erro: {ws.exception()}")
                continue
            try:
                d = json.loads(msg.data)
            except json.JSONDecodeError:
                await ws.send_json({"tipo":"erro","msg":"JSON invalido"}); continue

            t = d.get("tipo")

            if t == "publicar_ordem":
                if not checar_rate_limit(ip):
                    await ws.send_json({"tipo":"erro","msg":"Rate limit"}); continue
                o = d.get("ordem")
                ok, err = validar_ordem(o)
                if not ok:
                    await ws.send_json({"tipo":"erro","msg":err}); continue
                async with LOCK: MURAL[o["hash"]] = o
                log.info(f"[MURAL] + {o['hash'][:10]}...")
                await broadcast({"tipo":"nova_ordem","ordem":o})

            elif t == "cancelar_ordem":
                h = d.get("hash")
                if not h: continue
                async with LOCK: rem = MURAL.pop(h, None)
                if rem:
                    log.info(f"[MURAL] - {h[:10]}...")
                    await broadcast({"tipo":"ordem_cancelada","hash":h})

            elif t == "ordem_executada":
                h = d.get("hash")
                if not h: continue
                tx = d.get("txHash","")
                async with LOCK: MURAL.pop(h, None)
                log.info(f"[MURAL] exec {h[:10]}...")
                await broadcast({"tipo":"ordem_executada","hash":h,"txHash":tx})

            elif t == "pedir_snapshot":
                agora2 = int(time.time())
                s = [o for o in MURAL.values() if o.get("expiracao",0) > agora2]
                await ws.send_json({"tipo":"snapshot","ordens":s})

            elif t == "ping":
                await ws.send_json({"tipo":"pong","ts":int(time.time())})
    finally:
        async with LOCK: CLIENTES_WS.discard(ws)
        log.info(f"[WS] - {ip} (total={len(CLIENTES_WS)})")
    return ws


async def tarefa_limpeza(app):
    log.info("[LIMPEZA] iniciada")
    try:
        while True:
            await asyncio.sleep(60)
            agora = int(time.time())
            async with LOCK:
                exp = [h for h, o in MURAL.items() if o.get("expiracao",0) <= agora]
                for h in exp: MURAL.pop(h, None)
            for h in exp:
                await broadcast({"tipo":"ordem_expirada","hash":h})
            if exp: log.info(f"[LIMPEZA] {len(exp)} expiradas")
    except asyncio.CancelledError:
        raise


def _pasta_config():
    return os.path.dirname(sys.executable) if getattr(sys,"frozen",False) \
           else os.path.dirname(os.path.abspath(__file__))


async def iniciar_tasks(app):
    global TUNEL
    app["task_limpeza"] = asyncio.create_task(tarefa_limpeza(app))

    token  = os.environ.get("NGROK_TOKEN")
    domain = os.environ.get("NGROK_DOMAIN")
    if not token:
        base = _pasta_config()
        tp = os.path.join(base, NGROK_TOKEN_FILE)
        dp = os.path.join(base, NGROK_DOMAIN_FILE)
        if os.path.exists(tp): token = open(tp, encoding="utf-8").read().strip()
        if not domain and os.path.exists(dp):
            domain = open(dp, encoding="utf-8").read().strip() or None

    if not token:
        log.warning("[NGROK] token ausente - modo local"); return
    try:
        from ngrok_tunnel import NgrokTunnel
        port = int(os.environ.get("PORT", 8080))
        TUNEL = NgrokTunnel(token=token, target=f"http://localhost:{port}", domain=domain)
        url = TUNEL.start()
        log.info(f"[NGROK] URL: {url}")
    except Exception as e:
        log.error(f"[NGROK] falha: {e}")
        TUNEL = None


async def parar_tasks(app):
    t = app.get("task_limpeza")
    if t:
        t.cancel()
        try: await t
        except asyncio.CancelledError: pass
    global TUNEL
    if TUNEL:
        try: TUNEL.close()
        except Exception as e: log.warning(f"[SHUTDOWN] {e}")
        TUNEL = None


def criar_app():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", index_handler)
    app.router.add_get("/index.html", index_handler)
    app.router.add_get("/app.js", app_js_handler)
    app.router.add_get("/config.js", config_js_handler)
    app.router.add_get("/api/mural", mural_rest_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ws", ws_handler)
    app.on_startup.append(iniciar_tasks)
    app.on_cleanup.append(parar_tasks)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info(f"Servidor: http://localhost:{port}")
    web.run_app(criar_app(), host="0.0.0.0", port=port, print=None)