# OpenCost AWS China Pricing Currency Bug

## Discovery

Date: 2026-05-26
OpenCost version: 1.118.0
Cluster: EKS (chris-eks, cn-northwest-1)
Node type: t3a.large (2 vCPU, 8 GB RAM)

## Symptom

OpenCost-reported node costs are ~7.25x higher than expected when using AWS China pricing.

| Source | Value |
|--------|-------|
| AWS China pricing API (CNY) | ¥0.345 / hr |
| OpenCost Assets API (USD) | $0.345 / hr |
| Correct USD equivalent | $0.0476 / hr (= ¥0.345 ÷ 7.25) |

## Root Cause

AWS China pricing files list prices in **CNY** (e.g. `pricePerUnit: {"CNY": "0.3450000000"}`).

OpenCost interprets these numeric values as **USD**, ignoring the currency unit. This causes all AWS China costs to be inflated by the USD→CNY exchange rate (~7.25x).

## Verification

### 1. AWS Pricing API Response

```bash
curl -s "https://pricing.cn-northwest-1.amazonaws.com.cn/offers/v1.0/cn/AmazonEC2/current/index.json"
```

Relevant excerpt for SKU `4A8HPJKCAMEVEPN5` (t3a.large Linux Ningxia):
```json
{
  "priceDimensions": {
    "4A8HPJKCAMEVEPN5.5Y9WH78GDR.Q7UJUT2CE6": {
      "unit": "Hrs",
      "pricePerUnit": {"CNY": "0.3450000000"},
      "description": "0.345 CNY per On Demand Linux t3a.large Instance Hour"
    }
  }
}
```

### 2. OpenCost Assets API Response

```bash
curl -s "http://localhost:9003/assets?window=1d&offset=1d"
```

Node asset shows:
- `cpuCost`: $1.277
- `ramCost`: $0.655
- `totalCost`: $1.933
- `minutes`: 336.15 (= 5.60 hr)
- Hourly rate: $1.933 ÷ 5.60 = **$0.345/hr**

The hourly rate matches the AWS pricing number (0.345) but in the wrong currency.

### 3. Internal Rate Breakdown

From OpenCost asset fields:
- CPU rate: $0.114 / core-hr (= ¥0.114 / core-hr in AWS file)
- RAM rate: $0.01528 / GB-hr (= ¥0.01528 / GB-hr in AWS file)
- Combined: 2×$0.114 + 8×$0.01528 = **$0.345/hr**

## Impact on Reports

| Metric | OpenCost Shows | Actual (Corrected) |
|--------|---------------|-------------------|
| 3 nodes × 5.6 hr | ¥42.16 CNY | ¥5.81 CNY |
| 3 nodes × 24 hr | ¥180.09 CNY | ¥24.84 CNY |
| Per-node hourly | ¥2.50 CNY | ¥0.345 CNY |

## Workarounds

### Option A: Treat OpenCost output as CNY directly (Recommended)

Since OpenCost reads CNY prices but returns them as raw numbers, the simplest fix is to treat the output values as CNY instead of converting from USD:

```python
# In generate_report.py
CNY_RATE = 1.0  # OpenCost AWS China returns CNY values directly
```

This is the default in the current script. The report displays values in CNY without additional conversion.

### Option B: Custom pricing CSV

Override OpenCost's AWS pricing with a custom CSV in USD:

```csv
InstanceID,Region,InstanceType,OperatingSystem,Price
i-xxx,cn-northwest-1,t3a.large,Linux,0.0476
```

Mount into OpenCost container and set `CUSTOM_PRICING_URL`.

### Option C: Accept and annotate

Keep OpenCost values as-is but add a disclaimer in reports:

> **Note**: OpenCost interprets AWS China CNY prices as USD. Displayed costs are ~7.25× actual CNY values. Use the ratio for trend analysis, not absolute billing.

## Current Script Behavior

The report script (`scripts/generate_report.py`) uses **Option A** by default:
- `CNY_RATE = 1.0` — treats OpenCost numeric output as CNY
- Report subtitle shows `AWS China (CNY)` instead of `1 USD = 7.25 CNY`
- For non-China regions, override with `--cny-rate 7.25`

## Related Files

- `scripts/generate_report.py` — report script that consumes OpenCost API
- `references/opencost-idle-api-behavior.md` — companion note on idle cost API behavior
