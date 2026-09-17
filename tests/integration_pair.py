"""Real HTTP integration between two independently stored bot projects."""
import asyncio
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PAIR=Path(os.getenv('ZAYAVKI_PATH',str(ROOT.parent/'zayavki'))).resolve()
sys.path.insert(0,str(PAIR))
sys.path.insert(0,str(ROOT))

from aiohttp import web
from config import Settings
from prices import parse_documents
from retail_catalog import to_product, render_prices
from retail_store import RetailStore
from catalog_bridge import CatalogBridge, CatalogBridgeError
from catalog_api import create_app
from orders import OrderService

KEY='a_test_only_catalog_key_0123456789abcdef'


class PairIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        # Physically separate DB files/connections; production may use PostgreSQL
        # in the checkout project without exposing its DSN to the price bot.
        self.local=RetailStore('sqlite:///'+self.tmp.name+'/price.db')
        self.remote=RetailStore(os.getenv('PAIR_TEST_DATABASE_URL') or 'sqlite:///'+self.tmp.name+'/orders.db')
        with self.remote.transaction() as tx:
            tx.execute('DELETE FROM retail_data')
        self.runner=web.AppRunner(create_app(self.remote,KEY),access_log=None)
        await self.runner.setup()
        site=web.TCPSite(self.runner,'127.0.0.1',0)
        await site.start()
        port=self.runner.addresses[0][1]
        self.bridge=CatalogBridge(self.local,f'http://127.0.0.1:{port}',KEY,attempts=1)
        self.service=OrderService(self.remote,[10])

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.tmp.cleanup()

    async def test_supplier_to_http_cart_delivery_and_archive(self):
        products=[to_product(i,Settings(),{'markup':'3000'}) for i in parse_documents([
            '17 Pro 256 Black (1Sim+eSim) — 100000\nS11 42 Jet Black — 28000']).items]
        await asyncio.to_thread(self.bridge.put_catalog,products,confirmed=True)
        pages,nav=render_prices(products,'test_orders_bot')
        self.assertEqual(list(nav),['Apple'])
        ids=re.findall(r'\?start=p_([a-f0-9]{24})','\n'.join(pages.values()))
        user={'id':20,'username':'pair_test_customer'}
        for pid in ids:
            self.service.add(20,pid)
        for field,value in {'name':'Иван Петров','phone':'89123456789','city':'Москва',
                            'street':'Тверская','house':'12А','delivery':'courier'}.items():
            self.service.field(20,field,value)
        order=self.service.submit(user,self.service.cart(20)['token'])
        self.assertEqual(order['subtotal'],'134000.00')
        with self.assertRaises(PermissionError):
            self.service.set_status(20,order['id'],'working')
        quote=self.service.delivery_price(10,order['id'],'700')
        self.service.accept_delivery(20,order['id'],quote['delivery_revision'])
        self.service.set_status(10,order['id'],'courier')
        self.service.add(20,ids[0])
        products[0]['price']='110000'
        await asyncio.to_thread(self.bridge.put_catalog,products,confirmed=True)
        self.assertFalse(self.service.cart(20))
        self.assertEqual(self.service.get_order(20,order['id'])['subtotal'],'134000.00')
        self.assertFalse(self.local.scan('orders'))
        self.assertFalse(self.local.scan('profiles'))
        completed=self.service.set_status(10,order['id'],'completed')
        self.assertFalse(self.service.list_orders(10))
        self.assertEqual(len(self.service.list_orders(10,archive=True)),1)
        self.remote.housekeeping(now=completed['expires_at']+1)
        self.assertIsNone(self.remote.get('orders',order['id']))

    async def test_link_and_closed_status_cross_http_without_database_access(self):
        products=[to_product(i,Settings()) for i in parse_documents(['17 256 Black (eSim) — 80000']).items]
        await asyncio.to_thread(self.bridge.put_catalog,products,confirmed=True)
        await asyncio.to_thread(self.bridge.set,'system','catalog_url','https://t.me/c/123/4')
        self.assertEqual(self.remote.get('system','catalog_url'),'https://t.me/c/123/4')
        await asyncio.to_thread(self.bridge.mark_uncertain,'Поставщик закрыт')
        self.assertFalse(self.remote.get('system','catalog')['confirmed'])
        self.assertEqual(self.remote.get('catalog',products[0]['id'])['price'],'80000')

    async def test_wrong_key_cannot_change_order_catalog(self):
        wrong=CatalogBridge(self.local,self.bridge.url.removesuffix('/api/catalog/sync'),'wrong_key_0123456789abcdefghijklmnop',attempts=1)
        with self.assertRaises(CatalogBridgeError):
            await asyncio.to_thread(wrong.put_catalog,[],confirmed=True)
        self.assertIsNone(self.remote.get('system','catalog'))


if __name__ == '__main__':
    unittest.main()
