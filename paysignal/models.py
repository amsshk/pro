"""The one standard format every payment is converted into, whatever its source."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class LineItem:
    sku: str
    name: str
    quantity: int
    unit_price: float
    unit_cost: Optional[float] = None  # for margin; None = unknown


@dataclass
class Payment:
    """A normalized payment or refund.

    kind: "purchase" or "refund". Refunds carry a positive amount and point at
    the original payment through `refund_of`.
    """
    client_id: str            # which agency client (store) this belongs to
    source: str               # "stripe", "shopify", "csv", ...
    external_id: str          # id in the source system (dedup key)
    kind: str                 # "purchase" | "refund"
    amount: float             # in major units, e.g. 129.99
    currency: str             # ISO code, e.g. "AED"
    occurred_at: datetime
    email: Optional[str] = None
    phone: Optional[str] = None
    customer_id: Optional[str] = None
    fbclid: Optional[str] = None      # Meta click id, if captured
    ttclid: Optional[str] = None      # TikTok click id, if captured
    discount: float = 0.0
    refund_of: Optional[str] = None   # external_id of the original purchase
    items: list[LineItem] = field(default_factory=list)
    channel: str = "website"          # "website" | "physical_store" | "system_generated"
    # Organic attribution: tagged links (UTMs) and promo codes tied to posts
    utm_source: Optional[str] = None    # e.g. "instagram", "tiktok"
    utm_medium: Optional[str] = None    # e.g. "organic", "paid"
    utm_campaign: Optional[str] = None
    utm_content: Optional[str] = None   # the post/story/reel id
    promo_code: Optional[str] = None
    # Browser details, when the checkout captured them (improve Meta matching)
    client_user_agent: Optional[str] = None
    client_ip_address: Optional[str] = None
    event_source_url: Optional[str] = None
    fbp: Optional[str] = None             # Meta browser id (_fbp cookie), when the checkout passed it on
    fbc: Optional[str] = None             # Meta click id cookie (_fbc), as captured on the visit
    ttp: Optional[str] = None             # TikTok browser id (_ttp cookie), when the checkout passed it on
    # Address details from the order, used only as hashed match keys for Meta
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    city: Optional[str] = None
    zip: Optional[str] = None
    country: Optional[str] = None         # two-letter code

    @property
    def event_id(self) -> str:
        """Stable id so the same payment is never counted twice by Meta/TikTok."""
        return f"{self.client_id}:{self.source}:{self.external_id}"

    def cost_of_goods(self) -> Optional[float]:
        if not self.items or any(i.unit_cost is None for i in self.items):
            return None
        return sum(i.unit_cost * i.quantity for i in self.items)
