import unittest
from config import Settings
from prices import Item, parse_documents, render_blocks

class ParserRegressionTests(unittest.TestCase):
    def parse(self, text):
        return parse_documents([text])

    def test_1sim_plus_esim_is_hybrid(self):
        items = self.parse("iPhone 17\n1Sim+eSim\n17 256 Black — 60000\niPhone 17 Pro 256 White (1Sim+eSim) — 70000").items
        self.assertEqual([i.sim for i in items], ["hybrid", "hybrid"])
        self.assertIn("SIM + eSIM", "\n".join(render_blocks(items, Settings()).values()))

    def test_short_iphone_tb_and_air_blocks(self):
        items = self.parse("17 Pro 1TB Orange — 100000\n17 Pro Max 2TB Silver — 120000\n17 Air 1TB Blue — 90000").items
        self.assertEqual([i.block for i in items], ["iPhone 17 Pro", "iPhone 17 Pro Max", "iPhone Air"])
        self.assertTrue(all(i.title.startswith("iPhone ") for i in items))

    def test_redmi_note_and_oura_do_not_inherit_dji(self):
        items = self.parse("DJI\nOsmo Pocket 3 — 38000\nNote 17 8/256 Black — 21900\nOura Ring 4 Silver — 35000").items
        self.assertEqual([i.block for i in items], ["DJI / Insta360", "Xiaomi", "Oura Ring"])
        self.assertNotIn("DJI Note", items[1].title)
        self.assertNotIn("DJI Oura", items[2].title)

    def test_watch_series_and_macs_are_grouped(self):
        items = self.parse("Apple Watch\nSeries 11 46mm Jet Black — 40000\nUltra 3 49mm Black — 70000\nMacBook Air M4 16/256 — 90000\niMac M4 24 16/256 — 110000").items
        self.assertEqual([i.block for i in items], ["Apple Watch", "Apple Watch", "MacBook / iMac", "MacBook / iMac"])
        packed = "\n".join(render_blocks(items, Settings()).values())
        self.assertIn("— Apple Watch —", packed)
        self.assertIn("Series 11", packed)
        self.assertIn("Ultra 3", packed)
        pages = "\n".join(render_blocks(items, Settings()).values())
        self.assertIn("<b>— MacBook / iMac —</b>", pages)
        self.assertIn("<b>Apple</b>", pages)

    def test_iphone_11_to_15_publish_as_individual_models(self):
        items = self.parse("""iPhone: 13-14-15
🇮🇳 13 128GB Midnight - 45100
🇺🇸 14 128GB Midnight - 46400
🇮🇳 15 128GB Black - 55400
🇮🇳 15 Plus 128GB Pink - 61400
🇦🇪 15 Pro 128GB Blue - 83600
iPhone 12 128 Black — 40000
iPhone 11 Pro Max 256 Green — 39000""").items
        self.assertEqual({item.block for item in items}, {
            "iPhone 11 Pro Max", "iPhone 12", "iPhone 13", "iPhone 14",
            "iPhone 15", "iPhone 15 Plus", "iPhone 15 Pro",
        })
        pages = render_blocks(items, Settings())
        headers = {page.split("\n", 1)[0] for page in pages.values()}
        self.assertTrue(any(page.startswith("<b>iPhone 11 / 12 / 13 / 14 / 15</b>") for page in pages.values()))


    def test_saved_wrong_block_is_reclassified(self):
        item = Item.from_dict({"title": "Oura Ring 4 Silver", "price": "35000", "currency": "RUB", "block": "DJI / Insta360", "sim": "unknown", "accessory": False})
        self.assertEqual(item.block, "Oura Ring")

if __name__ == "__main__":
    unittest.main()
