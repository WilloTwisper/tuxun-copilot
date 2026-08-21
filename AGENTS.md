# AGENTS.md

Automated GeoGuessr-style helper for [tuxun.fun](https://tuxun.fun): fetches 4-direction streetview images for a game ID, sends them to Google Gemini, and prints a structured location guess with a Google Maps link.

## Layout

- `main.py` — entrypoint and orchestration. `initialize()` creates services; `main()` owns the CLI loop. Gemini prompt, `RESULT_SCHEMA` (JSON mode) and result formatting live here.
- `config.py` — immutable environment configuration (`AppConfig`).
- `tuxun_client.py` — authenticated tuxun.fun API client and game ID parsing.
- `streetview.py` — unauthenticated Google Street View URL generation and isolated downloads.
- `models.py` — typed game state and round domain objects.
- `gemini_router.py` — model/key failover with isolated state.
- `clash_controller.py` — optional Clash/mihomo control-API client for auto node failover (`ClashController`).

## Monitor mode (auto round-following)

- `main.py` runs `preflight_checks()` at startup (download a probe thumbnail + text Gemini call; on failure auto-switch node once), then `monitor_game()` per ID: polls `TuxunClient.get_game()` every 2s, analyzes each new `GameRound.number` once, and exits back to the input loop when `status != "ongoing"`.
- Rationale: the game URL stays the same across rounds; the right moment to read is when a new round's `panoId` actually appears in the API (reading too early gets the previous round). No per-round pasting needed.
- If a finished game is pasted, monitor analyzes the last round once then returns. Ctrl+C in monitor returns to input (outer Ctrl+C quits).
- `TuxunClient.get_game(game_id)` returns a typed `GameState` (challenge→solo fallback, includes explicit `mode`). `TuxunClient.current_user_id()` validates the Cookie. The monitor consumes `GameRound` objects instead of raw API dictionaries.

## API key rotation

- `.env`: optional `API_KEY2` — a second Gemini key used when the primary hits 429 (rate limit) or 403 (project denied). `main.py` builds one `genai.Client` per key and `call_gemini()` cycles through model/key combinations on 429/403; non-rate-limit errors still raise so the caller's retry/Clash-switch logic applies.
- **Model fallbacks** (`GEMINI_MODEL_FALLBACKS`, comma-separated): free-tier quota is per-model-per-project, so when the primary model 429s, `GeminiRouter` rotates through (model, key) combinations. A 404 disables only that model/key combination for the session; a successful combination remains selected until it fails.
- **403-permanently-dead keys are marked (`dead_key_idx`) and never tried again this session**; 429 keeps cycling live keys (2 rounds) then raises `所有可用 Gemini Key 均被限流（429）`.
- **Key-level errors (429/403) must NOT trigger node switching** — quota is not a network problem. `analyze_round` waits ~20s (per-minute window) then retries once; the retry loop uses fixed 20s backoff on key errors vs `attempt*2` on network errors. Free-tier daily quota exhaustion (all-call 429) only resets at midnight PT.

## Node auto-failover (Clash)
- `.env`: `CLASH_CONTROLLER` (e.g. `http://127.0.0.1:9097`) + `CLASH_SECRET`; requires the External Controller toggle in Clash Verge settings. Without it everything silently downgrades to today's behavior.
- Flow: `pick_and_switch()` → resolve which selector groups the Gemini API / streetview domains route to (first matching rule in `/rules`, then walk `Selector` chains to the deepest manually switchable group) → parallel `/proxies/{node}/delay` tests → `PUT /proxies/{group}` for the fastest live node.
- **Delay probe must be a real streetview thumbnail (~130KB), NOT a 204 stub** — a node can have great RTT but terrible throughput. Probe URL in `clash_controller.py` as `STREETVIEW_PROBE_URL`.
- **Clash's own delay test can lie for us**: mihomo's test runs its own TLS stack (uTLS browser fingerprint); a node can pass its probe yet Python/OpenSSL gets SSL EOF on streetviewpixels. Hence `analyze_round` escalates: probe-best switch → retry → **`pick_and_switch(exclude_current=True)` forced rotation** → retry → give up (max 3 attempts).
- **Node blacklist**: real failures (download all-fail, slow download >8s, Gemini network errors) call `blacklist_nodes()` so the probe doesn't keep re-picking a node that looks fast but actually breaks (mihomo probe can show 28ms while Python gets connection resets). Blacklist expires (300s default), `pick_and_switch` skips blacklisted nodes entirely.
- **Two-stage probing in `pick_and_switch`**: mihomo `/proxies/{node}/delay` is only a coarse parallel filter (its own TLS stack and RTT-oriented result); the top-5 candidates are then probed for real with Python `requests` through the Clash mixed port (`_real_probe`, per-group URL: streetview thumbnail for the streetview group, `GET /v1beta/models` for the Gemini group — TLS handshake is what matters, 401 is success). This eliminates candidates whose probe latency looks healthy but whose real Python traffic is slow or broken.
- Triggered from `main.py` when image download takes >8s or the first Gemini attempt fails. One switch sweep costs ~13s (21 nodes, 5s test timeout each) — acceptable once-per-failure, not per-round.
- Delay tests return `None` for unreachable nodes — don't treat as crash. **`PUT` to a selector returns 204 with empty body — judge success by status code, not `bool(body)`.**

## Analysis schemes (PvP balance)

- Two prompts in `main.py`: `ANALYSIS_PROMPT_FAST` (reasoning ≤50 chars, ~16s) vs `ANALYSIS_PROMPT_PRECISE` (multi-line reasoning, ~29s). Gemini latency scales with output tokens — that's why the fast one is ~45% quicker.
- Scheme is chosen by `GameState.mode`: UUID challenge games → `precise` (daily challenge has 3 min/round, time is ample), solo/party PvP → `fast`.
- Do NOT use `max_output_tokens` to speed up the fast scheme — it truncates the JSON mid-`Here is the JSON requested:`.

## Run / verify

- No tests, no lint, no CI. Verification = syntax compilation plus running `main.py` with a live cookie.
- Run with `python -X utf8 -u main.py` (or the equivalent interpreter path for the active environment).
- Offline tests: `python -m unittest discover -s tests -v`.
- `-X utf8` matters: all output/comments are Chinese; without it console printing can mis-encode on Windows.
- Requires `.env` (copied from `.env.example`): `API_KEY`, `TUXUN_COOKIE`, optional `PROXY`, `GEMINI_MODEL`. Never commit `.env`.

## Gotchas (hard-earned)

- **Proxy routing is split on purpose**: `StreetViewClient` receives the optional Google proxy explicitly, while `TuxunClient` forces `trust_env = False` so tuxun.fun **always goes direct**. Do not unify them — it breaks either Google access (blocked in CN) or tuxun latency.
- **Streetview images 403 without `Referer: https://maps.google.com/`** header — every download path must send it.
- Cookie validity is checked via `GET /api/v0/tuxun/user/getSelfProfile`; the old `/api/get_profile` endpoint is dead.
- **Two game-data endpoints, split by ID shape**: short IDs (solo/party, e.g. `Ge9g36p53Vf2yAL9`) → `GET /api/v0/tuxun/solo/get?gameId=`. UUID IDs are ambiguous — daily challenges (`/challenge/<uuid>`) use `GET /api/v0/tuxun/challenge/getGameInfo?challengeId=` but solo/party games ALSO get UUIDs, so `get_pano_id` tries challenge first then falls back to solo on `unknown`. Both return `data.rounds[].panoId`.
- **`data.get('data')` can be `null` (not missing)** on API errors like `need_login`/`unknown` — `.get(key, {})` defaults don't apply, so guard with `body.get('success')` before touching `rounds`.
- **Parallel downloads intentionally use one fresh Session per worker** — shared connection reuse triggers SSL EOF through Clash proxy. Don't "optimize" back to a shared session.
- Image size defaults to 640x480 deliberately: larger images cause SSL EOF when fetched through the proxy.
- Gemini calls retry 3x for proxy flakiness — keep the retry when touching that code.
- Game ID parsing: `TuxunClient.parse_game_id` handles bare IDs and full URLs (`/solo/`, `/party/`, `/challenge/`, etc. or `?id=`/`gameId=`/`challengeId=` query params) and validates the result once.
- Python 3.10+ is required because the code uses PEP 604 unions and built-in generic types.

## Conventions

- Comments, prompts, and console output are in Chinese — keep new code that way.
- `requests.Session` + `urllib3 Retry` for tuxun API calls; plain `requests.get` per download worker.
- Keep user-facing failure messages in Chinese with `错误：`/`警告：` prefixes.

## Independence boundary

- The repository history is intentionally a clean rewrite. Do not reintroduce
  files or commits from the historical reference project without a deliberate
  provenance and license review.
