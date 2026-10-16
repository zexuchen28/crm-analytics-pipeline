"""Contract between the raw JSON payloads and the dbt staging models.

The raw layer stores every CRM record as JSON, so nothing breaks at load time
when the CRM changes. That moves the risk to staging. These tests catch the two
ways staging can drift from the source, without needing dbt or a warehouse:

  1. a staging model reads a field the CRM does not have (typo, rename, removal)
  2. the CRM has a field that no staging model reads (a new field nobody staged)

Run:  python -m unittest discover -s tests -t . -v
"""
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mock_crm_api.seed import build_dataset
from mock_crm_api.store import CRMStore

STAGING_DIR = Path(__file__).resolve().parent.parent / "dbt_project" / "models" / "staging"
JSON_FIELD = re.compile(r"json_value\(\s*'payload'\s*,\s*'(\w+)'\s*\)")

# Real columns on the raw tables, not parsed out of the JSON payload.
RAW_COLUMNS = {"id", "updated_at"}
# Deliberately not parsed in staging; carried in raw_payload for a later layer.
INTENTIONALLY_UNPARSED = {"deals": {"stage_history"}}


class StagingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = build_dataset(seed=1, n_accounts=40, now=datetime(2026, 9, 1, tzinfo=timezone.utc))
        store = CRMStore(data)
        store.mutate(n_updates=5, n_new=3, now=datetime(2026, 9, 2, tzinfo=timezone.utc), seed=2)
        cls.records = store.data

    def test_every_resource_has_a_staging_model(self):
        for resource in self.records:
            with self.subTest(resource=resource):
                self.assertTrue((STAGING_DIR / f"stg_{resource}.sql").exists())

    def test_staging_reads_only_fields_the_source_has(self):
        for resource, rows in self.records.items():
            sql = (STAGING_DIR / f"stg_{resource}.sql").read_text()
            referenced = set(JSON_FIELD.findall(sql))
            source_fields = set().union(*(r.keys() for r in rows))
            with self.subTest(resource=resource):
                self.assertEqual(referenced - source_fields, set(),
                                 "staging reads fields that do not exist in the source")

    def test_every_source_field_is_staged_or_explicitly_skipped(self):
        for resource, rows in self.records.items():
            sql = (STAGING_DIR / f"stg_{resource}.sql").read_text()
            referenced = set(JSON_FIELD.findall(sql))
            source_fields = set().union(*(r.keys() for r in rows))
            unstaged = source_fields - referenced - RAW_COLUMNS - INTENTIONALLY_UNPARSED.get(resource, set())
            with self.subTest(resource=resource):
                self.assertEqual(unstaged, set(), "source has fields that no staging model reads")


if __name__ == "__main__":
    unittest.main()
