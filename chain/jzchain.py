#!/usr/bin/env python3
"""
On-chain score settlement for jen-zombie. Devnet only.

WHAT THIS IS
    The game already scores: walker 10, runner 15, brute 25, snapshot 50, a hit
    -20, never below zero. This mints one SPL token per point, on Solana devnet,
    in ONE transaction per run rather than one per kill.

WHY IT IS SHAPED LIKE THIS
    It is a READ-ONLY POLLER. It asks the Pi for the telemetry the dashboard
    already reads, notices when a run ended, and settles. It is not called by
    the phone, it is not called by the Pi, and nothing in the game knows it
    exists. Kill this process mid-run and the game does not change by one frame
    -- that is the entire point of the architecture and it is the first thing
    the test suite checks.

WHY BATCHES, AND WHY THIS CHAIN
    A transaction inside the combat loop would cost frames, so settlement
    happens on the two boundaries the game already has: death, and an explicit
    trigger. Batching is only economically sane because a Solana signature is
    ~5000 lamports -- at devnet-equivalent mainnet prices a whole night of play
    settles for a fraction of a cent. On a chain with dollar fees you would be
    forced to either eat the cost per run or build an off-chain ledger, which
    is the thing that makes a points counter "a database with extra steps".

WHAT IT NEVER DOES
    Raise. Devnet RPC is flaky and rate-limited. Every network call retries with
    backoff and then gives up QUIETLY -- but a failure is recorded as a failure
    and shown as one. A settlement that did not land stays in the queue and is
    retried; it is never dropped and never rendered as success.

KEYS
    Mint authority and player keypairs live in chain/keys/*.json at mode 600,
    gitignored. Devnet only, so the blast radius of a leak is nil, but they are
    still treated as keys. Nothing here ever runs in a browser.

USAGE
    ./.venv/bin/python jzchain.py setup      create/fund the mint and the wallet
    ./.venv/bin/python jzchain.py serve      poll telemetry and settle; panel on :8100
    ./.venv/bin/python jzchain.py settle     settle whatever is outstanding, now
    ./.venv/bin/python jzchain.py status     print state and exit
"""

import json
import os
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS = os.path.join(HERE, "keys")
STATE_PATH = os.path.join(HERE, "state.json")
QUEUE_PATH = os.path.join(HERE, "queue.json")

# The dashboard's own source. Read-only, and the only thing this process asks
# of the Pi.
TELEMETRY_URL = os.environ.get("JZ_TELEMETRY_URL", "https://10.42.1.1:8443/telemetry")
RPC_URL = os.environ.get("JZ_SOLANA_RPC", "https://api.devnet.solana.com")
CLUSTER = os.environ.get("JZ_SOLANA_CLUSTER", "devnet")
PORT = int(os.environ.get("JZ_CHAIN_PORT", "8100"))
POLL_S = float(os.environ.get("JZ_CHAIN_POLL_S", "0.5"))
# A session that stops posting for this long is over; anything unsettled in it
# is flushed rather than stranded.
SESSION_IDLE_S = float(os.environ.get("JZ_CHAIN_IDLE_S", "25"))
DECIMALS = 0                      # a score is a whole number of points
# `t` is ms since the session began and only counts up, so a DECREASE is a new
# run. The slack absorbs out-of-order arrivals at 5 Hz and nothing more: a
# full second of it was wide enough to miss a reload that restarted at t=10
# while the old session was only a second in, which silently merged two runs.
NEW_SESSION_SLACK_MS = 250
MIN_SOL = 0.02                    # top up below this
TOKEN_NAME = "JENZ"

# Devnet only, deliberately and permanently. A mainnet URL here would let this
# be mistaken for a financial claim; it is a score.
if "devnet" not in RPC_URL and "localhost" not in RPC_URL and "127.0.0.1" not in RPC_URL:
    print(f"[chain] REFUSING non-devnet RPC: {RPC_URL}", flush=True)
    sys.exit(2)

_print_lock = threading.Lock()


def log(*a):
    with _print_lock:
        print("[chain]", *a, flush=True)


# --------------------------------------------------------------------------
# Keys and state
# --------------------------------------------------------------------------

def _load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, obj, mode=0o600):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=1)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        return True
    except Exception as e:
        log(f"could not write {os.path.basename(path)}: {e}")
        return False


def keypair(name):
    """Load chain/keys/<name>.json, creating it at mode 600 if absent."""
    from solders.keypair import Keypair
    os.makedirs(KEYS, exist_ok=True)
    os.chmod(KEYS, 0o700)
    path = os.path.join(KEYS, name + ".json")
    raw = _load_json(path)
    if isinstance(raw, list) and len(raw) == 64:
        try:
            return Keypair.from_bytes(bytes(raw))
        except Exception as e:
            log(f"{name}.json unreadable ({e}); generating a new one")
    kp = Keypair()
    # The same JSON-array-of-bytes shape solana-keygen writes, so the CLI can
    # read these if anyone needs to inspect them.
    _save_json(path, list(bytes(kp)), mode=0o600)
    log(f"created {name} {kp.pubkey()}")
    return kp


# --------------------------------------------------------------------------
# JSON-RPC. solana-py 0.40 ships only an async client; this process is
# threaded, and the retry policy is the part that has to be exactly right,
# so the transport is 30 lines of urllib rather than an event loop.
# --------------------------------------------------------------------------

class Rpc:
    def __init__(self, url=RPC_URL, attempts=5, timeout=20):
        self.url = url
        self.attempts = attempts
        self.timeout = timeout
        self.calls = 0
        self.failures = 0
        self.last_error = None

    def call(self, method, params=None, attempts=None):
        """Returns (result, error_string). NEVER raises."""
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params or []}).encode()
        n = attempts if attempts is not None else self.attempts
        err = "not attempted"
        for i in range(n):
            self.calls += 1
            try:
                req = urllib.request.Request(
                    self.url, data=body, method="POST",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    payload = json.loads(r.read().decode("utf-8", "replace"))
                if "error" in payload:
                    e = payload["error"]
                    err = f"{e.get('code')}: {str(e.get('message'))[:180]}"
                    # A malformed request will fail identically every time;
                    # only rate limits and server faults are worth retrying.
                    if e.get("code") not in (-32005, -32603, -32004):
                        self.failures += 1
                        self.last_error = err
                        return None, err
                else:
                    return payload.get("result"), None
            except urllib.error.HTTPError as e:
                err = f"HTTP {e.code}"
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:140]}"
            if i < n - 1:
                # Exponential with jitter: devnet rate-limits hard, and a fleet
                # of retries landing on the same millisecond makes it worse.
                time.sleep(min(8.0, 0.4 * (2 ** i)) * (0.6 + random.random() * 0.8))
        self.failures += 1
        self.last_error = err
        return None, err


# --------------------------------------------------------------------------
# Chain operations
# --------------------------------------------------------------------------

LAMPORTS = 1_000_000_000


class Chain:
    """The mint, the wallet and the one transaction that matters."""

    def __init__(self, rpc=None):
        self.rpc = rpc or Rpc()
        self.authority = keypair("mint_authority")   # also the fee payer
        self.player = keypair("player")
        self.state = _load_json(STATE_PATH, {}) or {}
        self.ready = False
        self.setup_error = None

    # ---- read-only ----
    def sol(self, pubkey):
        res, err = self.rpc.call("getBalance", [str(pubkey)])
        if err or not isinstance(res, dict):
            return None
        return res.get("value", 0) / LAMPORTS

    def token_balance(self):
        ata = self.state.get("ata")
        if not ata:
            return None
        res, err = self.rpc.call("getTokenAccountBalance", [ata])
        if err or not isinstance(res, dict):
            return None
        try:
            return int(res["value"]["amount"])
        except Exception:
            return None

    def blockhash(self):
        res, err = self.rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])
        if err or not isinstance(res, dict):
            return None, err or "no blockhash"
        try:
            from solders.hash import Hash
            return Hash.from_string(res["value"]["blockhash"]), None
        except Exception as e:
            return None, f"bad blockhash: {e}"

    def _send(self, instructions, signers):
        """Build, sign, send, confirm. Returns (signature, error)."""
        from solders.transaction import Transaction
        bh, err = self.blockhash()
        if err:
            return None, err
        try:
            tx = Transaction.new_signed_with_payer(
                instructions, self.authority.pubkey(), signers, bh)
            import base64
            raw = base64.b64encode(bytes(tx)).decode()
        except Exception as e:
            return None, f"could not build transaction: {type(e).__name__}: {e}"
        res, err = self.rpc.call("sendTransaction", [
            raw, {"encoding": "base64", "skipPreflight": False,
                  "preflightCommitment": "confirmed", "maxRetries": 3}])
        if err:
            return None, err
        sig = res if isinstance(res, str) else None
        if not sig:
            return None, "no signature returned"
        return sig, self._confirm(sig)

    def _confirm(self, sig, timeout_s=45):
        """None when confirmed, otherwise why not. Never raises."""
        deadline = time.time() + timeout_s
        last = "not confirmed"
        while time.time() < deadline:
            res, err = self.rpc.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}],
                                     attempts=2)
            if not err and isinstance(res, dict):
                try:
                    st = (res.get("value") or [None])[0]
                except Exception:
                    st = None
                if st:
                    if st.get("err"):
                        return f"transaction failed on chain: {str(st['err'])[:140]}"
                    if st.get("confirmationStatus") in ("confirmed", "finalized"):
                        return None
                    last = f"status {st.get('confirmationStatus')}"
            elif err:
                last = err
            time.sleep(1.2)
        return last

    # ---- setup ----
    def airdrop(self, need=MIN_SOL):
        have = self.sol(self.authority.pubkey())
        if have is None:
            return False, "could not read balance"
        if have >= need:
            return True, f"{have:.4f} SOL"
        res, err = self.rpc.call("requestAirdrop", [str(self.authority.pubkey()), int(1 * LAMPORTS)])
        if err:
            return False, f"airdrop refused ({err}) -- devnet faucets rate-limit hard"
        if isinstance(res, str):
            self._confirm(res)
        have = self.sol(self.authority.pubkey())
        return (have or 0) >= need, f"{(have or 0):.4f} SOL"

    def ensure(self):
        """Idempotent: mint + player token account. Returns (ok, message)."""
        try:
            from solders.pubkey import Pubkey
            from solders.keypair import Keypair
            from solders.system_program import create_account, CreateAccountParams
            from spl.token.constants import TOKEN_PROGRAM_ID
            from spl.token.instructions import (
                initialize_mint2, create_idempotent_associated_token_account,
                get_associated_token_address)
            from spl.token.models import InitializeMint2Params

            ok, msg = self.airdrop()
            if not ok:
                self.setup_error = f"no devnet SOL for fees: {msg}"
                return False, self.setup_error

            mint_str = self.state.get("mint")
            if mint_str:
                res, err = self.rpc.call("getAccountInfo", [mint_str, {"encoding": "base64"}])
                if err or not (isinstance(res, dict) and res.get("value")):
                    log(f"recorded mint {mint_str} is not on chain; making a new one")
                    mint_str = None

            if not mint_str:
                mint_kp = Keypair()
                res, err = self.rpc.call("getMinimumBalanceForRentExemption", [82])
                rent = res if isinstance(res, int) else 1_461_600
                ixs = [
                    create_account(CreateAccountParams(
                        from_pubkey=self.authority.pubkey(),
                        to_pubkey=mint_kp.pubkey(),
                        lamports=rent, space=82, owner=TOKEN_PROGRAM_ID)),
                    initialize_mint2(InitializeMint2Params(
                        decimals=DECIMALS, program_id=TOKEN_PROGRAM_ID,
                        mint=mint_kp.pubkey(),
                        mint_authority=self.authority.pubkey(),
                        freeze_authority=None)),
                ]
                sig, err = self._send(ixs, [self.authority, mint_kp])
                if err:
                    self.setup_error = f"mint creation failed: {err}"
                    return False, self.setup_error
                mint_str = str(mint_kp.pubkey())
                self.state["mint"] = mint_str
                self.state["mint_tx"] = sig
                log(f"mint created {mint_str} ({sig})")

            mint = Pubkey.from_string(mint_str)
            ata = get_associated_token_address(self.player.pubkey(), mint)
            res, err = self.rpc.call("getAccountInfo", [str(ata), {"encoding": "base64"}])
            if not (isinstance(res, dict) and res.get("value")):
                ix = create_idempotent_associated_token_account(
                    self.authority.pubkey(), self.player.pubkey(), mint)
                sig, err = self._send([ix], [self.authority])
                if err:
                    self.setup_error = f"token account creation failed: {err}"
                    return False, self.setup_error
                log(f"player token account {ata} ({sig})")

            self.state.update(mint=mint_str, ata=str(ata), decimals=DECIMALS,
                              player=str(self.player.pubkey()),
                              authority=str(self.authority.pubkey()),
                              cluster=CLUSTER)
            _save_json(STATE_PATH, self.state, mode=0o600)
            self.ready = True
            self.setup_error = None
            return True, mint_str
        except Exception as e:
            # Setup is the only place an import or a library change could throw.
            # It still must not take the process down.
            self.setup_error = f"{type(e).__name__}: {str(e)[:180]}"
            log("setup failed:", self.setup_error)
            return False, self.setup_error

    def mint_points(self, amount):
        """The transaction. One per run, `amount` whole points."""
        if amount <= 0:
            return None, "nothing to settle"
        if not self.state.get("mint") or not self.state.get("ata"):
            return None, "not set up"
        try:
            from solders.pubkey import Pubkey
            from spl.token.constants import TOKEN_PROGRAM_ID
            from spl.token.instructions import mint_to_checked
            from spl.token.models import MintToCheckedParams
            ix = mint_to_checked(MintToCheckedParams(
                program_id=TOKEN_PROGRAM_ID,
                mint=Pubkey.from_string(self.state["mint"]),
                dest=Pubkey.from_string(self.state["ata"]),
                mint_authority=self.authority.pubkey(),
                amount=int(amount), decimals=DECIMALS, signers=[]))
        except Exception as e:
            return None, f"could not build mint: {type(e).__name__}: {e}"
        return self._send([ix], [self.authority])


def solscan(sig):
    return f"https://solscan.io/tx/{sig}?cluster={CLUSTER}" if sig else None


def solscan_account(addr):
    return f"https://solscan.io/account/{addr}?cluster={CLUSTER}" if addr else None


# --------------------------------------------------------------------------
# Run detection. Pure, so it can be tested without a phone or a network.
# --------------------------------------------------------------------------

class RunTracker:
    """
    Turns a stream of telemetry samples into settlement requests.

    The game's score does NOT reset when you die -- `player.reset()` restores
    hearts, not points -- so a run's takings are the DELTA since whatever has
    already been minted. That also makes a second death in the same session
    settle only what was earned after the first, and makes "unsettled" a
    number that means something on screen.
    """

    def __init__(self):
        self.session = None        # telemetry `t` is ms since the session began
        self.score = 0
        # The high-water mark this tracker will not settle below. It is NOT the
        # same as "minted": attaching to a run already in progress sets it to
        # whatever the score was, so the history is never claimed. Conflating
        # the two made the panel read "settled 525" when 250 tokens existed,
        # which is exactly the kind of number a judge is right to distrust.
        self.watermark = 0
        self.minted = 0            # what this tracker actually queued, this session
        self.pre_existing = 0      # score already on the board when we attached
        self.overruns = 0
        self.last_at = 0.0
        self.runs_seen = 0

    @property
    def unsettled(self):
        return max(0, self.score - self.watermark)

    # Kept as a read-only alias: `settled` used to mean the watermark, and
    # calling it that is what caused the confusion.
    @property
    def settled(self):
        return self.watermark

    def observe(self, tel, now):
        """
        Feed one telemetry sample. Returns a list of (amount, reason) to settle.
        Tolerates missing, partial and rubbish payloads.
        """
        out = []
        try:
            if not isinstance(tel, dict):
                return out
            data = tel.get("data") if isinstance(tel.get("data"), dict) else tel
            player = data.get("player") or {}
            sc = player.get("score") or {}
            t = data.get("t")
            score = sc.get("value")
            overruns = player.get("overruns")
            if not isinstance(score, (int, float)):
                return out
            score = int(score)
            t = float(t) if isinstance(t, (int, float)) else 0.0
            overruns = int(overruns) if isinstance(overruns, (int, float)) else 0

            # A session boundary: `t` counts up from zero inside one run, so a
            # decrease is a new one. Flush what the old session still owed.
            if self.session is None or t < self.session - NEW_SESSION_SLACK_MS:
                if self.unsettled > 0:
                    # Belongs to the session being discarded, so it is emitted
                    # WITHOUT claiming: the counters below are about to be
                    # reset for the new run and must not inherit it.
                    out.append((self.unsettled, "session ended"))
                self.session = t
                self.score = score
                self.overruns = overruns
                self.runs_seen += 1
                self.last_at = now
                self.minted = 0
                # A session that already has points when we attach is not a run
                # we watched, so its history is not ours to mint. Recorded
                # separately and shown, rather than quietly folded into a total.
                self.watermark = score
                self.pre_existing = score
                return out

            self.session = t
            self.score = score
            self.last_at = now

            if overruns > self.overruns:
                self.overruns = overruns
                amount = self.unsettled
                if amount > 0:
                    # Claimed here, not by the caller. Doing the bookkeeping at
                    # the point the amount is decided is the only way a session
                    # boundary cannot credit the wrong run.
                    self.take(amount)
                    out.append((amount, "death"))
            elif overruns < self.overruns:
                self.overruns = overruns
        except Exception:
            return []
        return out

    def idle_flush(self, now, idle_s=SESSION_IDLE_S):
        """The phone stopped posting. Settle the remainder rather than lose it."""
        if self.session is None or self.unsettled <= 0:
            return None
        if now - self.last_at < idle_s:
            return None
        amount = self.unsettled
        self.take(amount)
        self.session = None
        return (amount, "session went quiet")

    def take(self, amount):
        """Called once a settlement is queued: it is ours now, do not re-queue."""
        self.watermark += int(amount)
        self.minted += int(amount)


# --------------------------------------------------------------------------
# The queue. A settlement that did not land is kept and retried; it is never
# dropped, and never shown as anything other than what it is.
# --------------------------------------------------------------------------

class Queue:
    MAX = 200

    def __init__(self, path=QUEUE_PATH):
        self.path = path
        self.lock = threading.Lock()
        self.items = _load_json(path, []) or []
        self.seq = max([i.get("id", 0) for i in self.items], default=0)

    def add(self, amount, reason):
        with self.lock:
            self.seq += 1
            item = {"id": self.seq, "amount": int(amount), "reason": reason,
                    "state": "pending", "attempts": 0, "at": time.time(),
                    "signature": None, "error": None, "settled_at": None}
            self.items.append(item)
            self.items = self.items[-self.MAX:]
            self._save()
            return item

    def next_pending(self, now, backoff_base=4.0):
        with self.lock:
            for i in self.items:
                if i["state"] in ("confirmed",):
                    continue
                if i["state"] == "pending" or i["state"] == "failed":
                    wait = 0 if not i["attempts"] else min(120.0, backoff_base * (2 ** (i["attempts"] - 1)))
                    if now - (i.get("last_try") or 0) >= wait:
                        return i
            return None

    def mark(self, item, **kw):
        with self.lock:
            item.update(kw)
            self._save()

    def _save(self):
        _save_json(self.path, self.items, mode=0o600)

    def snapshot(self):
        with self.lock:
            return list(self.items)

    def totals(self):
        with self.lock:
            done = [i for i in self.items if i["state"] == "confirmed"]
            failed = [i for i in self.items if i["state"] == "failed"]
            pend = [i for i in self.items if i["state"] in ("pending", "sending")]
            return {
                "settled_runs": len(done),
                "settled_points": sum(i["amount"] for i in done),
                "failed": len(failed),
                "failed_points": sum(i["amount"] for i in failed),
                "pending": len(pend),
                "pending_points": sum(i["amount"] for i in pend),
            }

    def last_confirmed(self):
        with self.lock:
            for i in reversed(self.items):
                if i["state"] == "confirmed":
                    return dict(i)
            return None


# --------------------------------------------------------------------------
# The service: poll, settle, and show.
# --------------------------------------------------------------------------

def fetch_telemetry(url=TELEMETRY_URL, timeout=4):
    """
    The one thing asked of the Pi, and it is a GET. The demo certificate is a
    mkcert one the Mac may not trust, and this is a read of a score on a local
    wire -- so verification is off here and ONLY here. Nothing is sent.
    """
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return json.loads(r.read().decode("utf-8", "replace")), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"


class Service:
    def __init__(self):
        self.chain = Chain()
        self.tracker = RunTracker()
        self.queue = Queue()
        self.stop = threading.Event()
        self.telemetry_ok = False
        self.telemetry_error = None
        self.polls = 0
        self.started = time.time()
        self.balance = None
        self.sol_balance = None
        self.last_balance_at = 0.0

    # ---- the loops ----
    def poll_loop(self):
        while not self.stop.is_set():
            try:
                tel, err = fetch_telemetry()
                self.polls += 1
                now = time.time()
                if err:
                    self.telemetry_ok = False
                    self.telemetry_error = err
                else:
                    self.telemetry_ok = True
                    self.telemetry_error = None
                    for amount, reason in self.tracker.observe(tel, now):
                        item = self.queue.add(amount, reason)
                        log(f"queued {amount} {TOKEN_NAME} ({reason}) as #{item['id']}")
                flush = self.tracker.idle_flush(now)
                if flush:
                    item = self.queue.add(flush[0], flush[1])
                    log(f"queued {flush[0]} {TOKEN_NAME} ({flush[1]}) as #{item['id']}")
            except Exception as e:
                # The poller outliving every possible bug is the whole contract.
                log("poll loop (ignored):", f"{type(e).__name__}: {e}")
            self.stop.wait(POLL_S)

    def settle_loop(self):
        while not self.stop.is_set():
            try:
                if not self.chain.ready:
                    ok, _ = self.chain.ensure()
                    if not ok:
                        self.stop.wait(15)
                        continue
                item = self.queue.next_pending(time.time())
                if item:
                    self._settle(item)
                self._refresh_balances()
            except Exception as e:
                log("settle loop (ignored):", f"{type(e).__name__}: {e}")
            self.stop.wait(1.0)

    def _settle(self, item):
        self.queue.mark(item, state="sending", attempts=item["attempts"] + 1,
                        last_try=time.time())
        sig, err = self.chain.mint_points(item["amount"])
        if err or not sig:
            # Visible as failed. Kept in the queue. Retried with backoff.
            self.queue.mark(item, state="failed", error=str(err)[:200], signature=sig)
            log(f"settlement #{item['id']} FAILED ({item['amount']} {TOKEN_NAME}): {err}")
            return False
        self.queue.mark(item, state="confirmed", signature=sig, error=None,
                        settled_at=time.time())
        log(f"settled #{item['id']}: {item['amount']} {TOKEN_NAME} -> {solscan(sig)}")
        self.last_balance_at = 0.0          # force a re-read
        return True

    def settle_now(self):
        """Manual trigger: bank whatever the current run has earned so far."""
        amount = self.tracker.unsettled
        if amount <= 0:
            return None, "nothing unsettled"
        item = self.queue.add(amount, "manual")
        self.tracker.take(amount)
        log(f"queued {amount} {TOKEN_NAME} (manual) as #{item['id']}")
        return item, None

    def _refresh_balances(self, every=12.0):
        now = time.time()
        if now - self.last_balance_at < every:
            return
        self.last_balance_at = now
        if not self.chain.ready:
            return
        b = self.chain.token_balance()
        if b is not None:
            self.balance = b
        s = self.chain.sol(self.chain.authority.pubkey())
        if s is not None:
            self.sol_balance = s
            if s < MIN_SOL:
                self.chain.airdrop()

    # ---- what the panel and the dashboard read ----
    def api(self):
        last = self.queue.last_confirmed()
        st = self.chain.state
        return {
            "ok": True,
            "cluster": CLUSTER,
            "uptime_s": round(time.time() - self.started, 1),
            "ready": self.chain.ready,
            "setup_error": self.chain.setup_error,
            "telemetry": {"ok": self.telemetry_ok, "error": self.telemetry_error,
                          "url": TELEMETRY_URL, "polls": self.polls},
            "run": {"session_ms": self.tracker.session, "score": self.tracker.score,
                    "minted": self.tracker.minted,
                    "pre_existing": self.tracker.pre_existing,
                    "watermark": self.tracker.watermark,
                    "unsettled": self.tracker.unsettled,
                    "deaths": self.tracker.overruns, "sessions": self.tracker.runs_seen},
            "token": {"name": TOKEN_NAME, "mint": st.get("mint"), "decimals": DECIMALS,
                      "account": st.get("ata"), "balance": self.balance,
                      "mint_url": solscan_account(st.get("mint")),
                      "account_url": solscan_account(st.get("ata"))},
            "wallet": {"player": st.get("player"), "authority": st.get("authority"),
                       "sol": self.sol_balance},
            "totals": self.queue.totals(),
            "last": ({"id": last["id"], "amount": last["amount"], "reason": last["reason"],
                      "signature": last["signature"], "url": solscan(last["signature"]),
                      "at": last["settled_at"]} if last else None),
            "queue": [{"id": i["id"], "amount": i["amount"], "reason": i["reason"],
                       "state": i["state"], "attempts": i["attempts"],
                       "error": i["error"], "signature": i["signature"],
                       "url": solscan(i["signature"])}
                      for i in self.queue.snapshot()[-12:]][::-1],
            "rpc": {"url": RPC_URL, "calls": self.chain.rpc.calls,
                    "failures": self.chain.rpc.failures,
                    "last_error": self.chain.rpc.last_error},
        }


PANEL = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>jen-zombie - on-chain score</title><style>
:root{--bg:#0b0d10;--card:#11151b;--line:#1d2530;--fg:#e8ecf1;--dim:#8b96a5;
      --ok:#5be08a;--bad:#ff6b6b;--warn:#ffb020;--link:#4aa3ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:15px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;padding:18px}
h1{font-size:19px;margin:0 0 3px}
.sub{color:var(--dim);font-size:13px;margin-bottom:16px}
.tag{display:inline-block;border:1px solid var(--warn);color:var(--warn);
     border-radius:999px;padding:1px 9px;font-size:11px;letter-spacing:1px;margin-left:6px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(290px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
h2{font-size:12px;letter-spacing:1.5px;color:var(--dim);margin:0 0 10px;font-weight:700}
.r{display:flex;justify-content:space-between;gap:10px;padding:3px 0;font-size:13px}
.k{color:var(--dim)} .v{font-weight:700;text-align:right;word-break:break-all}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.dim{color:var(--dim)}
.big{font-size:38px;font-weight:800;line-height:1.1}
a{color:var(--link)}
button{font:inherit;font-weight:700;background:#1f6feb;color:#fff;border:0;
       border-radius:9px;padding:9px 14px;cursor:pointer}
button.alt{background:#2a3038}
.sig{font-size:12px;word-break:break-all;color:var(--dim)}
.big-link{display:block;margin-top:9px;padding:11px;border:1px solid #1f6feb;
          border-radius:10px;text-align:center;font-weight:700;text-decoration:none}
table{width:100%;border-collapse:collapse;font-size:12px}
td{padding:4px 5px;border-top:1px solid var(--line);vertical-align:top}
#msg{color:var(--dim);font-size:12px;min-height:16px;margin-top:8px}
</style></head><body>
<h1>jen-zombie &mdash; on-chain score<span class="tag" id="cluster">DEVNET</span></h1>
<div class="sub">Every point the run scores is one SPL token, minted in a single
transaction at the end of the run &mdash; never per kill, which would cost frames.
This page polls a read-only service; the game does not know it exists.</div>
<div class="grid">
  <div class="card">
    <h2>THIS RUN</h2>
    <div class="big" id="unsettled">&mdash;</div>
    <div class="sub" style="margin:0">points not yet on chain</div>
    <div class="r"><span class="k">score this session</span><span class="v" id="score">&mdash;</span></div>
    <div class="r"><span class="k">minted this session</span><span class="v" id="settled">&mdash;</span></div>
    <div class="r" id="prerow" style="display:none"><span class="k">predates this service</span><span class="v warn" id="pre">&mdash;</span></div>
    <div class="r"><span class="k">deaths</span><span class="v" id="deaths">&mdash;</span></div>
    <div class="r"><span class="k">telemetry</span><span class="v" id="tel">&mdash;</span></div>
    <div style="margin-top:10px"><button id="settle">SETTLE NOW</button>
    <button class="alt" id="refresh">refresh</button></div>
    <div id="msg"></div>
  </div>
  <div class="card">
    <h2>TOKEN BALANCE</h2>
    <div class="big" id="balance">&mdash;</div>
    <div class="sub" style="margin:0"><span id="tokname">JENZ</span> held by the player wallet</div>
    <div class="r"><span class="k">mint</span><span class="v"><a id="mintlink" href="#">&mdash;</a></span></div>
    <div class="r"><span class="k">token account</span><span class="v"><a id="atalink" href="#">&mdash;</a></span></div>
    <div class="r"><span class="k">player wallet</span><span class="v" id="player">&mdash;</span></div>
    <div class="r"><span class="k">fee payer SOL</span><span class="v" id="sol">&mdash;</span></div>
  </div>
  <div class="card">
    <h2>LAST TRANSACTION</h2>
    <div class="r"><span class="k">amount</span><span class="v" id="lastamt">&mdash;</span></div>
    <div class="r"><span class="k">settled on</span><span class="v" id="lastwhy">&mdash;</span></div>
    <div class="sig" id="lastsig">no transaction yet</div>
    <a class="big-link" id="solscan" href="#" target="_blank" rel="noopener">OPEN IN SOLSCAN</a>
  </div>
  <div class="card">
    <h2>SETTLEMENTS</h2>
    <div class="r"><span class="k">confirmed</span><span class="v ok" id="tdone">&mdash;</span></div>
    <div class="r"><span class="k">pending</span><span class="v" id="tpend">&mdash;</span></div>
    <div class="r"><span class="k">failed (kept, retrying)</span><span class="v" id="tfail">&mdash;</span></div>
    <div class="r"><span class="k">RPC failures</span><span class="v" id="rpcfail">&mdash;</span></div>
    <div class="sig" id="rpcerr"></div>
    <table id="q"></table>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const fmt = n => (n === null || n === undefined) ? '\\u2014' : String(n);
const short = s => s ? s.slice(0, 8) + '\\u2026' + s.slice(-6) : '\\u2014';
async function poll() {
  let d;
  try { d = await (await fetch('/api', {cache:'no-store'})).json(); }
  catch (e) { $('msg').textContent = 'service unreachable: ' + e; return; }
  $('cluster').textContent = (d.cluster || '').toUpperCase();
  $('unsettled').textContent = fmt(d.run.unsettled);
  $('score').textContent = fmt(d.run.score);
  $('settled').textContent = fmt(d.run.minted);
  // Points that were already on the board when the service attached are shown
  // as exactly that, never folded into a settled total.
  $('prerow').style.display = d.run.pre_existing ? 'flex' : 'none';
  $('pre').textContent = d.run.pre_existing + ' not claimed (start me before the run)';
  $('deaths').textContent = fmt(d.run.deaths);
  const t = $('tel');
  t.textContent = d.telemetry.ok ? 'reading the game' : ('no telemetry: ' + (d.telemetry.error||''));
  t.className = 'v ' + (d.telemetry.ok ? 'ok' : 'bad');
  $('balance').textContent = fmt(d.token.balance);
  $('tokname').textContent = d.token.name;
  const ml = $('mintlink'); ml.textContent = short(d.token.mint); ml.href = d.token.mint_url || '#';
  const al = $('atalink'); al.textContent = short(d.token.account); al.href = d.token.account_url || '#';
  $('player').textContent = short(d.wallet.player);
  $('sol').textContent = d.wallet.sol === null ? '\\u2014' : d.wallet.sol.toFixed(4) + ' SOL';
  if (d.last) {
    $('lastamt').textContent = d.last.amount + ' ' + d.token.name;
    $('lastwhy').textContent = d.last.reason;
    $('lastsig').textContent = d.last.signature;
    $('solscan').href = d.last.url; $('solscan').style.opacity = 1;
  } else { $('solscan').style.opacity = .35; }
  $('tdone').textContent = d.totals.settled_runs + ' runs, ' + d.totals.settled_points + ' pts';
  $('tpend').textContent = d.totals.pending + ' (' + d.totals.pending_points + ' pts)';
  const tf = $('tfail');
  tf.textContent = d.totals.failed + ' (' + d.totals.failed_points + ' pts)';
  tf.className = 'v ' + (d.totals.failed ? 'bad' : 'dim');
  $('rpcfail').textContent = d.rpc.failures + ' of ' + d.rpc.calls;
  $('rpcerr').textContent = d.rpc.last_error || '';
  if (!d.ready && d.setup_error) $('msg').textContent = 'setup: ' + d.setup_error;
  $('q').innerHTML = d.queue.map(i => {
    const cls = i.state === 'confirmed' ? 'ok' : (i.state === 'failed' ? 'bad' : 'warn');
    const link = i.url ? `<a href="${i.url}" target="_blank" rel="noopener">${short(i.signature)}</a>` : (i.error || '');
    return `<tr><td>#${i.id}</td><td><b>${i.amount}</b></td><td class="${cls}">${i.state}</td>`
         + `<td class="dim">${i.reason}</td><td>${link}</td></tr>`;
  }).join('');
}
$('settle').addEventListener('click', async () => {
  $('msg').textContent = 'settling\\u2026';
  try {
    const r = await fetch('/settle', { method: 'POST' });
    const j = await r.json();
    $('msg').textContent = j.ok ? `queued ${j.amount} ${'\\u2014'} watch LAST TRANSACTION` : ('nothing to settle: ' + (j.error||''));
  } catch (e) { $('msg').textContent = 'could not reach the service: ' + e; }
  poll();
});
$('refresh').addEventListener('click', poll);
poll(); setInterval(poll, 1500);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    service = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass                              # the service logs what matters

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # So the Pi-served dashboard can read this too, if it is ever pointed
        # here. Read-only data on a demo LAN.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        try:
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path == "/":
                return self._send(200, PANEL, "text/html; charset=utf-8")
            if path == "/api":
                return self._send(200, self.service.api())
            if path == "/health":
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        try:
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path != "/settle":
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
            except Exception:
                pass
            item, err = self.service.settle_now()
            if err:
                return self._send(200, {"ok": False, "error": err})
            return self._send(200, {"ok": True, "id": item["id"], "amount": item["amount"]})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


# --------------------------------------------------------------------------

def cmd_setup():
    c = Chain()
    log(f"cluster      {CLUSTER}")
    log(f"authority    {c.authority.pubkey()}")
    log(f"player       {c.player.pubkey()}")
    ok, msg = c.ensure()
    if not ok:
        log("SETUP FAILED:", msg)
        return 1
    log(f"mint         {c.state['mint']}")
    log(f"token acct   {c.state['ata']}")
    log(f"balance      {c.token_balance()} {TOKEN_NAME}")
    log(f"mint on solscan  {solscan_account(c.state['mint'])}")
    return 0


def cmd_status():
    c = Chain()
    print(json.dumps({
        "cluster": CLUSTER, "state": c.state,
        "sol": c.sol(c.authority.pubkey()),
        "token_balance": c.token_balance(),
        "queue": Queue().totals(),
    }, indent=1))
    return 0


def cmd_settle_once():
    """Settle everything outstanding in the queue, plus whatever telemetry shows."""
    svc = Service()
    ok, msg = svc.chain.ensure()
    if not ok:
        log("not set up:", msg)
        return 1
    tel, err = fetch_telemetry()
    if not err:
        svc.tracker.observe(tel, time.time())
        if svc.tracker.unsettled > 0:
            svc.settle_now()
    else:
        log("no telemetry:", err, "-- settling the queue only")
    done = 0
    while True:
        item = svc.queue.next_pending(time.time())
        if not item:
            break
        if not svc._settle(item):
            break
        done += 1
    last = svc.queue.last_confirmed()
    if last:
        log(f"last: {last['amount']} {TOKEN_NAME}  {solscan(last['signature'])}")
    log(f"balance {svc.chain.token_balance()} {TOKEN_NAME}")
    return 0 if done else 1


def cmd_serve():
    svc = Service()
    Handler.service = svc
    threading.Thread(target=svc.poll_loop, daemon=True, name="poll").start()
    threading.Thread(target=svc.settle_loop, daemon=True, name="settle").start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    log(f"panel   http://localhost:{PORT}/")
    log(f"reading {TELEMETRY_URL}")
    log(f"rpc     {RPC_URL} ({CLUSTER})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        svc.stop.set()
    return 0


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "serve").lower()
    return {"setup": cmd_setup, "serve": cmd_serve, "status": cmd_status,
            "settle": cmd_settle_once}.get(cmd, cmd_serve)()


if __name__ == "__main__":
    sys.exit(main())
