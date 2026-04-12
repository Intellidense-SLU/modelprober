# model_probe

`model_probe.py` is a zero-dependency Python CLI for scanning an OpenAI-compatible API endpoint, listing its models, and probing how compatible each model is with common request patterns.

It is built entirely on the Python standard library. No `requests`, no SDK, no install step.

## What It Does

For a given OpenAI-style base URL, `model_probe.py` can:

- Fetch available models from `GET /models`
- Interactively select models to test, or probe them all
- Detect whether a model expects `max_tokens`, `max_completion_tokens`, or accepts both
- Check `POST /chat/completions`
- Check `POST /responses`
- Check whether extra OpenRouter-style body fields are accepted:
  - `reasoning_effort`
  - `provider` routing hints
- Run a fast parallel "is this model online?" sweep across all models
- Print colored terminal output when stdout is a TTY

## Requirements

- Python 3.10+
- An OpenAI-compatible API endpoint
- An API key for that endpoint

## Quick Start

```bash
API_TOKEN=sk-... python3 model_probe.py --base-url https://example.org/v1
```

Or:

```bash
python3 model_probe.py --base-url https://example.org/v1 --api-key sk-...
```

Fast scan mode:

```bash
python3 model_probe.py --base-url https://example.org/v1 --api-key sk-... --fast --workers 20
```

Debug mode:

```bash
python3 model_probe.py --base-url https://example.org/v1 --api-key sk-... --debug
```

## How Base URLs Work

Pass the API base path that should receive:

- `/models`
- `/chat/completions`
- `/responses`

Example:

- `https://openrouter.ai/api/v1`
- `https://your-host.example/v1`

The script strips a trailing `/` automatically.

## CLI Options

```text
--base-url, -u        Required. Base API URL.
--api-key, -k         API key. Falls back to API_TOKEN.
--debug, -d           Print request/response details.
--proxy, -p           Proxy URL. Falls back to HTTPS_PROXY / https_proxy.
--user-agent, --ua    Custom User-Agent. Falls back to PROBE_USER_AGENT.
--timeout, -t         Request timeout in seconds. Default: 30.
--no-ssl-verify       Disable TLS certificate verification.
--fast, -f            Run online/offline checks only.
--workers, -w         Worker count for --fast mode. Default: 2.
--all, -a             Probe all models without interactive selection.
--version, -V         Print version and exit.
```

## Environment Variables

```text
API_TOKEN         API key fallback for --api-key
PROBE_USER_AGENT  User-Agent fallback for --user-agent
HTTPS_PROXY       Proxy fallback for --proxy
NO_COLOR          Disable ANSI colors
```

Color is also disabled automatically when stdout is not a TTY.

## Probe Flow

In normal mode, the script:

1. Fetches `/models`
2. Lets you choose models by number, range, substring match, `all`, or `q`
3. For each selected model:
   - detects token-limit parameter behavior
   - checks `/chat/completions`
   - checks `/responses`
   - checks extra request-body parameters
4. Prints a summary table when more than one model was probed

In `--fast` mode, it skips the deeper checks and sends a minimal `/chat/completions` request per model to classify models as online or failing.

## Notes on Compatibility Detection

The token-limit probe is based on actual responses, not static assumptions:

- If a model clearly honors `max_completion_tokens`, it is reported as such
- If it only accepts `max_tokens`, it is reported as legacy
- If both appear accepted, the script reports which one wins when both are present
- If the model stops too early to infer behavior reliably, the result is marked `ambiguous`

This means results are practical and endpoint-specific, but still heuristic in some edge cases.

## Examples

Probe all models:

```bash
python3 model_probe.py \
  --base-url https://openrouter.ai/api/v1 \
  --api-key sk-... \
  --all
```

Use a proxy and custom user agent:

```bash
python3 model_probe.py \
  --base-url https://example.org/v1 \
  --api-key sk-... \
  --proxy http://127.0.0.1:8080 \
  --user-agent "model_probe/1.0"
```

Disable TLS verification for a lab endpoint:

```bash
python3 model_probe.py \
  --base-url https://internal.example/v1 \
  --api-key sk-... \
  --no-ssl-verify
```

## Limitations

- The script assumes OpenAI-like JSON endpoints and payload shapes
- `POST /responses` is checked at `BASE_URL + /responses`
- Completion-token detection falls back to rough text-length estimation when usage metadata is missing
- Network errors are surfaced directly from `urllib`
- There is no machine-readable output mode yet

## License

This project is released under [The Unlicense](LICENSE), placing it in the public domain where possible and otherwise granting broad permission to use, modify, distribute, and sell it without restriction.
