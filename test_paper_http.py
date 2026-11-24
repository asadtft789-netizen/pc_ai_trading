"""Optional real localhost HTTP smoke; never uses a broker or non-disposable DB."""
import importlib.util, json, os, socket, tempfile, threading, time, unittest, urllib.request
from pathlib import Path
from unittest.mock import patch
from paper_store import init

@unittest.skipUnless(all((importlib.util.find_spec(m) for m in ['fastapi', 'uvicorn', 'plotly'])), 'dashboard runtime dependencies unavailable')
class PaperHTTP(unittest.TestCase):

    def test_dashboard_and_health(self):
        import uvicorn
        from dashboard.app import app
        with tempfile.TemporaryDirectory() as d, socket.socket() as sock:
            path = Path(d) / 'health.sqlite'
            sock.bind(('127.0.0.1', 0))
            sock.listen(8)
            base = 'http://127.0.0.1:' + str(sock.getsockname()[1])
            server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            with patch.dict(os.environ, {'PAPER_DB': str(path)}):
                thread.start()
                try:
                    for _ in range(120):
                        if server.started:
                            break
                        time.sleep(0.025)
                    self.assertTrue(server.started)
                    with self.assertRaises(urllib.error.HTTPError) as err:
                        urllib.request.urlopen(base + '/api/health', timeout=3)
                    self.assertEqual(err.exception.code, 503)
                    init(path)
                    for route in ['', 'api/health', 'api/hourly', 'api/price-chart/BTC/USD', 'api/metrics', 'assets/plotly.js']:
                        with urllib.request.urlopen(base + '/' + route, timeout=5) as r:
                            self.assertEqual(r.status, 200)
                finally:
                    server.should_exit = True
                    thread.join(5)
