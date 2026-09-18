"""
comprar_brn.py — Formulário de Compra de BRN com verificação on-chain
========================================================================

Duas opções de pagamento:
  🟠 BTC  → scanner observa um endereço Bitcoin da ponte
  🔵 USDC → scanner observa um endereço Polygon da ponte

Fluxo:
  1. Cliente preenche: endereço Polygon que vai receber BRN + valor em BRN
  2. Programa calcula quanto pagar (BTC ou USDC) e mostra o endereço de pagamento
  3. Cliente paga do lado de fora (carteira BTC ou MetaMask USDC)
  4. Scanner detecta a transação
  5. Se válida → envia BRN da reserva da ponte para a carteira do cliente
  6. Status atualiza em tempo real na tela

Não usa Bitcoin Core. Usa APIs públicas para leitura e web3.py para envio do BRN.
"""

import os
import sys
import json
import time
import threading
import webbrowser
from queue import Queue
from datetime import datetime, timezone

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

import requests


# ============================================================
# Configuração — carregada de config.json
# ============================================================
CONFIG_PADRAO = {
    "network": "mainnet",
    "polygon_rpc": "https://polygon-bor-rpc.publicnode.com",
    "polygon_chain_id": 137,

    # Token BRN (Polygon)
    "brn_contract_address": "0xdBc1c747B1D4c27113F65A4620b8fEaC74e2A210",
    "brn_decimals": 18,

    # Endereço Polygon do operador (que tem estoque de BRN)
    "operator_polygon_address": "0x...",

    # Endereço Bitcoin que recebe pagamentos em BTC
    "pay_btc_address": "bc1q...",

    # Endereço Polygon que recebe pagamentos em USDC
    "pay_usdc_address": "0x...",

    # Preços (editáveis no formulário)
    "price_per_brn_btc":  0.0000001,   # quanto BTC por 1 BRN
    "price_per_brn_usdc": 0.0005,      # quanto USDC por 1 BRN

    # Mínimo de confirmações
    "btc_min_confirmations":  1,
    "polygon_min_confirmations": 3,

    # Limites
    "min_brn_purchase": 1,
    "max_brn_purchase": 10000,
}


def carregar_config():
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "config.json")
    cfg = dict(CONFIG_PADRAO)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"⚠️ Erro lendo config.json: {e}")
    return cfg


# ============================================================
# Clientes de blockchain
# ============================================================
class BitcoinClient:
    """Leitura apenas — Blockstream Esplora."""

    def __init__(self, network="mainnet"):
        self.network = network
        self.base = (
            "https://blockstream.info/api/"
            if network == "mainnet"
            else "https://blockstream.info/testnet/api/"
        )

    def _get(self, path):
        r = requests.get(self.base + path, timeout=20)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        return r.json() if "json" in ct else r.text.strip()

    def tip_height(self):
        return int(self._get("blocks/tip/height"))

    def address_txs(self, address, limit=25):
        txs = self._get(f"address/{address}/txs")
        return txs[:limit] if isinstance(txs, list) else []

    def classify(self, tx, address):
        """Retorna (recebido_sats, enviado_sats, tipo)."""
        recebido = 0
        enviado = 0
        for vout in tx.get("vout", []):
            if vout.get("scriptpubkey_address") == address:
                recebido += int(vout.get("value", 0))
        for vin in tx.get("vin", []):
            prev = vin.get("prevout") or {}
            if prev.get("scriptpubkey_address") == address:
                enviado += int(prev.get("value", 0))
        if recebido == 0 and enviado == 0:
            return 0, 0, "IGNORAR"
        if recebido > enviado:
            return recebido, enviado, "DEPOSITO"
        if enviado > recebido:
            return recebido, enviado, "SAQUE"
        return recebido, enviado, "INTERNO"

    def explorer_url(self, txid):
        return (
            f"https://mempool.space/tx/{txid}"
            if self.network == "mainnet"
            else f"https://mempool.space/testnet/tx/{txid}"
        )


class PolygonClient:
    """Leitura + envio de BRN via web3.py."""

    ERC20_ABI = [
        "function balanceOf(address) view returns (uint256)",
        "function transfer(address,uint256) returns (bool)",
        "function decimals() view returns (uint8)",
        "function symbol() view returns (string)",
    ]

    USDC_ABI = [
        "function balanceOf(address) view returns (uint256)",
        "function decimals() view returns (uint8)",
    ]

    def __init__(self, rpc, chain_id, brn_address, decimals=18):
        try:
            from web3 import Web3
            from web3.middleware import geth_poa_middleware
        except ImportError:
            raise RuntimeError("Instale web3: pip install web3")
        self.Web3 = Web3
        self.w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
        self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        self.chain_id = chain_id
        self.brn = self.w3.eth.contract(
            address=Web3.to_checksum_address(brn_address),
            abi=self.ERC20_ABI,
        )
        self.decimals = decimals

    def ok(self):
        try:
            return self.w3.is_connected()
        except Exception:
            return False

    def block_number(self):
        return self.w3.eth.block_number

    def brn_balance(self, addr):
        a = self.Web3.to_checksum_address(addr)
        raw = self.brn.functions.balanceOf(a).call()
        return raw / (10 ** self.decimals)

    def send_brn(self, to_address, amount_brn, private_key):
        """Transfere BRN da carteira do operador. Retorna dict."""
        try:
            to_ck = self.Web3.to_checksum_address(to_address)
        except Exception:
            return {"success": False, "message": f"Endereço inválido: {to_address}"}

        if amount_brn <= 0:
            return {"success": False, "message": "Valor inválido"}

        key = private_key.strip()
        if not key.startswith("0x"):
            key = "0x" + key
        try:
            account = self.w3.eth.account.from_key(key)
        except Exception as e:
            return {"success": False, "message": f"Chave inválida: {e}"}

        # Saldo
        saldo = self.brn_balance(account.address)
        if saldo < amount_brn:
            return {"success": False,
                    "message": f"Saldo insuficiente. Tenho {saldo:.6f} BRN, preciso {amount_brn:.6f}"}

        # POL para gas
        pol = self.w3.eth.get_balance(account.address) / 1e18
        if pol < 0.005:
            return {"success": False,
                    "message": f"POL insuficiente para gas: {pol:.6f}"}

        wei = int(amount_brn * (10 ** self.decimals))

        try:
            nonce = self.w3.eth.get_transaction_count(account.address)
            tx = self.brn.functions.transfer(to_ck, wei).build_transaction({
                "from": account.address,
                "nonce": nonce,
                "gas": 100000,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": self.chain_id,
            })
        except Exception as e:
            return {"success": False, "message": f"Erro montando tx: {e}"}

        try:
            signed = self.w3.eth.account.sign_transaction(tx, key)
            txh = self.w3.eth.send_raw_transaction(signed.rawTransaction)
            tx_hex = txh.hex()
            if not tx_hex.startswith("0x"):
                tx_hex = "0x" + tx_hex
        except Exception as e:
            return {"success": False, "message": f"Erro enviando: {e}"}

        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(txh, timeout=180)
            if receipt.status != 1:
                return {"success": False, "message": "Tx revertida",
                        "tx_hash": tx_hex}
        except Exception as e:
            return {"success": False, "message": f"Sem confirmação: {e}",
                    "tx_hash": tx_hex}

        return {
            "success": True,
            "tx_hash": tx_hex,
            "block": receipt.blockNumber,
            "explorer": f"https://polygonscan.com/tx/{tx_hex}",
        }


# ============================================================
# UI principal
# ============================================================
class CompraBRN:
    def __init__(self, root, config):
        self.root = root
        self.config = config
        self.root.title("Comprar BRN — Verificação on-chain")
        self.root.geometry("1180x760")

        # blockchain
        self.btc = BitcoinClient(network=config.get("network", "mainnet"))
        self.poly = None
        try:
            self.poly = PolygonClient(
                rpc=config["polygon_rpc"],
                chain_id=config["polygon_chain_id"],
                brn_address=config["brn_contract_address"],
                decimals=config["brn_decimals"],
            )
        except Exception as e:
            print(f"⚠️ Polygon off: {e}")

        # estado
        self.ordens = {}       # order_id -> dict
        self.seen_btc = set()
        self.seen_usdc = set()
        self.running = False
        self.thread = None
        self.ui_queue = Queue()

        self._build_ui()
        self._poll_ui()
        self._atualizar_status_redes()

    # ------------------------------------------------------
    # UI
    # ------------------------------------------------------
    def _build_ui(self):
        # ---------- FORMULÁRIO ----------
        form = ttk.LabelFrame(self.root, text="  🛒  Formulário de Compra de BRN  ")
        form.pack(fill=tk.X, padx=10, pady=8)

        # Linha 1: opção de pagamento
        ttk.Label(form, text="Forma de pagamento:").grid(
            row=0, column=0, sticky=tk.W, padx=8, pady=8)
        self.pay_method = tk.StringVar(value="BTC")
        frame_method = ttk.Frame(form)
        frame_method.grid(row=0, column=1, columnspan=3, sticky=tk.W, padx=8, pady=8)
        ttk.Radiobutton(frame_method, text="🟠 BTC (Bitcoin)",
                        variable=self.pay_method, value="BTC",
                        command=self._recalcular).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(frame_method, text="🔵 USDC (Polygon)",
                        variable=self.pay_method, value="USDC",
                        command=self._recalcular).pack(side=tk.LEFT)

        # Linha 2: endereço BRN que vai receber
        ttk.Label(form, text="Carteira BRN que vai receber (0x…):").grid(
            row=1, column=0, sticky=tk.W, padx=8, pady=6)
        self.brn_receiver_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.brn_receiver_var, width=62).grid(
            row=1, column=1, columnspan=3, sticky=tk.W, padx=8, pady=6)

        # Linha 3: valor em BRN
        ttk.Label(form, text="Quantidade de BRN:").grid(
            row=2, column=0, sticky=tk.W, padx=8, pady=6)
        self.brn_amount_var = tk.StringVar(value="10")
        e = ttk.Entry(form, textvariable=self.brn_amount_var, width=20)
        e.grid(row=2, column=1, sticky=tk.W, padx=8, pady=6)
        e.bind("<KeyRelease>", lambda ev: self._recalcular())

        # Linha 4: preço unitário (editable)
        ttk.Label(form, text="Preço por 1 BRN:").grid(
            row=2, column=2, sticky=tk.W, padx=8, pady=6)
        self.price_var = tk.StringVar()
        pe = ttk.Entry(form, textvariable=self.price_var, width=22)
        pe.grid(row=2, column=3, sticky=tk.W, padx=8, pady=6)
        pe.bind("<KeyRelease>", lambda ev: self._recalcular())

        # Linha 5: endereço de pagamento (destino do cliente)
        ttk.Label(form, text="Cliente paga para:").grid(
            row=3, column=0, sticky=tk.W, padx=8, pady=6)
        self.pay_to_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.pay_to_var, width=62,
                  state="readonly").grid(
            row=3, column=1, columnspan=3, sticky=tk.W, padx=8, pady=6)

        # Linha 6: total
        ttk.Label(form, text="Total a pagar:").grid(
            row=4, column=0, sticky=tk.W, padx=8, pady=6)
        self.total_var = tk.StringVar(value="—")
        ttk.Label(form, textvariable=self.total_var,
                  font=("Consolas", 13, "bold"),
                  foreground="#38bdf8").grid(
            row=4, column=1, columnspan=3, sticky=tk.W, padx=8, pady=6)

        # Linha 7: botões
        bf = ttk.Frame(form)
        bf.grid(row=5, column=0, columnspan=4, sticky=tk.W, padx=8, pady=10)
        ttk.Button(bf, text="📋  Gerar Ordem de Compra",
                   command=self.gerar_ordem).pack(side=tk.LEFT, padx=3)
        ttk.Button(bf, text="▶  Iniciar Scanner",
                   command=self.iniciar_scanner).pack(side=tk.LEFT, padx=3)
        ttk.Button(bf, text="⏹  Parar Scanner",
                   command=self.parar_scanner).pack(side=tk.LEFT, padx=3)
        ttk.Button(bf, text="📂  Carregar do config.json",
                   command=self._carregar_config).pack(side=tk.LEFT, padx=3)

        # ---------- TABELA DE ORDENS ----------
        lf = ttk.LabelFrame(self.root, text="  📦  Ordens  ")
        lf.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        cols = ("order_id", "pagamento", "valor_brn", "endereco_brn",
                "total", "status", "tx_pagamento", "tx_brn")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings", height=12)

        headers = [
            ("order_id",     "ID",             140),
            ("pagamento",    "Método",          80),
            ("valor_brn",    "BRN",             90),
            ("endereco_brn", "Endereço BRN",   280),
            ("total",        "Total a pagar",  160),
            ("status",       "Status",         160),
            ("tx_pagamento", "TX pagamento",   220),
            ("tx_brn",       "TX BRN",         220),
        ]
        for c, t, w in headers:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=tk.W)

        self.tree.tag_configure("aguardando", foreground="#94a3b8")
        self.tree.tag_configure("confirmado", foreground="#38bdf8")
        self.tree.tag_configure("enviado",    foreground="#04d361")
        self.tree.tag_configure("erro",       foreground="#f87171")

        vsb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0), pady=4)
        vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 4), pady=4)

        # ---------- LOG ----------
        logf = ttk.LabelFrame(self.root, text="  📜  Log  ")
        logf.pack(fill=tk.BOTH, expand=False, padx=10, pady=8)
        self.log_box = scrolledtext.ScrolledText(logf, height=8,
                                                 font=("Consolas", 9))
        self.log_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # ---------- BARRA DE STATUS ----------
        self.status_var = tk.StringVar(value="Pronto.")
        ttk.Label(self.root, textvariable=self.status_var,
                  relief=tk.SUNKEN, anchor=tk.W).pack(
            fill=tk.X, side=tk.BOTTOM)

        # inicializa preço
        self._recalcular()

    # ------------------------------------------------------
    # Helpers
    # ------------------------------------------------------
    def log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        linha = f"[{ts}] {msg}\n"
        self.log_box.insert(tk.END, linha)
        self.log_box.see(tk.END)

    def _carregar_config(self):
        cfg = carregar_config()
        self.config.update(cfg)
        self._recalcular()
        self.log("✅ config.json recarregado")

    def _recalcular(self):
        """Atualiza campos dependentes do método e valores."""
        metodo = self.pay_method.get()

        # Preço unitário
        if metodo == "BTC":
            preco = self.config.get("price_per_brn_btc", 0.0000001)
            self.price_var.set(f"{preco:.12f}".rstrip("0").rstrip(".") or "0.0000001")
            pay_addr = self.config.get("pay_btc_address", "")
        else:
            preco = self.config.get("price_per_brn_usdc", 0.0005)
            self.price_var.set(f"{preco:.6f}")
            pay_addr = self.config.get("pay_usdc_address", "")

        self.pay_to_var.set(pay_addr or "⚠️ não configurado")

        # Total
        try:
            qtd = float(self.brn_amount_var.get().replace(",", "."))
            pu = float(self.price_var.get().replace(",", "."))
            total = qtd * pu
            unidade = "BTC" if metodo == "BTC" else "USDC"
            self.total_var.set(f"{total:.8f} {unidade}")
        except ValueError:
            self.total_var.set("—")

    def _atualizar_status_redes(self):
        # BTC
        try:
            h = self.btc.tip_height()
            self.log(f"🟠 Bitcoin OK — bloco {h}")
        except Exception as e:
            self.log(f"❌ Bitcoin: {e}")
        # Polygon
        if self.poly and self.poly.ok():
            self.log(f"🔵 Polygon OK — bloco {self.poly.block_number()}")
        else:
            self.log("❌ Polygon offline")

    # ------------------------------------------------------
    # Gerar ordem
    # ------------------------------------------------------
    def gerar_ordem(self):
        metodo = self.pay_method.get()
        brn_dest = self.brn_receiver_var.get().strip()

        # valida endereço BRN (Polygon)
        if not (brn_dest.startswith("0x") and len(brn_dest) == 42):
            messagebox.showerror("Erro", "Endereço BRN inválido (precisa 0x + 40 hex)")
            return

        try:
            qtd = float(self.brn_amount_var.get().replace(",", "."))
            pu = float(self.price_var.get().replace(",", "."))
        except ValueError:
            messagebox.showerror("Erro", "Valores inválidos")
            return

        if qtd < self.config.get("min_brn_purchase", 1):
            messagebox.showerror("Erro", f"Mínimo: {self.config['min_brn_purchase']} BRN")
            return
        if qtd > self.config.get("max_brn_purchase", 10000):
            messagebox.showerror("Erro", f"Máximo: {self.config['max_brn_purchase']} BRN")
            return

        total = qtd * pu
        order_id = f"BRN-{int(time.time()*1000)}"

        # Endereço de pagamento
        if metodo == "BTC":
            pay_addr = self.config.get("pay_btc_address", "")
            if not pay_addr:
                messagebox.showerror("Erro", "pay_btc_address não configurado")
                return
        else:
            pay_addr = self.config.get("pay_usdc_address", "")
            if not pay_addr:
                messagebox.showerror("Erro", "pay_usdc_address não configurado")
                return

        self.ordens[order_id] = {
            "order_id": order_id,
            "metodo": metodo,
            "brn_dest": brn_dest,
            "brn_qty": qtd,
            "price_unit": pu,
            "total": total,
            "pay_to": pay_addr,
            "status": "AGUARDANDO_PAGAMENTO",
            "tx_pagamento": "",
            "tx_brn": "",
            "criado_em": time.time(),
            "confirmacoes": 0,
        }

        self._add_row(self.ordens[order_id])

        msg = (
            f"📋 Ordem criada!\n\n"
            f"ID: {order_id}\n"
            f"Forma de pagamento: {metodo}\n"
            f"Total: {total:.8f} {metodo}\n"
            f"Pague para: {pay_addr}\n\n"
            f"Destino do BRN: {brn_dest}\n"
            f"Quantidade: {qtd} BRN\n\n"
            f"➡️ Cliente paga agora. O scanner vai detectar automaticamente."
        )
        messagebox.showinfo("Ordem criada", msg)
        self.log(f"📋 Ordem {order_id} — {qtd} BRN — {total:.8f} {metodo}")

        # Auto-inicia scanner
        if not self.running:
            self.iniciar_scanner()

    # ------------------------------------------------------
    # Scanner (thread)
    # ------------------------------------------------------
    def iniciar_scanner(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._scan_loop, daemon=True)
        self.thread.start()
        self.status_var.set("🔄 Scanner rodando…")
        self.log("▶ Scanner iniciado")

    def parar_scanner(self):
        self.running = False
        self.log("⏹ Parando scanner…")

    def _scan_loop(self):
        """Loop: verifica ordens pendentes em ambas as redes."""
        # snapshot inicial — ignora tudo que já existe
        try:
            tip = self.btc.tip_height()
            for ordem in self.ordens.values():
                if ordem["metodo"] == "BTC":
                    for tx in self.btc.address_txs(ordem["pay_to"], 25):
                        self.seen_btc.add(tx.get("txid"))
                    break  # só faz uma vez por endereço, ok
        except Exception as e:
            self.log(f"⚠️ snapshot BTC falhou: {e}")

        while self.running:
            try:
                self._checar_btc()
                self._checar_usdc()
                time.sleep(15)
            except Exception as e:
                self.log(f"⚠️ erro no loop: {e}")
                time.sleep(10)

        self.ui_queue.put(("status", "⏹ Scanner parado"))
        self.ui_queue.put(("running", False))

    # ---- BTC ----
    def _checar_btc(self):
        # agrupa por endereço
        pendentes = [o for o in self.ordens.values()
                     if o["metodo"] == "BTC"
                     and o["status"] in ("AGUARDANDO_PAGAMENTO", "AGUARDANDO_CONFIRMACAO")]
        if not pendentes:
            return

        enderecos = set(o["pay_to"] for o in pendentes)
        tip = self.btc.tip_height()
        min_conf = self.config.get("btc_min_confirmations", 1)

        for addr in enderecos:
            try:
                txs = self.btc.address_txs(addr, 25)
            except Exception as e:
                self.log(f"⚠️ BTC {addr[:12]}… erro: {e}")
                continue

            for tx in txs:
                txid = tx.get("txid")
                if not txid or txid in self.seen_btc:
                    continue

                recebido, enviado, tipo = self.btc.classify(tx, addr)
                if tipo != "DEPOSITO":
                    self.seen_btc.add(txid)
                    continue

                valor_btc = recebido / 1e8
                status = tx.get("status", {})
                confs = (tip - status.get("block_height", tip) + 1) \
                        if status.get("confirmed") else 0

                # procura ordem compatível
                for ordem in pendentes:
                    if ordem["pay_to"] != addr:
                        continue
                    if ordem["tx_pagamento"]:
                        continue
                    # compara valor (tolerância 1%)
                    if abs(valor_btc - ordem["total"]) / ordem["total"] > 0.01:
                        continue

                    ordem["tx_pagamento"] = txid
                    ordem["confirmacoes"] = confs
                    self.log(f"💰 BTC {valor_btc:.8f} detectado — ordem {ordem['order_id']} "
                             f"({confs} conf.)")
                    self.ui_queue.put(("update", dict(ordem)))

                    if confs >= min_conf:
                        ordem["status"] = "PAGAMENTO_CONFIRMADO"
                        self.ui_queue.put(("update", dict(ordem)))
                        self._enviar_brn(ordem)
                    else:
                        ordem["status"] = "AGUARDANDO_CONFIRMACAO"
                    self.seen_btc.add(txid)
                    break

    # ---- USDC ----
    def _checar_usdc(self):
        if not self.poly or not self.poly.ok():
            return

        pendentes = [o for o in self.ordens.values()
                     if o["metodo"] == "USDC"
                     and o["status"] in ("AGUARDANDO_PAGAMENTO", "AGUARDANDO_CONFIRMACAO")]
        if not pendentes:
            return

        # para USDC é mais simples: consultamos logs do contrato USDC
        # filtrando por "to" = pay_usdc_address
        try:
            from web3 import Web3
            usdc_addr = Web3.to_checksum_address(
                "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC Polygon
            )
            transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
            tip = self.poly.block_number()
            de = tip - 200  # últimas ~200 blocos (~7 min)
            for ordem in pendentes:
                pay_to = Web3.to_checksum_address(ordem["pay_to"])
                pay_to_topic = "0x" + "0" * 24 + pay_to[2:].lower()

                logs = self.poly.w3.eth.get_logs({
                    "fromBlock": de,
                    "toBlock": "latest",
                    "address": usdc_addr,
                    "topics": [transfer_topic, None, pay_to_topic],
                })

                for lg in logs:
                    txhash = lg["transactionHash"].hex()
                    if not txhash.startswith("0x"):
                        txhash = "0x" + txhash
                    if txhash in self.seen_usdc:
                        continue

                    # valor (USDC 6 decimals)
                    valor_raw = int.from_bytes(lg["data"], byteorder="big")
                    valor = valor_raw / 1e6

                    if abs(valor - ordem["total"]) / ordem["total"] > 0.01:
                        continue

                    # espera confirmações
                    rec = self.poly.w3.eth.get_transaction_receipt(txhash)
                    confs = tip - rec.blockNumber + 1

                    ordem["tx_pagamento"] = txhash
                    ordem["confirmacoes"] = confs
                    self.log(f"💵 USDC {valor:.4f} detectado — ordem {ordem['order_id']} "
                             f"({confs} conf.)")
                    self.ui_queue.put(("update", dict(ordem)))

                    min_conf = self.config.get("polygon_min_confirmations", 3)
                    if confs >= min_conf:
                        ordem["status"] = "PAGAMENTO_CONFIRMADO"
                        self.ui_queue.put(("update", dict(ordem)))
                        self._enviar_brn(ordem)
                    else:
                        ordem["status"] = "AGUARDANDO_CONFIRMACAO"
                    self.seen_usdc.add(txhash)
                    break
        except Exception as e:
            self.log(f"⚠️ USDC scan: {e}")

    # ------------------------------------------------------
    # Envio do BRN
    # ------------------------------------------------------
    def _enviar_brn(self, ordem):
        if ordem["status"] == "ENVIANDO_BRN":
            return
        if ordem["tx_brn"]:
            return

        if not self.poly:
            ordem["status"] = "ERRO"
            self.log(f"❌ Polygon offline — não consigo enviar BRN da ordem {ordem['order_id']}")
            self.ui_queue.put(("update", dict(ordem)))
            return

        op_key = os.environ.get("BRN_OPERATOR_KEY", "").strip()
        if not op_key:
            ordem["status"] = "ERRO_SEM_CHAVE"
            self.log("❌ BRN_OPERATOR_KEY não definida no ambiente")
            self.ui_queue.put(("update", dict(ordem)))
            return

        ordem["status"] = "ENVIANDO_BRN"
        self.ui_queue.put(("update", dict(ordem)))
        self.log(f"📤 Enviando {ordem['brn_qty']} BRN para {ordem['brn_dest'][:16]}…")

        res = self.poly.send_brn(
            to_address=ordem["brn_dest"],
            amount_brn=ordem["brn_qty"],
            private_key=op_key,
        )

        if res["success"]:
            ordem["status"] = "ENVIADO"
            ordem["tx_brn"] = res["tx_hash"]
            self.log(f"✅ BRN enviado! TX: {res['tx_hash']}")
            self.log(f"   {res['explorer']}")
            self.ui_queue.put(("update", dict(ordem)))
            self.ui_queue.put(("open_explorer", res["explorer"]))
        else:
            ordem["status"] = "ERRO_ENVIO"
            self.log(f"❌ Falha ao enviar BRN: {res['message']}")
            self.ui_queue.put(("update", dict(ordem)))

    # ------------------------------------------------------
    # Tabela
    # ------------------------------------------------------
    def _tag(self, status):
        if status in ("ENVIADO",):
            return "enviado"
        if status in ("PAGAMENTO_CONFIRMADO", "ENVIANDO_BRN", "AGUARDANDO_CONFIRMACAO"):
            return "confirmado"
        if status.startswith("ERRO"):
            return "erro"
        return "aguardando"

    def _add_row(self, ordem):
        self.tree.insert("", tk.END, iid=ordem["order_id"], values=(
            ordem["order_id"],
            ordem["metodo"],
            f"{ordem['brn_qty']}",
            ordem["brn_dest"],
            f"{ordem['total']:.8f} {ordem['metodo']}",
            ordem["status"],
            ordem["tx_pagamento"] or "-",
            ordem["tx_brn"] or "-",
        ), tags=(self._tag(ordem["status"]),))

    def _update_row(self, ordem):
        if not self.tree.exists(ordem["order_id"]):
            return
        self.tree.item(ordem["order_id"], values=(
            ordem["order_id"],
            ordem["metodo"],
            f"{ordem['brn_qty']}",
            ordem["brn_dest"],
            f"{ordem['total']:.8f} {ordem['metodo']}",
            ordem["status"],
            ordem["tx_pagamento"] or "-",
            ordem["tx_brn"] or "-",
        ), tags=(self._tag(ordem["status"]),))

    # ------------------------------------------------------
    # Poll UI queue
    # ------------------------------------------------------
    def _poll_ui(self):
        try:
            while True:
                tipo, payload = self.ui_queue.get_nowait()
                if tipo == "update":
                    self._update_row(payload)
                elif tipo == "status":
                    self.status_var.set(payload)
                elif tipo == "running":
                    pass
                elif tipo == "open_explorer":
                    try:
                        webbrowser.open(payload)
                    except Exception:
                        pass
        except Exception:
            pass
        self.root.after(400, self._poll_ui)


# ============================================================
if __name__ == "__main__":
    cfg = carregar_config()
    root = tk.Tk()
    app = CompraBRN(root, cfg)
    root.mainloop()