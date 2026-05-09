from datetime import datetime


# ---------------------------------------------------------------------------
# Input shape (fields that influence the score):
#
#   extracted (dict) — output from the parsing stage:
#     - "days"            list[str]   Days of week happy hour runs
#     - "start_time"      str | None  Start time (e.g. "16:00")
#     - "end_time"        str | None  End time (e.g. "19:00")
#     - "deals_text"      str | None  Raw deals description
#     - "structured_deals" list[dict] Parsed deal objects (item, price, discount)
#     - "source"          str         Origin: "website" | "yelp" | "instagram"
#
#   sources (list[dict]) — one entry per scraped source:
#     - "source"          str         Same source tag as above
#     - "raw_text"        str         Raw text scraped from that source
#     - "extracted"       dict        Per-source extraction result
#
#   scraped_at (datetime | None) — UTC timestamp of the scrape run;
#     used for recency decay.
# ---------------------------------------------------------------------------


def calculate_confidence(
    extracted: dict,
    sources: list[dict],
    scraped_at: datetime | None = None,
) -> float:
    """Deterministic confidence score in the range [0.0, 1.0].

    Weighs source count, cross-source agreement, field completeness
    (times, days, prices), and recency of the scrape. Returns a rounded
    float suitable for storage and display.
    """
    raise NotImplementedError("Proprietary scoring algorithm")
