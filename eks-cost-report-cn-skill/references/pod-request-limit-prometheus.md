# Pod Request / Limit from Prometheus

OpenCost `/allocation/compute` does **not** expose pod-level `limit` fields. It only provides request/usage averages on a per-allocation basis. To show Pod Tag cost tables with **CPU / memory request and limit** values, fetch the metrics directly from Prometheus (kube-state-metrics).

## Metrics Used

| Metric | Labels | Value |
|--------|--------|-------|
| `kube_pod_container_resource_requests` | `pod`, `namespace`, `resource` (cpu or memory), `unit` | request value per container |
| `kube_pod_container_resource_limits` | `pod`, `namespace`, `resource` (cpu or memory), `unit` | limit value per container |

## Aggregation Logic

1. Query `kube_pod_container_resource_requests` and `kube_pod_container_resource_limits` via `/api/v1/query`.
2. For each result, extract `metric.pod`, `metric.namespace`, `metric.resource`.
3. Sum container-level values **per pod**:
   - `resource="cpu"` → `cpu_request` / `cpu_limit`
   - `resource="memory"` → `mem_request` / `mem_limit`
4. In the report aggregation loop, look up each allocation's pod in the pod-resources map.
5. Add the pod's request/limit to the corresponding **Tag** group.

## Request / Limit Cost Estimation

In addition to showing raw request/limit quantities, the report can estimate what the cost would be if every pod ran at full request or limit for the entire time window. This is useful for sizing and capacity planning.

How it works:
1. Derive a per-node unit price from OpenCost's actual measured allocations:
   - `cpuPrice = Σ(cpuCost) / Σ(cpuCoreHours)`
   - `ramPrice = Σ(ramCost) / Σ(ramByteHours)`
2. For each allocation, read OpenCost's `minutes` field (defaults to 1440 if missing) and convert to hours: `hours = minutes / 60.0`.
3. Multiply each pod's request/limit by the node price and the window duration:
   - `cpuReqCost = cpu_request × hours × cpuPrice`
   - `cpuLimCost = cpu_limit × hours × cpuPrice`
   - `memReqCost = mem_request × hours × ramPrice`
   - `memLimCost = mem_limit × hours × ramPrice`
4. Roll up by Pod Tag.

Important notes:
- These are **estimates based on current node prices**, not forecasts of future billing.
- The estimate assumes the pod runs for the entire window at full request/limit. In practice, pods may run partial windows or throttle, so actual cost is usually lower.
- **Actual CPU cost / actual RAM cost** columns still come directly from OpenCost and are the ground truth.
- If Prometheus is unreachable, request/limit and estimated costs show `0`.

## Important Notes

- These are **current point-in-time** request/limit snapshots, not window averages. Pod restarts or HPA changes during the window won't be reflected.
- Memory values are usually bytes; convert to GiB for display (`val / 1024**3`).
- CPU values are plain cores (e.g. `0.5` = 0.5 vCPU).
- If a pod has no limit, the value is `0` (OpenCost also reports `0.000` for unlimited CPU / memory).
- `<untagged>` pods still get request/limit values as long as the pod name is found in Prometheus.
- If Prometheus is unreachable, the script warns and shows `0` for request/limit columns. Cost columns still work because they come from OpenCost.

## Port-forward Setup

Prometheus is usually exposed only inside the cluster. Use kubectl port-forward from the local machine:

```bash
kubectl port-forward svc/prometheus 9090:9090 -n monitoring --address 127.0.0.1
```

Verify with:

```bash
curl -s 'http://localhost:9090/api/v1/query?query=kube_pod_container_resource_requests' | head -c 200
```

## CLI Usage

```bash
# Default: assumes Prometheus is at http://localhost:9090
python3 scripts/generate_report.py --window 1d --offset 1d --cny-rate 1.0

# Override Prometheus URL
python3 scripts/generate_report.py --window 1d --offset 1d --cny-rate 1.0 --prometheus-url http://prometheus.monitoring.svc:9090
```

## Troubleshooting

- **Empty request/limit columns**: Check that kube-state-metrics is running and scraping `kube_pod_container_resource_*` metrics.
- **Connection refused**: Confirm Prometheus port-forward is active and the URL matches.
- **Memory values look wrong**: Confirm conversion from bytes to GiB; Prometheus returns bytes for memory resources.
