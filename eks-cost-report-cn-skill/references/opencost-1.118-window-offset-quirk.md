# OpenCost 1.118 Window/Offset Quirk

## Symptom

On OpenCost 1.118 (running in EKS chris-eks cn-northwest-1), the `offset` query parameter on `/allocation/compute` is silently ignored when paired with `window=1d`.

All of these return the **same** window (today's date):

```bash
curl ".../allocation/compute?window=1d&offset=1d&includeIdle=true"
curl ".../allocation/compute?window=1d&offset=2d&includeIdle=true"
curl ".../allocation/compute?window=1d&offset=3d&includeIdle=true"
# window: 2026-07-16T00:00:00Z to 2026-07-17T00:00:00Z (TODAY)
```

This breaks the common `--window 1d --offset 1d` pattern that should return yesterday's data.

## Root Cause

The `1d` window on this OpenCost version appears to be a **daily fixed window aligned to the current Prometheus scrape**, not a rolling 24-hour window. The `offset` parameter is accepted but not honored.

## Working Pattern

Use `window=24h` (numeric hour form) instead of `window=1d`:

```bash
# YESTERDAY: 24-hour window ending 24h ago
curl ".../allocation/compute?window=24h&offset=24h&includeIdle=true"
# window: 2026-07-15T03:00:00Z to 2026-07-16T03:00:00Z (YESTERDAY) ✓

curl ".../allocation/compute?window=24h&offset=1d&includeIdle=true"
# Same result - 1d and 24h offset values are equivalent when window is 24h
```

The start time is offset by the scrape alignment (typically 03:00 UTC in this cluster). Plan reports around that offset.

## Other Working Combos

| Goal | window | offset | Notes |
|------|--------|--------|-------|
| Last 1 hour | `1h` | `1h` | Returns the previous hour |
| Last 24 hours (today) | `24h` | `0d` | Default current 24h window |
| Yesterday | `24h` | `24h` | or `24h&offset=1d` — both work |
| Last 2 days | `2d` | — | Returns 2-day window ending now |

## How To Verify

Always check the `window` field in the response to confirm you got the window you expected:

```python
import json, urllib.request
url = "http://localhost:9003/allocation/compute?window=24h&offset=24h&includeIdle=true"
with urllib.request.urlopen(url, timeout=30) as resp:
    raw = json.loads(resp.read().decode())
alloc = next(iter(raw["data"][0].values()))
print(alloc["window"])
# {'start': '2026-07-15T03:00:00Z', 'end': '2026-07-16T03:00:00Z'}
```

If the returned window doesn't match expectations, switch from `1d` to `24h` form before anything else.

## Affected Versions

Confirmed on OpenCost 1.118.0 (chris-eks, 2026-07). Behavior may differ on newer versions — always check on first run of the session.