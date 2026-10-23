from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

from hermes_constants import get_hermes_home
from providers import register_provider
from providers.base import ProviderProfile

# ── PayPerQ published rate card ──────────────────────────────────────────────
# http://api.ppq.ai is a relay endpoint, so Hermes' bundled price table and the
# models.dev fallback deliberately decline to price it (a custom base_url never
# inherits the vendor's list price). The provider's own published API rates at
# https://ppq.ai/pricing are the authoritative source, so this profile reads a
# local snapshot of that page — refreshed every two days by the
# `ppq-price-watch` cron job running ~/.hermes/scripts/ppq_price_watch.py —
# and prices each response from it.
#
# Snapshot path: $HERMES_HOME/plugin-data/ppq/prices.json (per ACTIVE profile;
# resolved at call time via _plugin_data_dir(), never frozen at import)
#   { "fetched_at": iso8601, "source": url,
#     "models": { "<model id>": {"input": usd_per_1M|null, "output": ...,
#                                "cache_read": ..., "ui_input": ...,
#                                "name": ..., "provider": ...} } }


def _plugin_data_dir() -> str:
    """Per-profile plugin state dir: <active hermes home>/plugin-data/ppq.

    get_hermes_home() follows the active context/profile (context-local
    override -> HERMES_HOME env -> platform default), so every profile reads
    its own files instead of the launch profile's. Resolved per call, never
    at import.
    """
    return os.path.join(str(get_hermes_home()), "plugin-data", "ppq")


def _prices_path() -> str:
    return os.path.join(_plugin_data_dir(), "prices.json")


_PRICES_TTL_S = 300.0
# Cache keyed by resolved path: one process can serve several profiles via
# context-local overrides, so a profile-A table must never be served to B.
_prices_cache: tuple[float, str, dict, dict] | None = None
_prices_meta: dict = {}


def load_ppq_prices(path: str | None = None) -> dict:
    """Current PayPerQ chat-model rate table, memoized for a few minutes.

    Returns {} when no snapshot exists yet or it is unreadable — callers must
    fall back to "unknown cost" rather than inventing a rate. Snapshot
    metadata (fetch time, source URL) lands in the module-level _prices_meta.
    """
    global _prices_cache, _prices_meta
    target = path or _prices_path()
    now = time.monotonic()
    if _prices_cache and _prices_cache[0] > now and _prices_cache[1] == target:
        _prices_meta = _prices_cache[3]
        return _prices_cache[2]
    table: dict = {}
    meta: dict = {}
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            models = data.get("models")
            table = models if isinstance(models, dict) else {}
            meta = {k: data.get(k) for k in ("fetched_at", "source", "model_count")}
    except Exception:
        table, meta = {}, {}
    _prices_cache = (now + _PRICES_TTL_S, target, table, meta)
    _prices_meta = meta
    return table


# ── ZDR (Zero Data Retention) + no-data-collection opt-in ────────────────────
# Over the ppq API, privacy routing is opt-in per request via a ``provider``
# body object (https://ppq.ai/api-docs → Zero Data Retention). For badged
# models this plugin sends ``{"zdr": true, "data_collection": "deny"}``:
# zdr routes only to upstream endpoints with a Zero Data Retention policy,
# data_collection=deny additionally excludes providers that may store data.
# GET /v1/models works unauthenticated and tags every chat model with a
# ``privacyLevel`` ("anon" | "zdr" | "e2e"), so the ZDR-capable set is
# refreshed here on a 1 h TTL. Requests for models badged "zdr" get the
# opt-in automatically; every other model is left untouched, because
# requesting zdr on a model with no ZDR endpoint fails to route entirely.
# (e2e models are deliberately excluded — they carry the stronger TEE
# guarantee and may have no ZDR-classified endpoint to route to.)
_ZDR_REFRESH_TTL_S = 3600.0
_zdr_model_ids: set[str] | None = None
_zdr_last_attempt = 0.0
_ZDR_FALLBACK_IDS = frozenset({
    # Verified against GET /v1/models privacyLevel on 2026-10-21; used only
    # while the live refresh is unavailable.
    "z-ai/glm-5.3-flash",
    "deepseek/deepseek-v4.1-flash",
})


def _zdr_known_models(base_url: str | None) -> frozenset[str]:
    """Model ids badged privacyLevel='zdr'; one live attempt per TTL, best effort."""
    global _zdr_model_ids, _zdr_last_attempt
    now = time.monotonic()
    if now - _zdr_last_attempt < _ZDR_REFRESH_TTL_S:
        return frozenset(_zdr_model_ids) if _zdr_model_ids else frozenset(_ZDR_FALLBACK_IDS)
    _zdr_last_attempt = now
    url = f"{(base_url or 'https://api.ppq.ai/v1').rstrip('/')}/models"
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        ids = {
            m.get("id")
            for m in data.get("data", [])
            if isinstance(m, dict) and m.get("privacyLevel") == "zdr" and m.get("id")
        }
        if ids:
            _zdr_model_ids = ids
    except Exception:
        pass  # keep serving the previous set, or the pinned fallback
    return frozenset(_zdr_model_ids) if _zdr_model_ids else frozenset(_ZDR_FALLBACK_IDS)


# ── Model picker pins: 3-layer strategy ─────────────────────────────────────
# This plugin is published for generic Hermes installs, so the author's
# personal pins must NOT ship as defaults. The picker's curated block
# (``fallback_models``, merged ahead of the live /v1/models list, tuple
# order = picker order) resolves in layers:
#
#   Layer A — user-local overrides: $HERMES_HOME/plugin-data/ppq/pins.json
#             (a JSON list of model ids, or {"models": [...]}, non-empty).
#             Present and valid → pins come ENTIRELY from this file. This is
#             how the author — or any user — keeps personal pins (including
#             private/* e2e ids that only exist for them) while installing
#             the plugin from the catalog channel. The file lives OUTSIDE
#             the plugin dir on purpose: installs overwrite that directory.
#   Layer B — dynamic defaults: the top chat models of the last 7 days are
#             fetched from ppq's public usage dashboard (stats.ppq.ai, a
#             Plotly Dash app; the data comes over its Dash update protocol,
#             POST /_dash-update-component — semi-fragile, hence only one
#             layer among three). Cached 1 h on success; a failed fetch is
#             retried at most every 10 min and simply yields fewer pins.
#   Layer C — static generic fallback: a small curated list of broadly
#             popular chat models, used when A is absent AND B has no fresh
#             answer. Deliberately NOT the author's personal flavor;
#             private/* e2e ids never appear here.
#
# With no pins.json and the stats endpoint down, a fresh install sees only
# Layer C ahead of the live catalog — a safe start, not the author's setup.
def _pins_path() -> str:
    return os.path.join(_plugin_data_dir(), "pins.json")


_PINS_TTL_S = 60.0
_pins_cache: tuple[float, str, list[str] | None] | None = None

_STATS_TOP_URL = "https://stats.ppq.ai/_dash-update-component"
_STATS_TOP_TTL_S = 3600.0        # successful fetch cached 1 h
_STATS_TOP_RETRY_S = 600.0       # after a failure, wait 10 min before retrying
_STATS_TOP_LIMIT = 6             # keep the picker's curated block small
_stats_cache: tuple[float, list[str]] | None = None
_stats_last_attempt = 0.0
_stats_thread: threading.Thread | None = None
_stats_thread_lock = threading.Lock()

_GENERIC_FALLBACK_MODELS: tuple[str, ...] = (
    # Layer C — curated from stats.ppq.ai "Top Chat Models (Last 7 Days)"
    # (verified live 2026-10-21); chat tier, no private/* e2e ids, no
    # author-only flavor. Edited deliberately rarely, when the landscape
    # shifts.
    "google/gemini-2.5-flash-lite",
    "deepseek/deepseek-v4.1-flash",
    "z-ai/glm-5.3-flash",
    "google/gemini-2.5-flash",
)
_MODEL_ID_RE = re.compile(r"^[a-z0-9][\w.\-]*/[\w.\-]+$")


def _load_pins_override() -> list[str] | None:
    """Layer A: user-local pins.json. None = absent/empty/invalid → generic layers."""
    global _pins_cache
    now = time.monotonic()
    target = _pins_path()
    if _pins_cache and _pins_cache[0] > now and _pins_cache[1] == target:
        return _pins_cache[2]
    pins: list[str] | None = None
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
        raw = data.get("models") if isinstance(data, dict) else data
        if isinstance(raw, list):
            cleaned = [m.strip() for m in raw if isinstance(m, str) and m.strip()]
            pins = cleaned or None
    except FileNotFoundError:
        pass
    except Exception:
        pass  # malformed file → treated as absent rather than breaking the picker
    _pins_cache = (now + _PINS_TTL_S, target, pins)
    return pins


def _fetch_stats_top_models(timeout: float = 4.0) -> list[str]:
    """Layer B fetch: POST ppq's Dash update protocol for the top-chat list."""
    payload = {
        "output": "top-models-list.children",
        "outputs": {"id": "top-models-list", "property": "children"},
        "inputs": [{"id": "refresh", "property": "n_intervals", "value": 0}],
        "changedPropIds": ["refresh.n_intervals"],
    }
    try:
        import urllib.request

        req = urllib.request.Request(
            _STATS_TOP_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "ppq-hermes-plugin/0.3.1",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        kids = data["response"]["top-models-list"]["children"]["props"]["children"]
        ids: list[str] = []
        for li in kids if isinstance(kids, list) else []:
            inner = li.get("props", {}).get("children") if isinstance(li, dict) else None
            spans = inner["props"]["children"] if isinstance(inner, dict) else inner
            if not isinstance(spans, list):
                continue
            texts = [
                s["props"]["children"]
                for s in spans
                if isinstance(s, dict) and isinstance(s.get("props", {}).get("children"), str)
            ]
            mid = next((t.strip() for t in texts if _MODEL_ID_RE.fullmatch(t.strip())), None)
            if mid and not mid.startswith("private/") and mid not in ids:
                ids.append(mid)
        return ids
    except Exception:
        return []


def _stats_top_models() -> list[str] | None:
    """Layer B read: serves only a fresh cache; a miss kicks a background refresh.

    Never blocks the picker build: the 4 s Dash POST runs on a daemon thread,
    so no picker build ever pays the network stall (Hermes builds pickers for
    every provider, including ones nobody picks). Bounded: at most one refresh
    in flight, at most one attempt per _STATS_TOP_RETRY_S after a failure.
    The picker serves Layer C until the refresh lands (next build), then B.
    """
    global _stats_last_attempt
    now = time.monotonic()
    if _stats_cache and _stats_cache[0] > now:
        return _stats_cache[1]
    if now - _stats_last_attempt >= _STATS_TOP_RETRY_S:
        _stats_last_attempt = now
        _kick_stats_refresh()
    return None


def _kick_stats_refresh() -> None:
    """Start the background Layer B fetch; no-op while one is in flight."""
    global _stats_thread
    with _stats_thread_lock:
        if _stats_thread is not None and _stats_thread.is_alive():
            return
        _stats_thread = threading.Thread(target=_stats_refresh_job, daemon=True)
        _stats_thread.start()


def _stats_refresh_job() -> None:
    """Background body of the Layer B fetch; fills the cache on success."""
    global _stats_cache, _stats_last_attempt
    now = time.monotonic()
    _stats_last_attempt = now
    ids = _fetch_stats_top_models()
    if ids:
        _stats_cache = (now + _STATS_TOP_TTL_S, ids[:_STATS_TOP_LIMIT])


class PPQProfile(ProviderProfile):
    # Layer A content, set via the fallback_models setter below; None = the
    # profile ships no hard pins of its own (the published default).
    _pins_override: list[str] | None = None

    @property
    def fallback_models(self) -> tuple:
        """Curated picker block, resolved in layers: A → B → C (see above)."""
        override = _load_pins_override()
        if override is not None:
            return tuple(override)
        top = _stats_top_models()
        pins = list(top) if top else []
        for m in _GENERIC_FALLBACK_MODELS:
            if m not in pins:
                pins.append(m)
        return tuple(pins)

    @fallback_models.setter
    def fallback_models(self, value: Any) -> None:
        # The base dataclass's generated __init__ assigns its field default ()
        # on every instantiation — that is NOT a pin override, so ignore it.
        # A non-default tuple counts as a hard override (kept for programmatic
        # use; the published profile passes none).
        if value is None or value == ():
            return
        self._pins_override = list(value)

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        supports_reasoning: bool = False,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        model_id = str(context.get("model") or "")
        if model_id and model_id in _zdr_known_models(context.get("base_url")):
            # Per-request privacy opt-in (ZDR + no data collection); a
            # caller-supplied extra_body_additions "provider" object merges
            # later and still wins if it must.
            extra_body["provider"] = {"zdr": True, "data_collection": "deny"}
        if supports_reasoning:
            if reasoning_config is not None:
                extra_body["reasoning"] = dict(reasoning_config)
            else:
                extra_body["reasoning"] = {"enabled": True, "effort": "medium"}
        return extra_body, {}

    def get_usage_cost(self, model: str, usage: Any):
        """Price one PayPerQ response from the published API rate card.

        Without this hook every ppq session stays ``cost_status='unknown'``
        (no bundled entry, no first-party fallback), so state.db, `hermes
        insights` and the cost readouts all show nothing. Rates are per 1M
        tokens; ``usage`` is a CanonicalUsage. ppq's API reports no cost field
        of its own, so the result is honestly marked ``estimated`` with the
        published page as its source — never ``actual``.
        """
        from decimal import Decimal

        rates = None
        table = load_ppq_prices()
        for key in (model, str(model or "").strip(), str(model or "").split("/")[-1]):
            entry = table.get(key) if table else None
            if isinstance(entry, dict):
                rates = entry
                break
        if not rates:
            return None
        r_in, r_out, r_cr = rates.get("input"), rates.get("output"), rates.get("cache_read")

        def _d(v):
            return None if v is None else Decimal(str(v))

        d_in, d_out, d_cr = _d(r_in), _d(r_out), _d(r_cr)
        if d_in is None or d_out is None:
            return None  # 'Variable' priced router model (auto/autoclaw) — stay unknown

        def _tokens(*names: str) -> int:
            for n in names:
                v = getattr(usage, n, None)
                if isinstance(v, (int, float)):
                    return int(v)
            return 0

        cache_read = _tokens("cache_read_tokens")
        cache_write = _tokens("cache_write_tokens")
        amount = (
            Decimal(_tokens("input_tokens")) * d_in
            + Decimal(_tokens("output_tokens")) * d_out
            + Decimal(cache_read) * (d_cr if d_cr is not None else d_in)
            # ppq publishes no cache-write rate; it bills the first write as
            # input tokens, so price it there rather than dropping the cost.
            + Decimal(cache_write) * d_in
        ) / Decimal(1_000_000)

        from agent.usage_pricing import CostResult, format_cost_label

        notes = [
            "PayPerQ API rates from https://ppq.ai/pricing (local snapshot, refreshed every 2 days).",
            "ppq reports no per-response cost; this is an estimate from the published rate card.",
        ]
        if cache_read and d_cr is None:
            notes.append("cache-read rate unavailable — priced at the input rate.")
        if cache_write:
            notes.append("cache-write priced at the input rate (no published cache-write rate).")

        return CostResult(
            amount_usd=amount,
            status="estimated",
            source="official_docs_snapshot",
            label=format_cost_label(amount),
            pricing_version=(
                f"ppq.ai/pricing@{_prices_meta.get('fetched_at') or 'snapshot'}"
            ),
            notes=tuple(notes),
        )

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
        **kwargs: Any,
    ) -> list[str] | None:
        import urllib.request
        import json

        url = f"{base_url or self.base_url}/models"
        req = urllib.request.Request(url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            models = data.get("data", [])
            return [
                m["id"]
                for m in models
                if isinstance(m, dict) and m.get("type") == "chat"
            ]
        except Exception:
            return None


ppq = PPQProfile(
    name="ppq",
    aliases=("payperq", "ppq-inference"),
    display_name="PayPerQ",
    description="PayPerQ — OpenAI-compatible inference API",
    signup_url="https://payperq.ai",
    env_vars=("PPQ_API_KEY", "PPQ_BASE_URL"),
    base_url="https://api.ppq.ai/v1",
    auth_type="api_key",
    api_mode="chat_completions",
    default_aux_model="z-ai/glm-5.3-flash",
    default_headers={"User-Agent": "ppq-hermes-plugin/0.3.1"},
    # No hardcoded fallback_models: the curated picker block is layered at
    # runtime — user pins.json (A) → stats.ppq.ai top chat models (B) →
    # generic static list (C). See the "Model picker pins" section above.
)

register_provider(ppq)
