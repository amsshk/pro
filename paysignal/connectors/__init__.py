"""Connectors turn each payment source's own format into a normalized Payment.

Adding a new source (PayPal, Tabby, Square...) = one new module with a parse
function returning list[Payment]. Nothing else in the engine changes.
"""
from .stripe import parse_stripe_event
from .shopify import parse_shopify_webhook
from .csv_upload import parse_csv

__all__ = ["parse_stripe_event", "parse_shopify_webhook", "parse_csv"]
