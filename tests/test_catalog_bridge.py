import json
import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from catalog_bridge import CatalogBridge, CatalogBridgeError, NoRedirect, validate_config
from retail_store import RetailStore, stable_id

KEY='a_test_only_catalog_key_0123456789abcdef'
PRODUCT={'id':stable_id('phone'), 'title':'iPhone 17', 'price':'80000', 'currency':'RUB',
         'brand':'Apple', 'section':'iPhone 17'}


class Reply(BytesIO):
    def __enter__(self): return self
    def __exit__(self,*args): self.close()


class FakeOpener:
    def __init__(self):
        self.bodies=[]
        self.failures=[]

    def open(self,request,**kwargs):
        data=json.loads(request.data)
        self.bodies.append(data)
        if self.failures:
            raise self.failures.pop(0)
        return Reply(json.dumps({'ok':True,'protocol':1,'revision':data['revision'], 'cancelled_drafts':0}).encode())


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=RetailStore('sqlite:///'+self.tmp.name+'/local.db')
        self.bridge=CatalogBridge(self.store,'https://checkout.example.test',KEY,attempts=2)
        self.opener=FakeOpener()
        self.network=patch('catalog_bridge.build_opener',return_value=self.opener)
        self.network.start()
        self.sleep=patch('catalog_bridge.time.sleep')
        self.sleep.start()

    def tearDown(self):
        self.network.stop(); self.sleep.stop(); self.tmp.cleanup()

    def test_retry_uses_identical_revision_and_original_supplier_timestamp(self):
        self.opener.failures=[URLError('connection reset')]
        self.bridge.put_catalog([PRODUCT],confirmed=False,checked_at=123)
        self.assertEqual(self.opener.bodies[0],self.opener.bodies[1])
        self.assertEqual(self.opener.bodies[1]['checked_at'],123)

    def test_bad_credentials_stop_without_retry(self):
        self.opener.failures=[HTTPError('https://checkout.example.test',401,'Unauthorized',{},None)]
        with self.assertRaisesRegex(CatalogBridgeError,'SYNC_API_KEY'):
            self.bridge.put_catalog([PRODUCT],confirmed=True)
        self.assertEqual(len(self.opener.bodies),1)

    def test_http_400_surfaces_checkout_validation_detail(self):
        body=BytesIO(json.dumps({'error':'invalid_catalog','detail':'Duplicate product ID: deadbeef'}).encode())
        self.opener.failures=[HTTPError('https://checkout.example.test',400,'Bad Request',{},body)]
        with self.assertRaisesRegex(CatalogBridgeError,'Duplicate product ID'):
            self.bridge.put_catalog([PRODUCT],confirmed=True)
        self.assertEqual(len(self.opener.bodies),1)

    def test_network_failure_is_reported_without_secret_or_url(self):
        self.opener.failures=[URLError(KEY),URLError(KEY)]
        with self.assertRaises(CatalogBridgeError) as result:
            self.bridge.put_catalog([PRODUCT],confirmed=True)
        self.assertNotIn(KEY,str(result.exception))
        self.assertFalse(self.store.get('bridge','status')['ok'])

    def test_unavailable_checkout_does_not_disable_control_bot(self):
        self.opener.failures=[URLError('offline'),URLError('offline')]
        self.assertFalse(self.bridge.mark_uncertain('closed'))

    def test_url_is_retried_inside_the_next_snapshot(self):
        self.opener.failures=[URLError('offline'),URLError('offline')]
        self.assertFalse(self.bridge.set('system','catalog_url','https://t.me/c/123/4'))
        self.bridge.put_catalog([PRODUCT],confirmed=True)
        self.assertEqual(self.opener.bodies[-1]['catalog_url'],'https://t.me/c/123/4')

    def test_revisions_survive_restart_and_clock_rollback(self):
        with patch('catalog_bridge.time.time_ns',return_value=100_000):
            self.bridge.put_catalog([PRODUCT],confirmed=True)
            replacement=CatalogBridge(self.store,'https://checkout.example.test',KEY)
            replacement.put_catalog([PRODUCT],confirmed=True)
        self.assertGreater(self.opener.bodies[1]['revision'],self.opener.bodies[0]['revision'])

    def test_checkout_database_is_never_touched_locally(self):
        self.bridge.put_catalog([PRODUCT],confirmed=True)
        self.assertFalse(self.store.scan('catalog'))
        self.assertFalse(self.store.scan('orders'))
        with self.assertRaises(ValueError):
            self.bridge.set('orders','someone',{'secret':1})

    def test_redirect_does_not_forward_authentication(self):
        handler=NoRedirect()
        request=Request('https://checkout.example.test',headers={'Authorization':'Bearer '+KEY})
        self.assertIsNone(handler.redirect_request(request,None,302,'redirect',{},'https://evil.test'))

    def test_config_accepts_https_and_loopback_only(self):
        validate_config('https://checkout.example.test/',KEY)
        validate_config('http://127.0.0.1:8080',KEY)
        for url in ['http://checkout.example.test','https://user:pass@checkout.example.test',
                    'https://checkout.example.test/api','https://checkout.example.test?key=1','']:
            with self.assertRaises(ValueError):
                validate_config(url,KEY)
