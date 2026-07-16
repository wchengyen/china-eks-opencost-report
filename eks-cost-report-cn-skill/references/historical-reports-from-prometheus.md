# Historical Reports (>24h ago, before yesterday)

## Problem

OpenCost 1.118's `/allocation/compute` API only retains the most recent ~24-48h of data. Queries older than that (e.g. "give me the report for 2026-07-14 when today is 2026-07-16") return an empty or wrong window. Even `window=24h&offset=48h` only goes back 24-48h.

For a true historical report (any day in the past), you must **recompute the cost directly from Prometheus** rather than asking OpenCost.

## What You Have Available

OpenCost itself exposes its pricing model to Prometheus as a separate metric. This is the key insight — you don't need AWS pricing data, you read OpenCost's own price assumptions.

| Metric | Meaning | Example value (t3a.large, cn-northwest-1) |
|--------|---------|---|
| `node_cpu_hourly_cost` | CNY per core-hour, per node | `0.114002` |
| `node_ram_hourly_cost` | CNY per GiB-hour, per node | `0.01528` |
| `node_total_hourly_cost` | CNY per hour for the whole node | `0.345` |

Plus from `kube-state-metrics` and `cAdvisor` (already scraped by Prometheus):

| Metric | Meaning | Type |
|--------|---------|------|
| `container_cpu_usage_seconds_total` | Cumulative core-seconds used | Counter |
| `container_memory_working_set_bytes` | Current memory usage | Gauge |
| `kube_pod_container_resource_requests` | Request per container | Gauge |
| `kube_pod_container_resource_limits` | Limit per container | Gauge |

## Cost Formula

For each container in the target 24h window:

```
cpu_core_hours = (last_value - first_value) / 3600   # counter delta
cpu_cost       = cpu_core_hours * node_cpu_hourly_cost (per node)

mem_gib_hours  = avg(container_memory_working_set_bytes) / 1024^3 * 24
ram_cost       = mem_gib_hours * node_ram_hourly_cost (per node)
```

The labels on `container_cpu_usage_seconds_total` and `container_memory_working_set_bytes` include `instance` (which IS the node), so per-node pricing lookup is straightforward.

## What the Historical Report Has vs. Doesn't Have

| Cost Component | Historical (Prometheus) | Normal (OpenCost API) |
|----------------|------------------------|----------------------|
| Actual CPU + RAM per pod | ✓ | ✓ |
| Per-pod Request / Limit | ✓ (current snapshot at noon on target date) | ✓ |
| Request / Limit cost estimates | ✓ | ✓ |
| Pod labels (for tag grouping) | Uses CURRENT labels (assumed stable) | ✓ |
| Per-container breakdown | ✓ | ✓ |
| **Idle cost (`__idle__`)** | ✗ (no allocation data → no idle entry) | ✓ |
| **PV / LB / Network cost** | ✗ (not derived from these Prom metrics) | ✓ |
| **Per-Node idle breakdown** | ✗ | ✓ |

For historical reports, the "actual" cost is the CPU+RAM usage cost for that day only. Total cost will be lower than the OpenCost version because PV/LB/Network/idle are not included.

## Why Pod Labels Use Current State

OpenCost is the only source that exposes pod labels in a way the script can use. Since labels rarely change, the script queries today's `allocation/compute` to get the pod → labels mapping, then applies it to historical data. If a workload has been completely removed, its historical data is orphan-labeled `<unknown>`.

## Workflow

1. **Port-forward Prometheus** (and OpenCost if you want current pod → labels mapping):
   ```bash
   ./scripts/port-forward-both.sh start
   ```

2. **Verify Prometheus has data for the target date**:
   ```bash
   curl -s 'http://localhost:9090/api/v1/query_range?query=up&start=2026-07-14T00:00:00Z&end=2026-07-15T00:00:00Z&step=1h'
   ```

3. **Run the historical report generator**:
   ```bash
   python3 scripts/generate_historical_report.py 2026-07-14 /home/ubuntu/opencost-report-chris-eks-2026-07-14.html
   ```

4. The script:
   - Fetches current pod→labels from OpenCost
   - Pulls 24h of CPU/memory usage for the target date from Prometheus
   - Pulls node CPU/RAM hourly price at the target date from Prometheus
   - Builds a synthetic allocation list and feeds it into the same `generate_report.py` aggregation/HTML pipeline

## Troubleshooting

- **"No data for X"** — the target date is before Prometheus retention started. Check `up` metric at the start of the target date.
- **Total cost seems low** — that's expected. Without PV / LB / Network / idle, only CPU+RAM usage is counted. Use the OpenCost API report for the same date if it's within the last 24-48h.
- **Some pods missing from report** — they have no `container_cpu_usage_seconds_total` or `container_memory_working_set_bytes` samples in the target window (likely DaemonSet pods that don't report usage, or pods created during the window).
- **Labels look wrong** — workload was renamed since the target date. Cross-check with the deployment manifests.

## Why Not Just Use OpenCost's `/customcost` or `/assets` Endpoints?

OpenCost 1.118 doesn't expose them, and even if it did, they require pre-configured custom price sheets. Recomputing from Prometheus is the most direct path for one-off historical reports.
