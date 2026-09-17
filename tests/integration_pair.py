"""Run with the matching checkout repository path; uses an isolated temporary DB."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAIR = Path(os.getenv('ZAYAVKI_PATH', str(ROOT.parent/'zayavki'))).resolve()
sys.path.insert(0, str(PAIR))
sys.path.insert(0, str(ROOT))

from config import Settings
from prices import parse_documents
from retail_catalog import to_product, render_prices
from retail_store import RetailStore
from orders import OrderService, UserError


class PairIntegration(unittest.TestCase):
    def test_supplier_to_cart_delivery_and_archive(self):
        self.assertEqual((ROOT/'retail_store.py').read_bytes(), (PAIR/'retail_store.py').read_bytes(),
                         'Shared database contracts diverged')
        with tempfile.TemporaryDirectory() as tmp:
            url = os.getenv('PAIR_TEST_DATABASE_URL') or 'sqlite:///' + tmp + '/shared.db'
            publisher_store, checkout_store = RetailStore(url), RetailStore(url)
            settings = Settings()
            products = [to_product(i, settings, {'markup':'3000'}) for i in parse_documents([
                '17 Pro 256 Black (1Sim+eSim) — 100000\nS11 42 Jet Black — 28000']).items]
            publisher_store.put_catalog(products, confirmed=True)
            pages, nav = render_prices(products, 'test_orders_bot')
            self.assertEqual(list(nav), ['Apple'])
            ids = re.findall(r'\?start=p_([a-f0-9]{24})', '\n'.join(pages.values()))
            service = OrderService(checkout_store, [10])
            user = {'id':20, 'username':'pair_test_customer'}
            for pid in ids:
                service.add(20,pid)
            for field,value in {'name':'Иван Петров', 'phone':'89123456789', 'city':'Москва',
                                'street':'Тверская', 'house':'12А', 'delivery':'courier'}.items():
                service.field(20,field,value)
            order = service.submit(user, service.cart(20)['token'])
            self.assertEqual(order['subtotal'],'134000.00')
            with self.assertRaises(PermissionError):
                service.set_status(20,order['id'],'working')
            quote = service.delivery_price(10,order['id'],'700')
            service.accept_delivery(20,order['id'],quote['delivery_revision'])
            service.set_status(10,order['id'],'courier')
            service.add(20,ids[0])
            products[0]['price']='110000'
            publisher_store.put_catalog(products,confirmed=True)
            self.assertFalse(service.cart(20))
            self.assertEqual(service.get_order(20,order['id'])['subtotal'],'134000.00')
            completed = service.set_status(10,order['id'],'completed')
            self.assertFalse(service.list_orders(10))
            self.assertEqual(len(service.list_orders(10,archive=True)),1)
            checkout_store.housekeeping(now=completed['expires_at']+1)
            self.assertIsNone(checkout_store.get('orders',order['id']))


if __name__ == '__main__':
    unittest.main()
