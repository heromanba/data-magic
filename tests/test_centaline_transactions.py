import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scrape_centaline_transactions.py"
SPEC = importlib.util.spec_from_file_location("scrape_centaline_transactions", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TestCentalineTransactions(unittest.TestCase):
    def test_normalize_row_maps_first_hand_and_cleans_values(self):
        row = {
            "id": " 123 ",
            "detailUrl": "https://example.test/abc",
            "districtName": "  North Point  ",
            "estateName": "  ",
            "buildingName": "  Ka Bo Building  ",
            "address": " 102-108 Chun Yeung Street ",
            "postType": "S",
            "transactionPrice": "3800000",
            "gArea": "691",
            "gUnitPrice": "8883",
            "nArea": "577",
            "nUnitPrice": "10638",
            "insDate": "2026-08-08T00:00:00",
            "firstOrSecondHand": "FirstHand",
            "dataSource": "AC",
            "estateType": "Normal",
            "unitType": "AP",
            "scope": {"scp_mkt": "HK", "terr": "港島", "db": "南區", "hma": "深灣"},
            "bldgGrp": {"bldgGrpId": "BG1", "bldgGrpName": "Group A"},
            "specialCase": {"value": "Test"},
        }

        normalized = MODULE.normalize_row(row, query_name="sale_first_hand", offset=0)

        self.assertEqual(normalized["id"], "123")
        self.assertEqual(normalized["post_type"], "Sale")
        self.assertEqual(normalized["first_or_second_hand"], "FirstHand")
        self.assertEqual(normalized["district_name"], "North Point")
        self.assertEqual(normalized["building_name"], "Ka Bo Building")
        self.assertEqual(normalized["ins_date"], "2026-08-08")
        self.assertEqual(normalized["transaction_price"], 3800000.0)
        self.assertEqual(normalized["gross_area_sqft"], 691.0)
        self.assertEqual(normalized["scope_market"], "HK")
        self.assertEqual(normalized["building_group_name"], "Group A")

    def test_parse_date_replaces_placeholder_and_handles_missing_value(self):
        self.assertIsNone(MODULE.parse_date(None))
        self.assertEqual(MODULE.parse_date("1900-01-01"), None)
        self.assertEqual(MODULE.parse_date("2026-08-08T00:00:00"), "2026-08-08")
        self.assertEqual(MODULE.parse_date("2025/06/26"), "2025-06-26")

    def test_calculate_page_count_respects_total_rows_and_limit(self):
        self.assertEqual(MODULE.calculate_page_count(0, 100, 5), 0)
        self.assertEqual(MODULE.calculate_page_count(250, 100, 5), 3)
        self.assertEqual(MODULE.calculate_page_count(1000, 100, 5), 5)


if __name__ == "__main__":
    unittest.main()
