import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ['SERVICE'] = 'inventory'
os.environ['FAULT_DELAY_MS'] = '0'
from app import main
from fastapi.testclient import TestClient

class InventoryTests(unittest.TestCase):
    def test_inventory_health_and_metrics(self):
        with TestClient(main.app) as client:
            self.assertEqual(client.get('/health/ready').status_code, 200)
            self.assertEqual(client.get('/inventory/widget').json(), {'sku': 'widget', 'available': 100})
            metrics = client.get('/metrics').text
            self.assertIn('platform_http_requests_total', metrics)
            self.assertIn('route="/inventory/{sku}"', metrics)
            self.assertNotIn('route="/inventory/widget"', metrics)

    def test_worker_acknowledges_after_database_update(self):
        async def check():
            app = type('App', (), {})()
            app.state = type('State', (), {})()
            app.state.db = type('DB', (), {'execute': AsyncMock()})()
            app.state.redis = type('Redis', (), {'xack': AsyncMock()})()
            await main.process_message(app, '1-0', {'payload': '{"order_id":"12345678-1234-5678-1234-567812345678"}'})
            app.state.db.execute.assert_awaited_once()
            app.state.redis.xack.assert_awaited_once_with('orders', 'processors', '1-0')
            app.state.db.execute.side_effect = RuntimeError('database down')
            app.state.redis.xack.reset_mock()
            with self.assertRaises(RuntimeError):
                await main.process_message(app, '2-0', {'payload': '{"order_id":"12345678-1234-5678-1234-567812345678"}'})
            app.state.redis.xack.assert_not_awaited()
        asyncio.run(check())

if __name__ == '__main__':
    unittest.main()
