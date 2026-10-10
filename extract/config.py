"""Shared settings for the extractor."""
import os

RESOURCES = ("accounts", "contacts", "deals", "campaigns", "usage")
DEFAULT_BASE_URL = os.getenv("CRM_BASE_URL", "http://localhost:8000")
