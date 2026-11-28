from pathlib import Path
import sqlite3
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
ROOT = Path(__file__).resolve().parent
from dashboard.paper_view import read_view
app = FastAPI(title='Hourly paper trading monitor', docs_url=None, redoc_url=None, openapi_url=None)
# The private tunnel connects to the loopback listener. Deployments may
# explicitly allow the hostname assigned by their authenticated VPN.
import os
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[
    "127.0.0.1", "localhost", *os.getenv("PHOENIX_DASHBOARD_HOSTS", "").split(),
])

@app.middleware("http")
async def private_response(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

app.mount('/static', StaticFiles(directory=str(ROOT / 'static')), name='static')

def view(route, arg=None):
    try:
        return read_view(route, arg)
    except (sqlite3.Error, ValueError):
        raise HTTPException(503, 'Paper database unavailable') from None

@app.get('/assets/plotly.js')
def plotly_asset():
    from plotly import __file__ as location
    return FileResponse(str(Path(location).parent / 'package_data/plotly.min.js'), media_type='application/javascript')

@app.get('/')
def home():
    return FileResponse(str(ROOT / 'templates/index.html'))

@app.get('/api/price-chart/{symbol:path}')
def price(symbol: str):
    return view('price', symbol)

@app.get('/api/trades')
def trades(limit: int=Query(50, ge=1, le=200)):
    return view('trades', limit)

@app.get('/api/lessons')
def lessons(limit: int=Query(20, ge=1, le=200)):
    return view('lessons', limit)

def handler(name):

    def read():
        return view(name)
    return read
for name in ['metrics', 'strategy', 'strategies', 'regime', 'rules', 'equity-curve', 'mode', 'health', 'hourly', 'symbols']:
    app.add_api_route('/api/' + name, handler(name), methods=['GET'])
if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8000)
