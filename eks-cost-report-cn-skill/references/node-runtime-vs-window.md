# OpenCost Node Runtime vs Query Window

## Discovery

Date: 2026-05-26
OpenCost version: 1.118.0
Cluster: EKS (chris-eks, cn-northwest-1)
Query window: 1d (24 hours)

## Symptom

OpenCost Assets API reports node `minutes` ≈ 332–336 min (~5.5–5.6 hours) for a 1-day window, instead of the expected 1440 min (24 hours).

## Verification

```bash
curl -s "http://localhost:9003/assets?window=1d&offset=1d"
```

Node asset response:
```json
{
  "minutes": 336.15,
  "cpuCoreHours": 11.18,
  "ramByteHours": 45963311437.13,
  "cpuCost": 1.277,
  "ramCost": 0.655,
  "totalCost": 1.933
}
```

Hourly rate check: $1.933 ÷ 5.60 hr = **$0.345/hr** — the pricing is correct, but the runtime is short.

## Root Cause

OpenCost calculates node runtime from **Prometheus metrics availability**, not calendar time. If:
- Prometheus was down or restarted
- The node joined the cluster mid-window
- Metrics scraping was interrupted
- OpenCost pod itself was restarted

Then `minutes` reflects only the period with valid metrics, not the full 24 hours.

## Impact on Cost Calculation

| Scenario | Calculation | Result |
|----------|-------------|--------|
| Full 24 hr (theoretical) | 3 × $0.345 × 24 | $24.84 = ¥180.09 |
| Actual measured (5.6 hr) | 3 × $0.345 × 5.6 | $5.81 = ¥42.16 |

**Do not assume `window=1d` means 24 hours of cost.** Always check the `minutes` field.

## How to Check

### Assets API (recommended)
```bash
curl -s "http://localhost:9003/assets?window=1d&offset=1d" | jq '.data[] | select(.type=="Node") | {name: .properties.name, minutes: .minutes, hours: (.minutes/60), totalCost: .totalCost}'
```

### Allocation API
```bash
curl -s "http://localhost:9003/allocation/compute?window=1d&offset=1d&aggregate=node" | jq '.data[] | to_entries[] | {node: .value.properties.node, minutes: .value.minutes}'
```

## Best Practices for Reports

1. **Display actual runtime** alongside the query window:
   ```
   Query window: 2026-05-26 00:00 UTC ~ 2026-05-27 00:00 UTC (24 hr)
   Measured runtime: 5.6 hr per node
   ```

2. **Use `minutes` for hourly rate validation**, not for cost projection:
   ```python
   hourly_rate = total_cost / (minutes / 60)
   # Should match the expected instance price
   ```

3. **Flag short runtime** in reports if `minutes < 1380` (23 hours):
   ```python
   if minutes < 1380:
       alert = f"Node runtime only {minutes/60:.1f} hr — metrics may be incomplete"
   ```

4. **For billing reconciliation**, use AWS Cost Explorer or CUR (Cost and Usage Report), not OpenCost metrics-based estimates.

## Related

- `references/aws-china-pricing-currency-bug.md` — companion note on CNY/USD pricing mismatch
- `references/opencost-idle-api-behavior.md` — companion note on idle cost API behavior
