"""Scanner classes for Editor v1 — Step 2.

Per editor-spec-v1.md §3. Disclosure Delta + Threshold Crossing.

Each scanner emits CandidateBrief with salience_score = 0.0 + the metadata
fields the scorer needs in delta_summary.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from google.cloud import bigquery

from .schemas import CandidateBrief

log = logging.getLogger("hapi.editor.scanners")

_BQ_CLIENT: bigquery.Client | None = None


def _get_bq_client() -> bigquery.Client:
    global _BQ_CLIENT
    if _BQ_CLIENT is None:
        _BQ_CLIENT = bigquery.Client(project="qld2032-brain")
    return _BQ_CLIENT


# Dollar tier integer mapping. Used by both scanners.
DOLLAR_TIER_CASE = """
  CASE
    WHEN spend_range = '$100M+' THEN 5
    WHEN spend_range IN ('$50M - $100M', '$50m - $100m', '$100m - $250m', '$250m - $500m') THEN 4
    WHEN spend_range IN ('$10M - $50M', '$10m - $25m', '$25m - $50m') THEN 3
    WHEN spend_range = '$5M - $10M' THEN 2
    WHEN spend_range IN ('$1M - $5M', 'Less than $1M', '<10m') THEN 1
    ELSE 0
  END
"""

TIER_INT_TO_LABEL = {
    5: "$100M+",
    4: "$50M-$100M",
    3: "$10M-$50M",
    2: "$5M-$10M",
    1: "<$5M",
    0: "unpriced",
}


# ---- Disclosure Delta Scanner -----------------------------------------------

DISCLOSURE_DELTA_SQL = f"""
WITH baseline_agg AS (
  SELECT
    Agency,
    COUNT(*) AS baseline_total,
    SUM(CASE WHEN `Spend Range` IS NULL OR UPPER(`Spend Range`) = 'N/A' THEN 1 ELSE 0 END) AS baseline_na
  FROM `qld2032-brain.qld_procurement.raw_pipeline`
  GROUP BY Agency
),
current_agg AS (
  SELECT
    agency AS Agency,
    COUNT(*) AS current_total,
    SUM(CASE WHEN spend_range IS NULL OR UPPER(spend_range) = 'N/A' THEN 1 ELSE 0 END) AS current_na,
    MAX({DOLLAR_TIER_CASE}) AS highest_dollar_tier_int,
    MAX(CASE WHEN brisbane_2032_related = 'Yes' THEN 1 ELSE 0 END) AS has_b2032,
    MAX(chef_ingested_at) AS latest_ingested_at
  FROM `qld2032-brain.qld_procurement.chef_forward_procurement_pipeline`
  GROUP BY agency
)
SELECT
  COALESCE(b.Agency, c.Agency) AS agency,
  b.baseline_total,
  b.baseline_na,
  ROUND(SAFE_DIVIDE(b.baseline_na, b.baseline_total) * 100, 1) AS baseline_na_pct,
  c.current_total,
  c.current_na,
  ROUND(SAFE_DIVIDE(c.current_na, c.current_total) * 100, 1) AS current_na_pct,
  ROUND(
    ABS(SAFE_DIVIDE(b.baseline_na, b.baseline_total)
        - SAFE_DIVIDE(c.current_na, c.current_total)) * 100,
    1
  ) AS delta_pct,
  c.highest_dollar_tier_int,
  c.has_b2032,
  c.latest_ingested_at
FROM baseline_agg b
FULL OUTER JOIN current_agg c USING (Agency)
WHERE b.baseline_total IS NOT NULL
  AND c.current_total IS NOT NULL
  AND c.current_total >= 5
  AND ABS(
        SAFE_DIVIDE(b.baseline_na, b.baseline_total)
        - SAFE_DIVIDE(c.current_na, c.current_total)
      ) * 100 >= 50
ORDER BY delta_pct DESC
"""

DISCLOSURE_DELTA_THESIS_TEMPLATE = (
    "Between Nov 2025 and the latest release, {agency} disclosed pricing on "
    "procurement entries that previously showed N/A across {baseline_na_pct}% "
    "of {baseline_total} entries (now {current_na_pct}% of {current_total})."
)

DISCLOSURE_DELTA_DATA_SLICE_TEMPLATE = """
SELECT 'nov-2025' AS release_period, `Spend Range` AS spend_range, COUNT(*) AS n
FROM `qld2032-brain.qld_procurement.raw_pipeline`
WHERE Agency = '{agency}'
GROUP BY `Spend Range`
UNION ALL
SELECT 'may-2026' AS release_period, spend_range, COUNT(*) AS n
FROM `qld2032-brain.qld_procurement.chef_forward_procurement_pipeline`
WHERE agency = '{agency}'
GROUP BY spend_range
ORDER BY release_period, n DESC
"""


# ---- Threshold Crossing Scanner ---------------------------------------------

THRESHOLD_CROSSING_SQL = f"""
WITH new_entries AS (
  SELECT
    c.agency,
    c.spend_range,
    c.brisbane_2032_related,
    c.chef_ingested_at,
    {DOLLAR_TIER_CASE} AS dollar_tier_int
  FROM `qld2032-brain.qld_procurement.chef_forward_procurement_pipeline` c
  WHERE NOT EXISTS (
    SELECT 1 FROM `qld2032-brain.qld_procurement.raw_pipeline` b
    WHERE LOWER(b.`Program Description`) = LOWER(c.program_description)
      AND b.Agency = c.agency
  )
)
SELECT
  agency,
  COUNT(*) AS new_total,
  SUM(CASE WHEN brisbane_2032_related = 'Yes' THEN 1 ELSE 0 END) AS new_b2032,
  SUM(CASE WHEN dollar_tier_int >= 3 THEN 1 ELSE 0 END) AS new_big_ticket,
  MAX(dollar_tier_int) AS highest_dollar_tier_int,
  MAX(CASE WHEN brisbane_2032_related = 'Yes' THEN 1 ELSE 0 END) AS has_b2032,
  MAX(chef_ingested_at) AS latest_ingested_at
FROM new_entries
WHERE dollar_tier_int >= 3 OR brisbane_2032_related = 'Yes'
GROUP BY agency
HAVING new_total >= 3
ORDER BY new_total DESC
"""

THRESHOLD_CROSSING_THESIS_TEMPLATE = (
    "{new_total} new procurement opportunities have appeared in the {agency} "
    "pipeline since Nov 2025, with {new_b2032} flagged for Brisbane 2032 and "
    "{new_big_ticket} in the {top_tier_label} band or above."
)

THRESHOLD_CROSSING_DATA_SLICE_TEMPLATE = """
SELECT spend_range, brisbane_2032_related, COUNT(*) AS n
FROM `qld2032-brain.qld_procurement.chef_forward_procurement_pipeline` c
WHERE agency = '{agency}'
  AND NOT EXISTS (
    SELECT 1 FROM `qld2032-brain.qld_procurement.raw_pipeline` b
    WHERE LOWER(b.`Program Description`) = LOWER(c.program_description)
      AND b.Agency = c.agency
  )
GROUP BY spend_range, brisbane_2032_related
ORDER BY n DESC
"""


# Pre-registered brief_ids. Adding here also requires BRIEF_ALLOWLIST in server.py.
_REGISTERED_BRIEF_IDS = {
    ("disclosure_delta", "Stadiums Qld"): "stadium-pricing-delta",
    ("threshold_crossing", "Dept of Transport and Main Roads"): "tmr-brisbane-2032-emergence",
}


def _slugify(text: str) -> str:
    out, prev_hyphen = [], False
    for ch in text.lower().strip():
        if ch.isalnum():
            out.append(ch); prev_hyphen = False
        elif not prev_hyphen:
            out.append("-"); prev_hyphen = True
    return "".join(out).strip("-")


def _make_brief_id(scanner: str, agency: str) -> str:
    if (scanner, agency) in _REGISTERED_BRIEF_IDS:
        return _REGISTERED_BRIEF_IDS[(scanner, agency)]
    date_yyyymm = datetime.now(timezone.utc).strftime("%Y%m")
    return f"{scanner}_{_slugify(agency)}_{date_yyyymm}"


def _iso(ts) -> str | None:
    """Convert a BQ timestamp (datetime or string) to ISO-8601 string for JSON."""
    if ts is None:
        return None
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


class DisclosureDeltaScanner:
    """Detects material spend_range disclosure rate shifts per agency.

    Per editor-spec-v1.md §3.1.
    """

    name = "disclosure_delta"

    def scan(self, domains: list[str]) -> list[CandidateBrief]:
        if "forward_procurement" not in domains:
            return []

        client = _get_bq_client()
        log.info("disclosure_delta scan starting")
        rows = list(client.query(DISCLOSURE_DELTA_SQL).result())
        log.info("disclosure_delta retrieved %d agencies", len(rows))

        candidates = []
        for row in rows:
            agency = row["agency"]
            thesis = DISCLOSURE_DELTA_THESIS_TEMPLATE.format(
                agency=agency,
                baseline_na_pct=row["baseline_na_pct"],
                baseline_total=row["baseline_total"],
                current_na_pct=row["current_na_pct"],
                current_total=row["current_total"],
            )
            data_slice = DISCLOSURE_DELTA_DATA_SLICE_TEMPLATE.format(agency=agency)
            candidates.append(CandidateBrief(
                brief_id=_make_brief_id(self.name, agency),
                thesis=thesis,
                scanner=self.name,
                domain="forward_procurement",
                suggested_data_slice_sql=data_slice.strip(),
                delta_summary={
                    "agency": agency,
                    "baseline_total": row["baseline_total"],
                    "baseline_na": row["baseline_na"],
                    "baseline_na_pct": float(row["baseline_na_pct"] or 0.0),
                    "current_total": row["current_total"],
                    "current_na": row["current_na"],
                    "current_na_pct": float(row["current_na_pct"] or 0.0),
                    "delta_pct": float(row["delta_pct"] or 0.0),
                    "highest_dollar_tier_int": int(row["highest_dollar_tier_int"] or 0),
                    "has_brisbane_2032": bool(row["has_b2032"]),
                    "latest_ingested_at": _iso(row["latest_ingested_at"]),
                },
            ))
        return candidates


class ThresholdCrossingScanner:
    """Detects clusters of new entries above critical thresholds.

    Per editor-spec-v1.md §3.2.
    """

    name = "threshold_crossing"

    def scan(self, domains: list[str]) -> list[CandidateBrief]:
        if "forward_procurement" not in domains:
            return []

        client = _get_bq_client()
        log.info("threshold_crossing scan starting")
        rows = list(client.query(THRESHOLD_CROSSING_SQL).result())
        log.info("threshold_crossing retrieved %d clusters", len(rows))

        candidates = []
        for row in rows:
            agency = row["agency"]
            tier_int = int(row["highest_dollar_tier_int"] or 0)
            tier_label = TIER_INT_TO_LABEL.get(tier_int, "unknown")
            thesis = THRESHOLD_CROSSING_THESIS_TEMPLATE.format(
                agency=agency,
                new_total=row["new_total"],
                new_b2032=row["new_b2032"],
                new_big_ticket=row["new_big_ticket"],
                top_tier_label=tier_label,
            )
            data_slice = THRESHOLD_CROSSING_DATA_SLICE_TEMPLATE.format(agency=agency)
            candidates.append(CandidateBrief(
                brief_id=_make_brief_id(self.name, agency),
                thesis=thesis,
                scanner=self.name,
                domain="forward_procurement",
                suggested_data_slice_sql=data_slice.strip(),
                delta_summary={
                    "agency": agency,
                    "new_total": row["new_total"],
                    "new_b2032": row["new_b2032"],
                    "new_big_ticket": row["new_big_ticket"],
                    "highest_dollar_tier_int": tier_int,
                    "top_tier_label": tier_label,
                    "has_brisbane_2032": bool(row["has_b2032"]),
                    "latest_ingested_at": _iso(row["latest_ingested_at"]),
                },
            ))
        return candidates


# Hardcoded registry per spec §6 + §10.1
REGISTERED_SCANNERS = [
    DisclosureDeltaScanner(),
    ThresholdCrossingScanner(),
]
