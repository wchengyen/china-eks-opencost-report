# OpenCost Idle API Behavior

## Discovery

Date: 2026-05-26
OpenCost version: 1.118.0
Cluster: EKS (chris-eks, cn-northwest-1)

## Key Finding

The per-allocation fields `cpuCostIdle` and `ramCostIdle` **always return 0**, regardless of:
- `IDLE_ENABLED=true` environment variable on the OpenCost deployment
- API parameters like `idle=true`, `idle=share`, `idle=separate`, `idleByNode=true`
- Endpoint used (`/allocation/compute` or `/allocation/summary`)

## How Idle Cost Is Actually Returned

Idle cost is returned as a **separate allocation entry with the key `__idle__`** when the API parameter `includeIdle=true` is present.

### Without `includeIdle=true`

```json
{
  "code": 200,
  "data": [
    {"namespace/pod/container": {...}},
    ...
  ]
}
```

Total entries: 58 (for this cluster)
Total cost: ~$2.29

### With `includeIdle=true`

```json
{
  "code": 200,
  "data": [
    {"namespace/pod/container": {...}},
    ...,
    {"__idle__": {
      "cpuCost": 1.8768,
      "ramCost": 1.4826,
      "totalCost": 3.3594,
      "minutes": 317.6,
      "properties": {"cluster": "cluster-one"}
    }}
  ]
}
```

Total entries: 59 (adds the `__idle__` entry)
Total cost: ~$5.65

## Implementation Notes

- The `__idle__` entry has `cpuCostIdle: 0` and `ramCostIdle: 0` even within itself
- The actual idle costs are in `cpuCost` and `ramCost` fields of the `__idle__` entry
- `minutes` field indicates partial-day measurement (317.6 min ≈ 5.3 hours)
- The `__idle__` entry has empty `namespace`, `pod`, `node` properties

## API Parameters Tested (All Ineffective for Per-Allocation Idle)

| Parameter | Result |
|-----------|--------|
| `idle=true` | No effect on `cpuCostIdle`/`ramCostIdle` |
| `idle=share` | No effect |
| `idle=shareByNode` | No effect |
| `idle=separate` | No effect |
| `idle=separateByNode` | No effect |
| `idleByNode=true` | No effect |
| `includeIdle=true` | **Effective** — adds `__idle__` entry |

## Recommended Approach

1. Always use `includeIdle=true` in API calls
2. Extract the `__idle__` entry separately from regular allocations
3. Use `__idle__.cpuCost` and `__idle__.ramCost` for native idle cost
4. Optionally also calculate per-node request-usage gap estimates for detailed breakdown
