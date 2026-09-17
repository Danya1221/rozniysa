import re
import unittest
from decimal import Decimal

from config import Settings
from prices import (Item, amount_value, merge_sources, parse_documents, render_blocks,
                    select_items, units, marked_price)


class PriceTests(unittest.TestCase):
    def parse(self, text):
        return parse_documents([text])

    def test_iphone_variants_country_condition_and_markup(self):
        result = self.parse("""iPhone: 17
🇺🇸 17e 256GB Black - 58 800
🇮🇳 17e 256GB Black - 62 900
16 Pro 256 Natural CPO 🇺🇸 — 79 000
15 Pro Max 256 Black 🇯🇵 — 85 000
12 128 White 🇮🇳 — 33 000""")
        self.assertEqual(len(result.items), 5)
        self.assertEqual(result.items[2].block, "CPO")
        self.assertIn("CPO", result.items[2].title)
        self.assertIn("🇺🇸", result.items[2].title)
        self.assertEqual(result.items[0].price, Decimal("58800"))
        self.assertEqual(marked_price(result.items[0], Settings(markup=Decimal(500))), Decimal(59300))

    def test_pro_max_is_separate_from_base(self):
        items = self.parse("iPhone 17 256 Black — 60000\niPhone 17 Pro Max 256 Black — 90000").items
        self.assertEqual([i.block for i in items], ["iPhone 17", "iPhone 17 Pro Max"])

    def test_sim_header_and_explicit_variants(self):
        items = self.parse("""iPhone 17
eSIM
17 256 Black — 60000
SIM
17 256 Black — 62000
17 256 Black 2 SIM — 65000""").items
        self.assertEqual([i.sim for i in items], ["esim", "sim", "dual"])
        self.assertEqual(len(select_items(items, Settings(sim_filter="sim"))), 2)
        self.assertEqual(len(select_items(items, Settings(sim_filter="esim"))), 1)

    def test_country_flag_does_not_infer_iphone17_sim(self):
        item = self.parse("🇺🇸 iPhone 17 256GB Black — 58800").items[0]
        self.assertEqual(item.sim, "unknown")
        self.assertEqual(len(select_items([item], Settings(sim_filter="esim"))), 0)

    def test_supplier_sim_markers_drive_iphone17_variants(self):
        items = self.parse("""iPhone 17
SIM + eSIM
17 256 Black 🇮🇳 — 62000
eSIM
17 Pro 256 Black 🇺🇸 — 70000
2 SIM
17 Pro Max 256 Black 🇨🇳 — 80000""").items
        self.assertEqual([item.sim for item in items], ["hybrid", "esim", "dual"])

    def test_explicit_sim_marker_is_used(self):
        item = self.parse("🇺🇸 iPhone 17 256 Black SIM + eSIM — 60000").items[0]
        self.assertEqual(item.sim, "hybrid")

    def test_google_plus_and_samsung_sm_code(self):
        result = self.parse("Samsung Galaxy S26+ SM-S947B 12/256 Cobalt Violet 🇦🇪 — 68300")
        self.assertEqual(result.items[0].block, "Samsung S26")
        self.assertIn("S26+", result.items[0].title)
        self.assertNotIn("SM-", result.items[0].title)
        self.assertIn("🇦🇪", result.items[0].title)

    def test_brands_are_not_misclassified(self):
        items = self.parse("""LEGO Star Wars 75313 — 78000
Dyson Airwrap HS08 — 42000
Canon EOS R50 — 51000
DJI Osmo Pocket 3 — 38000
Insta360 X5 — 41000
Google Fitbit Air Obsidian — 10700
Ray-Ban Meta Wayfarer S50 Black — 32000
Ray-Ban Meta Skyler S53 Brown — 34000""").items
        self.assertEqual([i.block for i in items], [
            "LEGO", "Dyson", "Canon", "DJI / Insta360", "DJI / Insta360", "Google",
            "Ray-Ban Meta", "Ray-Ban Meta"])
        self.assertIn(" M ", items[-2].title)
        self.assertIn(" L ", items[-1].title)

    def test_note_14s_and_rode_do_not_inherit_dji_section(self):
        items = self.parse("""DJI / Insta360
Note 14S 8/256 Aurora Purple 🇪🇺 — 17800
Note 14S 8/256 Midnight Black 🇪🇺 — 17800
RODE Wireless Me Dual Set 🇷🇺 — 12700
RODE Wireless Pro 🇷🇺 — 23300
DJI Osmo Pocket 4 Creator Combo — 44800
Insta360 X6 Standard Bundle — 51100""").items
        self.assertEqual([item.block for item in items], [
            "Xiaomi", "Xiaomi", "Rode", "Rode", "DJI / Insta360", "DJI / Insta360"
        ])
        self.assertTrue(items[0].title.startswith("Note 14S"))
        self.assertTrue(items[2].title.startswith("RODE Wireless"))
        self.assertNotIn("DJI / Insta360 Note", items[0].title)
        self.assertNotIn("DJI / Insta360 RODE", items[2].title)

    def test_samsung_series_are_read_and_split_into_catalog_blocks(self):
        items = self.parse("""Samsung
Galaxy A
A27 8/256 Black 🇪🇺 — 24000
A37 8/256 Awesome Graphite 🇪🇺 — 35000
A57 12/256 Blue 🇪🇺 — 42000
Galaxy S
S26 12/256 Black 🇦🇪 — 65000
S26+ 12/256 Cobalt Violet 🇦🇪 — 68300
S26 Ultra 12/512 Titanium Black 🇦🇪 — 99000
Galaxy Z
Z Fold 7 12/256 Black 🇦🇪 — 120000
Z Flip 7 12/256 Mint 🇦🇪 — 78000""").items
        self.assertEqual([item.block for item in items], [
            "Samsung A + S25", "Samsung A + S25", "Samsung A + S25",
            "Samsung S26", "Samsung S26", "Samsung S26",
            "Samsung Fold / Flip", "Samsung Fold / Flip",
        ])
        pages = render_blocks(items, Settings())
        text = "\n".join(pages.values())
        self.assertIn("<b>Samsung</b>", text)
        self.assertIn("<b>— Samsung A + S25 —</b>", text)
        self.assertIn("<b>— Samsung S26 —</b>", text)
        self.assertIn("<b>— Samsung Fold / Flip —</b>", text)

    def test_coros_does_not_inherit_xiaomi_context(self):
        items = self.parse("""Xiaomi
Coros Pace 4 Black Nylon 🇨🇳 — 21000
Coros Pace 4 Black Silicone 🇨🇳 — 22000
Coros Pace 4 Ice Crystal Black Silicone 🇨🇳 — 22700
Coros Pace 4 Jacob&Co collaboration fabric strap version 🇨🇳 — 25300""").items
        self.assertEqual([item.block for item in items], ["COROS"] * 4)
        self.assertTrue(all(item.title.startswith("Coros ") for item in items))
        self.assertTrue(all(not item.title.startswith("Xiaomi ") for item in items))

    def test_requested_samsung_phone_split_and_tab_tablets(self):
        items = self.parse("""Samsung
Galaxy A
A37 8/256 Black — 35000
A57 12/256 Blue — 42000
Galaxy S25
S25 12/256 Black — 65000
S25 Ultra 12/512 Titanium — 90000
Galaxy S26
S26 12/256 Black — 68000
S26+ 12/256 Cobalt Violet — 71000
S26 Ultra 12/512 Titanium Black — 99000
Galaxy Tab S
Tab S10 12/256 Grey — 65000
Tab S11 Ultra 12/512 Silver — 98000""").items
        self.assertEqual([item.block for item in items], [
            "Samsung A + S25", "Samsung A + S25",
            "Samsung A + S25", "Samsung A + S25",
            "Samsung S26", "Samsung S26", "Samsung S26",
            "Samsung Tab S", "Samsung Tab S",
        ])
        self.assertTrue(all(not item.title.startswith("Samsung A +") for item in items))
        pages = render_blocks(items, Settings())
        text = "\n".join(pages.values())
        self.assertIn("<b>Samsung</b>", text)
        self.assertIn("<b>— Samsung A + S25 —</b>", text)
        self.assertIn("<b>— Samsung S26 —</b>", text)
        self.assertIn("<b>— Samsung Tab S —</b>", text)

    def test_supplier_style_samsung_sections(self):
        items = self.parse("""Buds 3 FE Black 🇦🇪 - 6400
Buds 4 White 🇰🇿 - 10400
🇷🇺A17 6/128GB Gray — 15000
🇷🇺A27 8/256GB Blue — 23800
🇷🇺A57 12/512GB Navy — 40500
S25 FE 8/128 Navy 🇮🇳 - 37400
S25 12/128 Navy 🇪🇺 - 45000
S25 Edge 12/256 Jetblack 🇪🇺 - 49100
S25 Ultra 12/1TB Gray 🇨🇱 - 79400
S26 FE 8/128 Graphite 🇿🇦 - 46500
S26+ 12/256 White 🇰🇿 - 69500
S26 Ultra 16/1TB Black 🇨🇱 - 117500""").items
        self.assertEqual([item.block for item in items], [
            "Samsung Buds", "Samsung Buds",
            "Samsung A + S25", "Samsung A + S25", "Samsung A + S25",
            "Samsung A + S25", "Samsung A + S25", "Samsung A + S25", "Samsung A + S25",
            "Samsung S26", "Samsung S26", "Samsung S26",
        ])
        pages = render_blocks(items, Settings())
        text = "\n".join(pages.values())
        self.assertIn("<b>Samsung</b>", text)
        self.assertIn("<b>— Samsung Buds —</b>", text)
        self.assertIn("<b>— Galaxy A —</b>", text)
        self.assertIn("<b>— Galaxy S25 —</b>", text)
        self.assertIn("<b>— Samsung S26 —</b>", text)
        combined = next(page for page in pages.values() if page.startswith("<b>Samsung</b>"))
        self.assertLess(combined.index("Galaxy A"), combined.index("Galaxy S25"))

    def test_all_apple_watches_share_one_block(self):
        items = self.parse("""Apple Watch
Series 11 46mm Black — 40000
SE 3 44mm Silver — 30000
Ultra 3 49mm Black — 70000""").items
        self.assertEqual([item.block for item in items], ["Apple Watch"] * 3)
        pages = render_blocks(items, Settings())
        self.assertEqual(len(pages), 1)
        watch_page = next(iter(pages.values()))
        self.assertTrue(watch_page.startswith("<b>Apple</b>"))
        self.assertIn("<b>— Apple Watch —</b>", watch_page)

    def test_only_requested_galaxy_a_models_are_auto_detected(self):
        items = self.parse("""A17 6/128GB Gray — 15000
A27 6/128GB Black — 20800
A37 8/128GB Lavender — 23900
A57 8/256GB Navy — 31800""").items
        self.assertEqual([item.block for item in items], ["Samsung A + S25"] * 4)
        self.assertEqual([item.title.split()[0] for item in items], ["A17", "A27", "A37", "A57"])

    def test_other_bare_a_models_are_not_assumed_to_be_samsung(self):
        items = self.parse("""A15 8/256 Black — 19900
A55 8/256 Blue — 29900""").items
        self.assertEqual([item.block for item in items], ["Товары", "Товары"])

    def test_iphone_16_family_is_one_physical_message_with_storage_gaps(self):
        items = self.parse("""iPhone 16 128 Black — 60000
iPhone 16 256 Blue — 65000
iPhone 16 Plus 128 Pink — 70000
iPhone 16 Plus 512 White — 82000
iPhone 16 Pro 256 Black — 90000
iPhone 16 Pro Max 256 Natural — 100000""").items
        self.assertEqual({item.block for item in items}, {
            "iPhone 16", "iPhone 16 Plus", "iPhone 16 Pro", "iPhone 16 Pro Max"
        })
        pages = render_blocks(items, Settings())
        self.assertEqual(len(pages), 1)
        content = next(iter(pages.values()))
        self.assertTrue(content.startswith("<b>iPhone 16 / 16 Plus / 16 Pro / 16 Pro Max</b>"))
        for model in ["iPhone 16", "iPhone 16 Plus", "iPhone 16 Pro", "iPhone 16 Pro Max"]:
            self.assertIn("— " + model + " —", content)
        self.assertIn("128 Black — 60 000</code>\n\n<code>iPhone 16 256 Blue", content)

    def test_requested_iphone_generations_are_three_large_messages(self):
        items = self.parse("""iPhone 11 128 Black — 30000
iPhone 12 128 Blue — 35000
iPhone 13 128 Black — 40000
iPhone 14 128 Black — 45000
iPhone 15 128 Black — 50000
iPhone 16 128 Black — 60000
iPhone 16 Plus 128 Pink — 70000
iPhone 16 Pro 256 Black — 80000
iPhone 16 Pro Max 256 Natural — 90000
iPhone 17 256 Black — 70000
iPhone 17 Pro 256 Blue — 90000
iPhone 17 Pro Max 256 Silver — 100000
iPhone Air 256 Gold — 80000""").items
        pages = list(render_blocks(items, Settings()).values())
        self.assertEqual(len(pages), 3)
        self.assertTrue(any(page.startswith("<b>iPhone 11 / 12 / 13 / 14 / 15</b>") for page in pages))
        self.assertTrue(any(page.startswith("<b>iPhone 16 / 16 Plus / 16 Pro / 16 Pro Max</b>") for page in pages))
        self.assertTrue(any(page.startswith("<b>iPhone 17 Air / 17 / 17 Pro / 17 Pro Max</b>") for page in pages))

    def test_small_brand_blocks_are_packed_with_visible_section_gap(self):
        items = self.parse("""Xiaomi 15 12/256 White — 48000
Vivo V70 12/256 Grey — 46000
Realme GT 7 12/256 Black — 42000""").items
        pages = list(render_blocks(items, Settings()).values())
        self.assertEqual(len(pages), 1)
        content = pages[0]
        header = content.split("\n", 1)[0]
        for brand in ["Xiaomi", "Vivo", "Realme"]:
            self.assertIn(brand, header)
        self.assertIn("</code>\n\n\n<b>— ", content)

    def test_apple_sections_stay_together_and_never_mix_with_other_brands(self):
        items = self.parse("""Kodak Mini Shot 3 — 12000
Mac mini M4 16/256 Silver — 68800
iMac M4 24 16/256 Blue — 110000
MacBook Air M4 16/256 — 90000
Marshall Major V Black — 5400
Oura Ring 5 Size 8 Black — 35800
Apple TV 4K 64GB — 20200""").items
        pages = list(render_blocks(items, Settings()).values())
        apple = next(page for page in pages if page.startswith("<b>Apple</b>"))
        for label in ["MacBook / iMac", "Mac mini", "Apple TV"]:
            self.assertIn(label, apple)
        for foreign in ["Kodak", "Marshall", "Oura Ring"]:
            self.assertNotIn("— " + foreign, apple)

    def test_single_apple_section_still_has_apple_as_top_heading(self):
        for source, section in [
            ("Mac mini M4 16/256 Silver — 68800", "Mac mini"),
            ("Apple TV 4K 64GB — 20200", "Apple TV"),
            ("MacBook Air M4 16/256 Silver — 90000", "MacBook / iMac"),
        ]:
            items = self.parse(source).items
            content = next(iter(render_blocks(items, Settings()).values()))
            self.assertTrue(content.startswith("<b>Apple</b>\n\n"), content)
            self.assertIn(f"<b>— {section} —</b>", content)

    def test_supplier_ipad_shorthand_and_pencil_are_read_as_apple(self):
        items = self.parse("""Apple Pencil TYPE-C 🇪🇺 - 6400
iPad 11 128 Blue Wi-Fi 🇺🇸 - 38600
MINI 7 128 Blue Wi-Fi 🇺🇸 - 43400
MINI 7 256 Gray Wi-Fi 🇺🇸 - 51300
AIR 13 M3 256 Starlight Wi-Fi 🇺🇸 - 75800
AIR 11 M4 128 Gray Wi-Fi 🇺🇸 - 60600
PRO 12.9 M2 128 Gray LTE 🇺🇸 - 71500
PRO 11 M4 1TB Black Wi-Fi (Nano Texture) 🇺🇸 - 99000
PRO 13 M4 256 Black LTE 🇺🇸 - 100500
PRO 11 M5 256 Black Wi-Fi 🇺🇸 - 96000""").items
        ipad = [item for item in items if item.block == "iPad"]
        pencil = [item for item in items if item.block == "Apple Accessories"]
        self.assertEqual(len(ipad), 9)
        self.assertEqual(len(pencil), 1)
        pages = list(render_blocks(items, Settings()).values())
        self.assertTrue(all(page.startswith("<b>Apple</b>") for page in pages), pages)

    def test_saved_order_cannot_strand_apple_tv_away_from_apple(self):
        items = self.parse("""AirPods Pro 3 Black — 20000
Honor 400 12/256 Black — 33000
Apple TV 4K 64GB (2022) — 20200
Oura Ring 5 Size 8 Black — 35000
Mac Mini (MU9D3) M4/16/256 Silver — 68500""").items
        pages = list(render_blocks(items, Settings(), {
            "block_order": ["AirPods", "Honor", "Apple TV", "Oura Ring", "Mac mini"]
        }).values())
        apple_pages = [page for page in pages if page.startswith("<b>Apple</b>")]
        self.assertEqual(len(apple_pages), 1, pages)
        apple = apple_pages[0]
        self.assertIn("— AirPods —", apple)
        self.assertIn("— Apple TV —", apple)
        self.assertIn("— Mac mini —", apple)
        self.assertNotIn("Honor", apple)
        self.assertNotIn("Oura", apple)


    def test_supplier_macbook_shorthand_and_apple_adapters_are_read(self):
        items = self.parse("""Mac Mini (MU9D3) M4/16/256 Silver - 68500
Neo 13 MHFD4 Citrus (A18 Pro 8/256) - 61100
Air 13 MDHE4 Midnight (M5 16/512) - 127100
Air 15 MDVE4 Starlight (M5 16/1TB) - 154200
Pro 14 Z1KH1 Space Black (M5 24/512GB) 🇺🇸 - 183500
Pro 16 MX2Y3 Space Black (M4 Pro 48/512GB) - 249000
 Adapter 20W 🇪🇺 - 900
(От 20 шт) - 850
Аdapter universal - 100
Переходник для MacBook - 100
 Аdapter USB-C to USB - 1300
Apple TV 4K 64GB (2022) 🇺🇸 - 20200""").items
        blocks = [item.block for item in items]
        self.assertIn("Mac mini", blocks)
        self.assertGreaterEqual(blocks.count("MacBook / iMac"), 5)
        self.assertGreaterEqual(blocks.count("Apple Accessories"), 4)
        self.assertIn("Apple TV", blocks)
        self.assertFalse(any(item.title.startswith("От 20") for item in items))

    def test_small_apple_sections_are_not_orphaned_into_single_model_posts(self):
        source = "\n".join([
            *[f"AirPods Pro 3 Variant {i} — {20000 + i}" for i in range(45)],
            "Mac Mini (MU9D3) M4/16/256 Silver — 68500",
            "Apple TV 4K 64GB (2022) — 20200",
            " Adapter 20W — 900",
            "Переходник для MacBook — 100",
            *[f"Air 13 MDH{i:02d} Midnight (M5 16/512) — {127000 + i}" for i in range(35)],
        ])
        items = self.parse(source).items
        pages = list(render_blocks(items, Settings()).values())
        apple_pages = [page for page in pages if page.startswith("<b>Apple</b>")]
        self.assertGreaterEqual(len(apple_pages), 2)
        mini_page = next(page for page in apple_pages if "— Mac mini —" in page)
        tv_page = next(page for page in apple_pages if "— Apple TV —" in page)
        self.assertIs(mini_page, tv_page)
        self.assertIn("— Apple Accessories —", mini_page)
        self.assertTrue(all(units(page) <= 4096 for page in pages))


    def test_single_samsung_section_still_has_samsung_as_top_heading(self):
        items = self.parse("S26 12/256 Black — 64300").items
        content = next(iter(render_blocks(items, Settings()).values()))
        self.assertTrue(content.startswith("<b>Samsung</b>\n\n"), content)
        self.assertIn("<b>— Samsung S26 —</b>", content)

    def test_samsung_sections_stay_together_and_do_not_mix_with_honor(self):
        items = self.parse("""Buds 4 Black — 9000
A27 8/256 Blue — 23800
S25 Ultra 12/256 Black — 65000
S26 12/256 Black — 64900
Honor 400 12/256 Black — 33000""").items
        pages = list(render_blocks(items, Settings()).values())
        samsung = next(page for page in pages if page.startswith("<b>Samsung</b>"))
        self.assertIn("Samsung Buds", samsung)
        self.assertIn("Samsung A + S25", samsung)
        self.assertIn("Samsung S26", samsung)
        self.assertNotIn("Honor", samsung)

    def test_blank_line_is_added_when_model_changes(self):
        items = self.parse("""A17 6/128GB Gray — 15000
A17 8/256GB Blue — 17800
A27 6/128GB Black — 20800
A27 8/256GB Blue — 23800
S25 12/256 Navy — 50500
S25 Ultra 12/256 Black — 65000""").items
        page = next(value for value in render_blocks(items, Settings()).values() if value.startswith("<b>Samsung</b>"))
        self.assertIn("<b>— Samsung A + S25 —</b>", page)
        self.assertIn("A17 8/256GB Blue — 17 800</code>\n\n<code>A27", page)
        self.assertIn("<b>— Galaxy S25 —</b>", page)
        self.assertIn("S25 12/256 Navy — 50 500</code>\n\n<code>S25 Ultra", page)

    def test_iphone_models_are_separate_and_keep_activation_sections(self):
        items = self.parse("""iPhone 16
16 128GB Black 🇮🇳 — 63700
16 128GB Black 🇮🇳 Актив — 60800
16 Plus 128GB Black 🇮🇳 — 73700
16 Plus 128GB Pink 🇮🇳 Актив — 72000
16 Pro 256GB Natural 🇦🇪 — 83600""").items
        self.assertEqual({item.block for item in items}, {"iPhone 16", "iPhone 16 Plus", "iPhone 16 Pro"})
        pages = render_blocks(items, Settings())
        iphone16 = next(page for page in pages.values() if page.startswith("<b>iPhone 16 / 16 Plus / 16 Pro / 16 Pro Max</b>"))
        self.assertIn("— iPhone 16 —", iphone16)
        self.assertIn("— iPhone 16 Plus —", iphone16)
        self.assertNotIn("— Не активированное —", iphone16)
        self.assertGreaterEqual(iphone16.count("— Актив —"), 2)

    def test_service_headings_have_visible_blank_lines(self):
        items = self.parse("iPhone 17e 256 Black 1Sim+eSim — 63800\niPhone 17e 512 White 1Sim+eSim Актив — 70000").items
        content = next(iter(render_blocks(items, Settings()).values()))
        self.assertIn("— iPhone 17e —</b>\n\n<b>— SIM + eSIM —</b>\n\n<code>", content)
        self.assertIn("— Актив —</b>\n\n<b>— SIM + eSIM —</b>\n\n<code>", content)
        self.assertNotIn("— Не активированное —", content)

    def test_iphone_11_to_15_visual_order_is_ascending(self):
        items = self.parse("iPhone 15 Pro Max 256 Black — 84000\niPhone 15 128 Blue — 48800\niPhone 14 512 Blue — 54300\niPhone 13 128 Midnight — 45200\niPhone 12 128 White — 33600\niPhone 11 128 Black — 30000").items
        content = next(iter(render_blocks(items, Settings()).values()))
        positions = [content.index(label) for label in ["— iPhone 11 —", "— iPhone 12 —", "— iPhone 13 —", "— iPhone 14 —", "— iPhone 15 —"]]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(content.startswith("<b>iPhone 11 / 12 / 13 / 14 / 15</b>"))

    def test_iphone17_visual_order_is_17e_air_pro_pro_max(self):
        items = self.parse("""iPhone 17 Pro Max 256 Silver — 100000
iPhone Air 256 Gold — 80000
iPhone 17e 256 Black — 65000
iPhone 17 Pro 256 Blue — 90000""").items
        content = next(iter(render_blocks(items, Settings()).values()))
        positions = [content.index(label) for label in ["— iPhone 17e —", "— iPhone Air —", "— iPhone 17 Pro —", "— iPhone 17 Pro Max —"]]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(content.startswith("<b>iPhone 17e / 17 Air / 17 Pro / 17 Pro Max</b>"))

    def test_samsung_memory_sizes_have_blank_lines(self):
        items = self.parse("""S26 12/128 Black — 57300
S26 12/128 Cobalt Violet — 57300
S26 12/256 Black — 64300
S26 12/256 Sky Blue — 63000
S26 12/512 Black — 70800""").items
        content = next(iter(render_blocks(items, Settings()).values()))
        self.assertIn("12/128 Cobalt Violet — 57 300</code>\n\n<code>S26 12/256", content)
        self.assertIn("12/256 Sky Blue — 63 000</code>\n\n<code>S26 12/512", content)

    def test_mac_mini_and_apple_tv_never_inherit_previous_brand(self):
        items = self.parse("""Kodak
Kodak Mini Shot 3 — 12000
Mac mini M4 16/256 Silver — 68800
Apple TV 4K 64GB — 20200""").items
        mac = next(item for item in items if item.block == "Mac mini")
        tv = next(item for item in items if item.block == "Apple TV")
        self.assertTrue(mac.title.startswith("Mac mini"))
        self.assertTrue(tv.title.startswith("Apple TV"))
        self.assertNotIn("Kodak", mac.title)
        self.assertNotIn("Kodak", tv.title)
        apple = next(page for page in render_blocks(items, Settings()).values() if page.startswith("<b>Apple</b>"))
        self.assertIn("— Mac mini —", apple)
        self.assertIn("— Apple TV —", apple)

    def test_preserve_explicit_condition_and_original_packaging(self):
        item = self.parse("iPhone 16 128 Black Актив (Ориг. Упаковка) — 45000").items[0]
        self.assertIn("Актив (Ориг. Упаковка)", item.title)

    def test_asis_and_cpo_are_separate_messages(self):
        items = self.parse("""iPhone 16 Pro 256 Natural CPO 🇺🇸 — 79000
ASIS
iPhone 16 Pro Max 256 Black 🇺🇸 — 80000
iPhone 17 256 Black 🇮🇳 — 60000""").items
        self.assertEqual(items[0].block, "CPO")
        self.assertEqual(items[1].block, "ASIS")
        pages = render_blocks(items, Settings())
        contents = list(pages.values())
        self.assertTrue(any("<b>CPO</b>" in page for page in contents))
        self.assertTrue(any("<b>ASIS</b>" in page for page in contents))

    def test_active_and_inactive_are_sorted_inside_message(self):
        items = self.parse("""iPhone 17 256 Black Актив 🇮🇳 — 60000
iPhone 17 256 White Неактив 🇮🇳 — 61000
iPhone 17 256 Blue 🇮🇳 — 62000""").items
        content = next(iter(render_blocks(items, Settings()).values()))
        self.assertNotIn("— Не активированное —", content)
        self.assertIn("— Актив —", content)
        self.assertNotIn("Статус не указан", content)
        self.assertLess(content.index("256 Blue"), content.index("— Актив —"))

    def test_accessories_filter(self):
        items = self.parse("Чехол iPhone 17 Black — 500\niPhone 17 256 Black — 60000").items
        self.assertEqual(len(items), 2)
        selected = select_items(items, Settings(allow_accessories=False))
        self.assertEqual(len(selected), 1)
        self.assertFalse(selected[0].accessory)

    def test_wholesale_tier_does_not_replace_retail(self):
        items = self.parse("iPhone 17 256 Black — 60000 от 6 шт — 58000").items
        self.assertEqual(items[0].price, Decimal(60000))
        self.assertEqual(self.parse("от 6 шт iPhone 17 256 Black — 58000").items, [])

    def test_menus_and_unavailable_are_not_products(self):
        self.assertEqual(self.parse("Прайс\nАктуальный прайс\nВыберите категорию\nОбновлён 2026").items, [])
        self.assertEqual(self.parse("iPhone 17 256 Black — 60000 ❌").items, [])

    def test_unclassified_priced_product_is_retained(self):
        result = self.parse("Something 256GB Green — 88000")
        self.assertEqual(result.items[0].block, "Товары")
        self.assertEqual(result.items[0].price, Decimal(88000))

    def test_currency_and_decimal_price(self):
        result = self.parse("iPhone 17 256 Black — 799.50 €\niPhone 17 256 White — $ 850")
        self.assertEqual([i.currency for i in result.items], ["EUR", "USD"])
        self.assertEqual(result.items[0].price, Decimal("799.50"))
        for value in ["79 000", "79,000", "79.000"]:
            self.assertEqual(amount_value(value), Decimal(79000))

    def test_multi_document_headers_and_duplicates(self):
        result = parse_documents(["iPhone 17\n17 256 Black — 60000", "17 256 Black — 61000"])
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].price, Decimal(61000))

    def test_source_priority_keeps_countries_separate(self):
        one = self.parse("🇺🇸 iPhone 17 256GB Black — 60000").items
        two = self.parse("iPhone 17 256 Black 🇺🇸 — 61000\n🇮🇳 iPhone 17 256 Black — 62000").items
        merged = merge_sources([one, two])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].price, Decimal(60000))

    def test_escaped_html_copyable_name_and_closed_prices(self):
        items = self.parse("B&W Px8 Black <Special> — 50000").items
        pages = render_blocks(items, Settings())
        content = next(iter(pages.values()))
        self.assertIn("<code>B&amp;W", content)
        self.assertIn("&lt;Special&gt;", content)
        closed = next(iter(render_blocks(items, Settings(), closed=True).values()))
        self.assertNotIn("50 000", closed)
        self.assertIn("Продажи закрыты", closed)

    def test_pages_fit_telegram_limit(self):
        items = [Item(f"iPhone 17 256GB Black {i} 🇺🇸", Decimal(60000), "RUB", "iPhone 17", "esim")
                 for i in range(220)]
        pages = render_blocks(items, Settings())
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(units(p) <= 4096 for p in pages.values()))
        self.assertEqual(sum(p.count("<code>") for p in pages.values()), 220)

    def test_split_pages_have_no_part_labels(self):
        items = [Item(f"iPhone 17 256GB Black {i} 🇺🇸", Decimal(60000), "RUB", "iPhone 17", "esim")
                 for i in range(220)]
        pages = render_blocks(items, Settings())
        self.assertGreater(len(pages), 1)
        self.assertTrue(all("Часть " not in page for page in pages.values()))

    def test_closure_and_block_filter(self):
        self.assertTrue(self.parse("В данный момент мы закрыты. Ждём завтра").closed)
        items = self.parse("iPhone 17 256 Black — 60000\nDyson HS08 — 40000").items
        self.assertEqual([i.block for i in select_items(items, Settings(), {"disabled_blocks": ["Dyson"]})], ["iPhone 17"])

    def test_physical_order_reorders_final_messages_not_logical_apple_sections(self):
        items = self.parse(
            "iPhone 17 256 Black — 60000\n"
            "Mac Mini (MU9D3) M4/16/256 Silver — 68500\n"
            "Apple TV 4K 128GB — 15000\n"
            "Dyson HS08 — 40000"
        ).items
        pages = render_blocks(items, Settings(), {"physical_order": ["Dyson", "Apple"]})
        titles = [re.match(r"<b>(.*?)</b>", page).group(1) for page in pages.values()]
        self.assertEqual(titles[:2], ["Dyson", "Apple"])
        self.assertNotIn("Mac mini", titles)
        self.assertNotIn("Apple TV", titles)

    def test_esim_separators_are_not_physical_sim(self):
        for label in ("e-SIM", "e SIM", "eSIM"):
            item = self.parse(f"iPhone 17 256 Black {label} — 60000").items[0]
            self.assertEqual(item.sim, "esim")


if __name__ == "__main__":
    unittest.main()
