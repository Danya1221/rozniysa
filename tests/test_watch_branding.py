import unittest

from prices import parse_documents


class WatchBrandingTests(unittest.TestCase):
    def test_galaxy_and_oneplus_watches_never_become_apple_watch(self):
        items = parse_documents(["""Galaxy Watch Ultra (2025) 47mm LTE Silver 🇦🇪 — 27300
Galaxy Watch Ultra (2025) 47mm LTE White 🇦🇪 — 26800
Galaxy Watch Ultra (2025) 47mm LTE White 🇦🇪 — 26800
OnePlus Watch Lite Black Steel OPWWE262 🇪🇺 — 10300
OnePlus Watch Lite Silver Steel OPWWE262 🇪🇺 — 10300"""]).items
        self.assertEqual([item.block for item in items], ["Samsung", "Samsung", "OnePlus", "OnePlus"])

    def test_real_apple_watch_still_reads_with_and_without_apple_prefix(self):
        direct = parse_documents(["Apple Watch Series 11 46mm Black — 40000\nWatch S10 46mm Silver — 35000"]).items
        self.assertEqual([item.block for item in direct], ["Apple Watch", "Apple Watch"])

        sectioned = parse_documents(["""Apple Watch
Series 11 46mm Black — 40000
SE 3 44mm Silver — 30000
Ultra 3 49mm Black — 70000"""]).items
        self.assertEqual([item.block for item in sectioned], ["Apple Watch"] * 3)

    def test_supplier_apple_watch_shorthand_is_recognized(self):
        items = parse_documents(["""SE3 40 Midnight(S/M) 2025 🇺🇸 - 19600
SE3 44 Midnight(M/L) 2025 🇺🇸 - 21400
S7 41 Midnight 🇺🇸 - 18000
S8 45 Starlight 🇺🇸 - 22000
S9 41 Pink 🇺🇸 - 24000
S10 46 Silver 🇺🇸 - 27000
S11 42 Jet Black(S/M) 🇺🇸 - 28300
S11 46 Space Gray(M/L) 🇺🇸 - 30800
UL 2 White - OB 🇺🇸 - 53600
UL 3 Natural / Blue/Bright Blue TL(S/M) 🇺🇸 - 57800"""]).items
        self.assertEqual(len(items), 10)
        self.assertTrue(all(item.block == "Apple Watch" for item in items))
        self.assertTrue(any(item.title.startswith("Ultra 2 ") for item in items))
        self.assertTrue(any(item.title.startswith("Ultra 3 ") for item in items))

    def test_ul_and_ultra_are_the_same_model_name(self):
        items = parse_documents(["""UL 3 Black / Black AL(S) 🇺🇸 - 57800
Ultra 3 Black / Black AL(S) 🇺🇸 - 57800"""]).items
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].title.startswith("Ultra 3 "))
        self.assertEqual(items[0].block, "Apple Watch")

    def test_s_series_phone_is_not_mistaken_for_apple_watch(self):
        items = parse_documents(["""Samsung Galaxy S10 8/128 Black — 25000
S11 12/256 Black — 50000"""]).items
        self.assertNotEqual(items[0].block, "Apple Watch")
        self.assertNotEqual(items[1].block, "Apple Watch")


if __name__ == "__main__":
    unittest.main()
