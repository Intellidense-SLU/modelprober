#!/usr/bin/env python3
"""
model_probe.py — OpenAI-compatible endpoint model scanner & compatibility checker.
Zero external dependencies. Pure Python stdlib.

Usage:
 API_TOKEN=sk-... python model_probe.py --base-url https://example.org/v1
 python model_probe.py --base-url https://example.org/v1 --api-key sk-... --debug
 python model_probe.py --base-url https://example.org/v1 --api-key sk-... --fast --workers 20

Environment variables:
 API_TOKEN API key (fallback if --api-key not given)
 PROBE_USER_AGENT Override default User-Agent
 HTTPS_PROXY Proxy URL (fallback if --proxy not given)
 NO_COLOR Disable ANSI colours
"""

import urllib.request
import urllib.error
import json
import ssl
import sys
import os
import time
import argparse
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Defaults
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT_UA = (
 "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
 "(KHTML, like Gecko) Chrome/136.0.0.1 Safari/537.36"
)
LONG_PROMPT = "Count from 1 to 500. Output only the numbers separated by commas."
VERSION = "1.0.0"
TEMPERATURE = 0.1

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Colour helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_NO_COLOR = bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()


def _c(code: str, text: str) -> str:
 if _NO_COLOR:
  return text
 codes = {
  "r": "\033[0m", "b": "\033[1m", "d": "\033[2m",
  "red": "\033[91m", "grn": "\033[92m", "ylw": "\033[93m",
  "blu": "\033[94m", "mag": "\033[95m", "cyn": "\033[96m",
  "wht": "\033[97m",
 }
 return f"{codes.get(code, '')}{text}\033[0m"


def ok(msg): print(f" {_c('grn', '✓')} {msg}")
def fail(msg): print(f" {_c('red', '✗')} {msg}")
def warn(msg): print(f" {_c('ylw', '⚠')} {msg}")
def info(msg): print(f" {_c('cyn', '·')} {msg}")
def hdr(msg): print(f"\n{_c('b', msg)}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HTTP client — pure urllib, proxy-aware, debug-capable
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class HTTPClient:
 """Minimal HTTP/JSON client wrapping urllib."""

 def __init__(self, base_url: str, api_key: str, *,
     user_agent: str = DEFAULT_UA,
     proxy: str | None = None,
     debug: bool = False,
     timeout: int = 30,
     no_ssl_verify: bool = False):
  self.base_url = base_url.rstrip("/")
  self.api_key = api_key
  self.user_agent = user_agent
  self.debug = debug
  self.timeout = timeout
  self._print_lock = Lock()

  # SSL context
  self._ssl_ctx = ssl.create_default_context()
  if no_ssl_verify:
   self._ssl_ctx.check_hostname = False
   self._ssl_ctx.verify_mode = ssl.CERT_NONE

  # Build opener chain
  handlers: list = [urllib.request.HTTPSHandler(context=self._ssl_ctx)]
  if proxy:
   handlers.append(urllib.request.ProxyHandler(
    {"http": proxy, "https": proxy}
   ))
  self._opener = urllib.request.build_opener(*handlers)

 # ── core request ──────────────────────────────────────────────
 def request(self, method: str, path: str, body=None,
    extra_headers: dict | None = None) -> tuple[int, dict, dict]:
  """
  Returns (status_code, json_body, response_headers).
  On network-level failure status_code = 0.
  """
  url = f"{self.base_url}{path}"
  hdrs = {
   "Authorization": f"Bearer {self.api_key}",
   "Content-Type": "application/json",
   "User-Agent": self.user_agent,
   "Accept": "application/json",
  }
  if extra_headers:
   hdrs.update(extra_headers)

  data = json.dumps(body).encode("utf-8") if body else None
  req = urllib.request.Request(url, data=data, headers=hdrs, method=method)

  if self.debug:
   self._dbg_request(method, url, hdrs, body)

  try:
   resp = self._opener.open(req, timeout=self.timeout)
   return self._parse(resp)
  except urllib.error.HTTPError as exc:
   return self._parse(exc)
  except Exception as exc:
   if self.debug:
    self._dbg_error(exc)
   return 0, {"error": str(exc)}, {}

 def get(self, path: str, **kw):
  return self.request("GET", path, **kw)

 def post(self, path: str, body: dict, **kw):
  return self.request("POST", path, body=body, **kw)

 # ── internals ─────────────────────────────────────────────────
 def _parse(self, resp) -> tuple[int, dict, dict]:
  raw = resp.read().decode("utf-8", errors="replace")
  status = resp.status if hasattr(resp, "status") else resp.code
  resp_hdrs = {k: v for k, v in resp.headers.items()}

  if self.debug:
   self._dbg_response(status, resp_hdrs, raw)

  try:
   body = json.loads(raw) if raw.strip() else {}
  except json.JSONDecodeError:
   body = {"_raw": raw[:2000]}
  return status, body, resp_hdrs

 # ── debug pretty-print (thread-safe) ─────────────────────────
 def _dbg_request(self, method, url, hdrs, body):
  with self._print_lock:
   print(f"\n{_c('d', '┌──')} {_c('mag', f'{method} {url}')}")
   for k, v in hdrs.items():
    val = f"Bearer {self.api_key[:8]}…" if k == "Authorization" else v
    print(f"{_c('d', '│')} {_c('d', '→')} {k}: {val}")
   if body:
    print(f"{_c('d', '│')} {_c('d', '→ Body:')} {json.dumps(body, indent=2)[:600]}")

 def _dbg_response(self, status, hdrs, raw):
  colour = "grn" if 200 <= status < 300 else "ylw" if status == 429 else "red"
  with self._print_lock:
   print(f"{_c('d', '│')} {_c(colour, f'← {status}')}")
   for k, v in hdrs.items():
    print(f"{_c('d', '│')} {_c('d', '←')} {k}: {v}")
   print(f"{_c('d', '│')} {_c('d', '← Body:')} {raw[:500]}")
   print(_c("d", "└──"))

 def _dbg_error(self, exc):
  with self._print_lock:
   print(f"{_c('d', '│')} {_c('red', f'!! {type(exc).__name__}: {exc}')}")
   print(_c("d", "└──"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Model probe — all compatibility checks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class ModelProbe:
 def __init__(self, client: HTTPClient):
  self.client = client

 # ── 1. Fetch models ───────────────────────────────────────────
 def fetch_models(self) -> list[dict]:
  status, body, _ = self.client.get("/models")
  if status != 200:
   print(f"\n{_c('red', 'ERROR')}: /models returned HTTP {status}")
   if "error" in body:
    print(f" Detail: {body['error']}")
   sys.exit(1)
  data = body.get("data", body.get("models", []))
  if isinstance(data, list):
   return sorted(data, key=lambda m: m.get("id", ""))
  return []

 # ── 2. Token-mode detection ───────────────────────────────────
 def detect_token_mode(self, model_id: str) -> dict:
  """
  Phase 1: send both max_tokens=5 AND max_completion_tokens=10.
  · ~10 completion tokens → max_completion_tokens overrides (OpenRouter new).
  · ~5 completion tokens → max_tokens wins; go to Phase 2.
  Phase 2 (only if max_tokens won): send ONLY max_completion_tokens=5.
  · 200 → both params work, max_tokens takes priority when co-present.
  · error → legacy max_tokens only.
  """
  payload_phase1 = {
   "model": model_id,
   "messages": [{"role": "user", "content": LONG_PROMPT}],
   "max_tokens": 5,
   "max_completion_tokens": 10,
   "temperature": TEMPERATURE,
  }
  s1, r1, _ = self.client.post("/chat/completions", payload_phase1)

  # If both together are rejected, try max_tokens alone
  if s1 not in (200, 201):
   err_msg = _extract_error(r1)
   # Might be rejected because of unknown param; try max_tokens only
   payload_legacy = {
    "model": model_id,
    "messages": [{"role": "user", "content": LONG_PROMPT}],
    "max_tokens": 5,
    "temperature": TEMPERATURE,
   }
   s1b, r1b, _ = self.client.post("/chat/completions", payload_legacy)
   if s1b in (200, 201):
    return {
     "mode": "max_tokens_only",
     "param": "max_tokens",
     "note": f"Both-params request failed ({s1}: {err_msg}); max_tokens alone works",
    }
   return {
    "mode": "error",
    "param": None,
    "note": f"Chat completions failed: HTTP {s1b} — {_extract_error(r1b)}",
   }

  comp_tokens = _get_completion_tokens(r1)

  if comp_tokens is not None and comp_tokens >= 8:
   # max_completion_tokens won
   return {
    "mode": "max_completion_tokens",
    "param": "max_completion_tokens",
    "note": f"Overrides max_tokens (saw {comp_tokens} tokens)",
   }

  if comp_tokens is not None and comp_tokens <= 6:
   # max_tokens won → Phase 2: test max_completion_tokens alone
   payload_phase2 = {
    "model": model_id,
    "messages": [{"role": "user", "content": LONG_PROMPT}],
    "max_completion_tokens": 5,
    "temperature": TEMPERATURE,
   }
   s2, r2, _ = self.client.post("/chat/completions", payload_phase2)
   if s2 in (200, 201):
    ct2 = _get_completion_tokens(r2)
    if ct2 is not None and ct2 <= 6:
     return {
      "mode": "both",
      "param": "max_completion_tokens",
      "note": f"Both work; max_tokens takes priority when co-present (phase1={comp_tokens}t, phase2={ct2}t)",
     }
    return {
     "mode": "max_tokens_priority",
     "param": "max_tokens",
     "note": f"max_tokens dominates; max_completion_tokens alone yielded {ct2}t (may be converted)",
    }
   else:
    return {
     "mode": "max_tokens_only",
     "param": "max_tokens",
     "note": f"Legacy only; max_completion_tokens alone → HTTP {s2} ({_extract_error(r2)})",
    }

  # Ambiguous zone
  return {
   "mode": "ambiguous",
   "param": "max_tokens",
   "note": f"Saw {comp_tokens} completion tokens — could not determine definitively (model may have stopped early)",
  }

 # ── 3. Chat completions check ─────────────────────────────────
 def check_chat_completions(self, model_id: str, token_param: str) -> dict:
  payload = {
   "model": model_id,
   "messages": [{"role": "user", "content": "Say OK"}],
   token_param: 1,
   "temperature": TEMPERATURE,
  }
  s, r, hdrs = self.client.post("/chat/completions", payload)
  return {"supported": s in (200, 201), "status": s, "detail": _extract_error(r) if s >= 400 else None}

 # ── 4. Responses API check ────────────────────────────────────
 def check_responses_api(self, model_id: str) -> dict:
  payload = {
   "model": model_id,
   "input": "Say OK",
   "max_output_tokens": 1,
  }
  # The Responses API may live at /responses (no /v1 prefix if base already has it)
  s, r, _ = self.client.post("/responses", payload)
  return {"supported": s in (200, 201), "status": s, "detail": _extract_error(r) if s >= 400 else None}

 # ── 5. Extra body / OpenRouter params ─────────────────────────
 def check_extra_body(self, model_id: str, token_param: str) -> dict:
  """Test if reasoning_effort and other OpenRouter extras are accepted."""
  payload = {
   "model": model_id,
   "messages": [{"role": "user", "content": "Say OK"}],
   token_param: 1,
   "temperature": TEMPERATURE,
   # OpenRouter-style extra body params
   "reasoning_effort": "low",
  }
  s, r, _ = self.client.post("/chat/completions", payload)
  accepted = s in (200, 201)

  # Also test provider-specific routing
  payload_provider = {
   "model": model_id,
   "messages": [{"role": "user", "content": "Say OK"}],
   token_param: 1,
   "temperature": TEMPERATURE,
   "provider": {"order": ["Together"]},
  }
  s2, r2, _ = self.client.post("/chat/completions", payload_provider)
  provider_ok = s2 in (200, 201)

  return {
   "reasoning_effort": {"accepted": accepted, "status": s,
    "detail": _extract_error(r) if s >= 400 else None},
   "provider_routing": {"accepted": provider_ok, "status": s2,
    "detail": _extract_error(r2) if s2 >= 400 else None},
  }

 # ── 6. Full probe for one model ───────────────────────────────
 def full_probe(self, model_id: str) -> dict:
  hdr(f"━━ Probing: {_c('cyn', model_id)} ━━")

  # Step 1 — token mode
  info("Detecting token-limit mode…")
  tm = self.detect_token_mode(model_id)
  token_param = tm["param"] or "max_tokens"
  _report_token_mode(tm)

  # Step 2 — chat completions (quick, with min tokens)
  info("Checking /chat/completions…")
  cc = self.check_chat_completions(model_id, token_param)
  (ok if cc["supported"] else fail)(
   f"/chat/completions → HTTP {cc['status']}"
   + (f" ({cc['detail']})" if cc["detail"] else "")
  )

  # Step 3 — responses API
  info("Checking /responses (new API)…")
  ra = self.check_responses_api(model_id)
  (ok if ra["supported"] else fail)(
   f"/responses → HTTP {ra['status']}"
   + (f" ({ra['detail']})" if ra["detail"] else "")
  )

  # Step 4 — extra body
  info("Checking extra_body params (reasoning_effort, provider routing)…")
  eb = self.check_extra_body(model_id, token_param)
  for name, result in eb.items():
   label = name.replace("_", " ")
   (ok if result["accepted"] else fail)(
    f"{label} → HTTP {result['status']}"
    + (f" ({result['detail']})" if result["detail"] else "")
   )

  return {"model": model_id, "token_mode": tm, "chat": cc,
   "responses": ra, "extra_body": eb}

 # ── 7. Ultra-fast parallel scan ───────────────────────────────
 def ultra_fast_scan(self, model_ids: list[str], workers: int = 2) -> list[dict]:
  hdr(f"⚡ Ultra-fast scan — {len(model_ids)} models, {workers} workers")
  results = []
  lock = Lock()
  completed = [0]

  def probe_one(mid: str) -> dict:
   payload = {
    "model": mid,
    "messages": [{"role": "user", "content": "1"}],
    "max_tokens": 1,
    "temperature": TEMPERATURE,
   }
   retries = 0
   while True:
    s, r, resp_hdrs = self.client.post("/chat/completions", payload)
    if s == 429:
     retry_after = int(resp_hdrs.get("Retry-After",
      resp_hdrs.get("retry-after", "2")))
     retries += 1
     if retries > 5:
      return {"model": mid, "status": 429, "online": False,
       "note": "Rate-limited after 5 retries"}
     time.sleep(retry_after)
     continue
    break

   online = s in (200, 201)
   note = ""
   if s == 403:
    note = "Forbidden"
   elif s == 404:
    note = "Not found"
   elif s >= 500:
    note = f"Server error: {_extract_error(r)}"
   elif not online:
    note = _extract_error(r)

   with lock:
    completed[0] += 1
    pct = completed[0] / len(model_ids) * 100
    sym = _c("grn", "✓") if online else _c("red", "✗")
    status_str = _c("grn" if online else "red", str(s))
    pad = " " * max(0, 55 - len(mid))
    extra = f" {_c('d', note)}" if note else ""
    print(f"\r {sym} [{completed[0]:>{len(str(len(model_ids)))}}"
     f"/{len(model_ids)}] {mid}{pad} {status_str}{extra}")

   return {"model": mid, "status": s, "online": online, "note": note}

  with ThreadPoolExecutor(max_workers=workers) as pool:
   futures = {pool.submit(probe_one, m): m for m in model_ids}
   for future in as_completed(futures):
    results.append(future.result())

  # Summary
  on = [r for r in results if r["online"]]
  off = [r for r in results if not r["online"]]
  print()
  ok(f"{len(on)} model(s) online")
  if off:
   fail(f"{len(off)} model(s) offline or errored:")
   for r in sorted(off, key=lambda x: x["model"]):
    print(f" {_c('red', str(r['status']))} {r['model']}"
     f"{' ' + r['note'] if r['note'] else ''}")

  return sorted(results, key=lambda x: x["model"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _extract_error(body: dict) -> str:
 if isinstance(body, dict):
  err = body.get("error", "")
  if isinstance(err, dict):
   return err.get("message", str(err))
  if isinstance(err, str) and err:
   return err
  raw = body.get("_raw", "")
  if raw:
   return raw[:120]
 return str(body)[:120]


def _get_completion_tokens(resp: dict) -> int | None:
 """Extract completion token count from response; fallback to text estimation."""
 usage = resp.get("usage", {})
 ct = usage.get("completion_tokens")
 if ct is not None:
  return int(ct)
 # Fallback: estimate from text length (~4 chars per token)
 try:
  text = resp["choices"][0]["message"]["content"]
  return max(1, len(text) // 4)
 except (KeyError, IndexError, TypeError):
  return None


def _report_token_mode(tm: dict):
 mode = tm["mode"]
 if mode == "error":
  fail(f"Token detection failed: {tm['note']}")
 elif mode == "max_completion_tokens":
  ok(f"Token mode: {_c('grn', 'max_completion_tokens')} — {tm['note']}")
 elif mode == "max_tokens_only":
  warn(f"Token mode: {_c('ylw', 'max_tokens')} (legacy) — {tm['note']}")
 elif mode == "both":
  ok(f"Token mode: {_c('grn', 'both supported')} — {tm['note']}")
 elif mode == "max_tokens_priority":
  info(f"Token mode: {_c('blu', 'max_tokens priority')} — {tm['note']}")
 else:
  warn(f"Token mode: {_c('ylw', 'ambiguous')} — {tm['note']}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Interactive model selector
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def select_models(models: list[dict]) -> list[str]:
 """Pretty model picker. Supports: numbers, ranges (3-7), 'all', 'q'."""
 hdr("Available models:")
 ids = [m.get("id", m.get("name", "?")) for m in models]
 pad = len(str(len(ids)))
 for i, mid in enumerate(ids, 1):
  print(f" {_c('d', str(i).rjust(pad))}) {mid}")

 print(f"\n{_c('b', 'Select models')} — enter numbers, ranges (3-7), "
  f"comma-separated, {_c('grn', 'all')}, or {_c('red', 'q')} to quit:")
 raw = input(f" {_c('cyn', '▸')} ").strip()

 if raw.lower() in ("q", "quit", "exit"):
  sys.exit(0)
 if raw.lower() == "all":
  return ids

 selected = set()
 for part in raw.replace(" ", ",").split(","):
  part = part.strip()
  if not part:
   continue
  if "-" in part:
   lo, hi = part.split("-", 1)
   try:
    for n in range(int(lo), int(hi) + 1):
     if 1 <= n <= len(ids):
      selected.add(n - 1)
   except ValueError:
    warn(f"Invalid range: {part}")
  else:
   try:
    n = int(part)
    if 1 <= n <= len(ids):
     selected.add(n - 1)
    else:
     warn(f"Out of range: {n}")
   except ValueError:
    # Try matching by substring
    matches = [i for i, mid in enumerate(ids) if part.lower() in mid.lower()]
    if matches:
     selected.update(matches)
    else:
     warn(f"Not recognised: {part}")

 chosen = [ids[i] for i in sorted(selected)]
 if not chosen:
  fail("No models selected.")
  sys.exit(1)
 print(f"\n Selected {_c('b', str(len(chosen)))} model(s):")
 for m in chosen:
  print(f" {_c('cyn', '·')} {m}")
 return chosen


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Summary table
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def print_summary(results: list[dict]):
 hdr("━━ Summary ━━")
 # Determine column width from longest model name
 max_name = max((len(r["model"]) for r in results), default=20)
 col = max(max_name + 2, 30)

 header = (f" {'Model':<{col}} {'Token Mode':<25} {'Chat':<7} "
  f"{'Resp.':<7} {'reason.':<9} {'prov.':<7}")
 print(_c("b", header))
 print(" " + "─" * (col + 55))

 for r in sorted(results, key=lambda x: x["model"]):
  mid = r["model"]
  tm = r["token_mode"]["mode"]
  chat_ok = _c("grn", " ✓") if r["chat"]["supported"] else _c("red", " ✗")
  resp_ok = _c("grn", " ✓") if r["responses"]["supported"] else _c("red", " ✗")
  re_ok = _c("grn", " ✓") if r["extra_body"]["reasoning_effort"]["accepted"] else _c("red", " ✗")
  prov_ok = _c("grn", " ✓") if r["extra_body"]["provider_routing"]["accepted"] else _c("red", " ✗")
  print(f" {mid:<{col}} {tm:<25} {chat_ok:<7} {resp_ok:<7} {re_ok:<9} {prov_ok:<7}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def build_parser() -> argparse.ArgumentParser:
 p = argparse.ArgumentParser(
  prog="model_probe",
  description="Scan & probe models on an OpenAI-compatible endpoint.",
  formatter_class=argparse.RawDescriptionHelpFormatter,
  epilog=textwrap.dedent("""\
 examples:
 %(prog)s --base-url https://openrouter.ai/api/v1 --api-key sk-...
 %(prog)s --base-url https://openrouter.ai/api/v1 --fast --workers 20
 API_TOKEN=sk-... %(prog)s --base-url https://my.endpoint/v1 --debug
 """),
 )
 p.add_argument("--base-url", "-u", required=True,
  help="Base URL of the API (e.g. https://example.org/v1)")
 p.add_argument("--api-key", "-k",
  default=os.environ.get("API_TOKEN", ""),
  help="API key (or set API_TOKEN env var)")
 p.add_argument("--debug", "-d", action="store_true",
  help="Print full request/response headers & bodies")
 p.add_argument("--proxy", "-p",
  default=os.environ.get("HTTPS_PROXY", os.environ.get("https_proxy")),
  help="HTTP(S) proxy URL (or set HTTPS_PROXY env var)")
 p.add_argument("--user-agent", "--ua",
  default=os.environ.get("PROBE_USER_AGENT", DEFAULT_UA),
  help="Custom User-Agent string")
 p.add_argument("--timeout", "-t", type=int, default=30,
  help="HTTP timeout in seconds (default: 30)")
 p.add_argument("--no-ssl-verify", action="store_true",
  help="Skip SSL certificate verification")
 p.add_argument("--fast", "-f", action="store_true",
  help="Ultra-fast parallel scan: online/offline check only")
 p.add_argument("--workers", "-w", type=int, default=2,
  help="Parallel workers for --fast mode (default: 2)")
 p.add_argument("--all", "-a", action="store_true",
  help="Select all models (skip interactive picker)")
 p.add_argument("--version", "-V", action="version",
  version=f"%(prog)s {VERSION}")
 return p


def main():
 args = build_parser().parse_args()

 if not args.api_key:
  print(f"{_c('red', 'ERROR')}: No API key. Use --api-key or set API_TOKEN env var.")
  sys.exit(1)

 banner = f"""
{_c('b', '┌────────────────────────────────────────────────────────────────┐')}
{_c('b', '│')} {_c('cyn', 'model_probe')} v{VERSION:<40}{_c('b', '│')}
{_c('b', '│')} Endpoint: {args.base_url:<29}{_c('b', '│')}
{_c('b', '│')} Debug: {str(args.debug):<29}{_c('b', '│')}
{_c('b', '│')} Proxy: {str(args.proxy or 'none'):<29}{_c('b', '│')}
{_c('b', '│')} UA: {args.user_agent[:29]:<29}{_c('b', '│')}
{_c('b', '└────────────────────────────────────────────────────────────────┘')}"""
 print(banner)

 client = HTTPClient(
  base_url=args.base_url,
  api_key=args.api_key,
  user_agent=args.user_agent,
  proxy=args.proxy,
  debug=args.debug,
  timeout=args.timeout,
  no_ssl_verify=args.no_ssl_verify,
 )
 probe = ModelProbe(client)

 # Fetch models
 hdr("Fetching model list…")
 models = probe.fetch_models()
 ok(f"Found {len(models)} model(s)")

 if not models:
  fail("No models returned by the endpoint.")
  sys.exit(1)

 model_ids = [m.get("id", m.get("name", "?")) for m in models]

 # ── Ultra-fast mode ───────────────────────────────────────────
 if args.fast:
  probe.ultra_fast_scan(model_ids, workers=args.workers)
  return

 # ── Interactive / full-probe mode ─────────────────────────────
 if args.all:
  chosen = model_ids
  print(f"\n Auto-selected all {_c('b', str(len(chosen)))} model(s)")
 else:
  chosen = select_models(models)

 results = []
 for mid in chosen:
  result = probe.full_probe(mid)
  results.append(result)

 if len(results) > 1:
  print_summary(results)

 print(f"\n{_c('grn', 'Done.')} Probed {len(results)} model(s).\n")


if __name__ == "__main__":
 main()
