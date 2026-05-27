# OpenCost Data Latency and Partial Window Issue

## Discovery

Date: 2026-05-26
OpenCost version: 1.118.0
Cluster: EKS (chris-eks, cn-northwest-1)

## Symptom

OpenCost returns `minutes` far less than the query window duration, even when nodes have been running continuously for weeks.

Example for `window=1h&offset=1h`:
- Expected: 60 minutes
- Actual: ~22–24 minutes
- Window end: 06:23:40Z (current time ~06:24Z)

Example for `window=1d`:
- Expected: 1440 minutes
- Actual: ~383 minutes
- Window: 00:00:00Z ~ 06:23:40Z (only 6.4 hours of data)

## Root Cause Analysis

This is **not** a node restart issue. Nodes were confirmed running for 26–27 days without restart.

The actual cause is **OpenCost data availability / latency**. The `end` timestamp in the response reflects the newest data OpenCost has computed, not the current wall-clock time.

Possible causes:
1. **OpenCost pod restart** — resets internal caches; only accumulates data since restart
2. **Prometheus data gaps** — missing metrics prevent OpenCost from computing allocation for the full window
3. **OpenCost computation delay** — the ETL pipeline may lag behind real time

## Diagnostic Steps

### 1. Check OpenCost Pod Age
```bash
kubectl get pods -n opencost -o wide
```
If the pod is young (e.g., 4h45m), it may only have accumulated data since it started.

### 2. Check Window End Time
```bash
curl -s "http://localhost:9003/assets?window=1h" | jq '.data[] | select(.type=="Node") | {name: .properties.name, start: .start, end: .end, minutes: .minutes}'
```
If `end` is close to current time but `minutes` is still short, the issue is data latency.
If `end` is significantly behind current time, OpenCost computation is delayed.

### 3. Compare Multiple Offsets
```bash
for offset in 0d 1d 2d; do
  curl -s "http://localhost:9003/assets?window=1d&offset=$offset" | \
    jq -r '.data[] | select(.type=="Node") | "\(.properties.name): \(.minutes) min"'
done
```
If all offsets show the same `end` time, OpenCost data is stuck at a specific point.

## Impact

- Cost reports show incomplete costs for recent windows
- Hourly rate calculations may appear inflated if dividing by partial runtime
- Idle cost percentages may be skewed

## Workarounds

1. **Use longer windows** (e.g., `window=1d` or `window=7d`) where partial data has less relative impact
2. **Check data completeness before reporting**:
   ```python
   minutes = alloc.get("minutes", 0)
   expected_minutes = window_hours * 60
   completeness = minutes / expected_minutes * 100
   if completeness < 90:
       print(f"Warning: data only {completeness:.1f}% complete")
   ```
3. **Wait for data to settle** before querying recent windows
4. **For real-time monitoring**, use Prometheus metrics directly instead of OpenCost allocation API

## Related

- `references/node-runtime-vs-window.md` — node runtime vs query window (covers node restart case)
- `references/aws-china-pricing-currency-bug.md` — AWS China pricing currency issue
