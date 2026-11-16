"""Three roles share instrumentation and an image; SERVICE selects the role."""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from uuid import UUID, uuid4

import asyncpg
import httpx
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Request, Response
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic import BaseModel, Field
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE = os.getenv("SERVICE", "gateway")
ORDERS_URL = os.getenv("ORDERS_URL", "http://orders:8000")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory:8000")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://platform:platform@postgres:5432/platform")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
FAULT_DELAY_MS = max(0, int(os.getenv("FAULT_DELAY_MS", "0")))
INVENTORY_CONCURRENCY = max(1, int(os.getenv("INVENTORY_CONCURRENCY", "4")))
log = logging.getLogger("platform")
provider = TracerProvider(resource=Resource.create({"service.name": SERVICE}))
if os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)
HTTPXClientInstrumentor().instrument()
AsyncPGInstrumentor().instrument()
REQUESTS = Counter("platform_http_requests_total", "HTTP requests", ["service", "method", "route", "status"])
LATENCY = Histogram("platform_http_request_duration_seconds", "HTTP request duration", ["service", "method", "route"], buckets=(.005,.01,.025,.05,.1,.25,.5,1,2,5,10))
slots = asyncio.Semaphore(INVENTORY_CONCURRENCY)

class OrderIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    quantity: int = Field(ge=1, le=100)

async def outbox_loop(app):
    while True:
        try:
            async with app.state.db.acquire() as conn:
                async with conn.transaction():
                    rows = await conn.fetch("SELECT id, payload FROM outbox WHERE sent_at IS NULL ORDER BY created_at LIMIT 50 FOR UPDATE SKIP LOCKED")
                    for row in rows:
                        await app.state.redis.xadd("orders", {"payload": row["payload"]})
                        await conn.execute("UPDATE outbox SET sent_at=now() WHERE id=$1", row["id"])
        except Exception:
            log.exception("outbox publisher failed; retrying")
        await asyncio.sleep(0.5)

async def process_message(app, message_id, fields):
    event = json.loads(fields["payload"])
    context = propagate.extract(event.get("trace", {}))
    with tracer.start_as_current_span("process order", context=context):
        # Replays are harmless: the status transition is idempotent.
        await app.state.db.execute("UPDATE orders SET status='processed' WHERE id=$1", UUID(event["order_id"]))
        await app.state.redis.xack("orders", "processors", message_id)

async def worker_loop(app):
    consumer = str(uuid4())
    while True:
        try:
            # Recover messages abandoned by crashed consumers after 30 seconds.
            claimed = await app.state.redis.xautoclaim("orders", "processors", consumer, 30000, "0-0", count=20)
            for message_id, fields in claimed[1]:
                await process_message(app, message_id, fields)
            batches = await app.state.redis.xreadgroup("processors", consumer, {"orders": ">"}, count=20, block=1000)
            for _, messages in batches:
                for message_id, fields in messages:
                    await process_message(app, message_id, fields)
        except Exception:
            log.exception("order worker failed; retrying")
            await asyncio.sleep(1)

@asynccontextmanager
async def lifespan(app):
    app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=1.0))
    tasks = []
    try:
        if SERVICE == "orders":
            app.state.db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10, command_timeout=3)
            await app.state.db.execute("""CREATE TABLE IF NOT EXISTS orders (id uuid PRIMARY KEY, sku text NOT NULL, quantity integer NOT NULL, status text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
                CREATE TABLE IF NOT EXISTS outbox (id uuid PRIMARY KEY, payload text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), sent_at timestamptz)""")
            app.state.redis = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2, socket_timeout=3)
            await app.state.redis.ping()
            try:
                await app.state.redis.xgroup_create("orders", "processors", id="0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            tasks = [asyncio.create_task(outbox_loop(app)), asyncio.create_task(worker_loop(app))]
        yield
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if hasattr(app.state, "redis"):
            await app.state.redis.aclose()
        if hasattr(app.state, "db"):
            await app.state.db.close()
        await app.state.client.aclose()
        provider.force_flush(timeout_millis=3000)

app = FastAPI(title=f"Reliability demo: {SERVICE}", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app, excluded_urls="health/live,health/ready,metrics")

@app.middleware("http")
async def observe(request: Request, call_next):
    if request.url.path in ("/metrics", "/health/live", "/health/ready"):
        return await call_next(request)
    start = time.monotonic()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        REQUESTS.labels(SERVICE, request.method, route, str(status)).inc()
        LATENCY.labels(SERVICE, request.method, route).observe(time.monotonic() - start)

@app.get("/health/live")
async def live():
    return {"status": "alive"}

@app.get("/health/ready")
async def ready():
    if SERVICE == "orders":
        try:
            await app.state.db.fetchval("SELECT 1")
            await app.state.redis.ping()
        except Exception:
            raise HTTPException(503, "dependency unavailable")
    return {"status": "ready"}

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

async def request_json(method, url, **kwargs):
    try:
        response = await app.state.client.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(exc.response.status_code, f"upstream error: {exc.response.status_code}")
    except httpx.RequestError:
        raise HTTPException(503, "upstream unavailable or timed out")

if SERVICE == "gateway":
    @app.post("/orders", status_code=202)
    async def create_order(order: OrderIn):
        return await request_json("POST", f"{ORDERS_URL}/orders", json=order.model_dump())

    @app.get("/orders/{order_id}")
    async def get_order(order_id: UUID):
        return await request_json("GET", f"{ORDERS_URL}/orders/{order_id}")

elif SERVICE == "orders":
    @app.post("/orders", status_code=202)
    async def create_order(order: OrderIn):
        available = await request_json("GET", f"{INVENTORY_URL}/inventory/{order.sku}")
        if available["available"] < order.quantity:
            raise HTTPException(409, "insufficient inventory")
        order_id = uuid4()
        carrier = {}
        propagate.inject(carrier)
        event = json.dumps({"order_id": str(order_id), "trace": carrier})
        async with app.state.db.acquire() as conn:
            async with conn.transaction():
                await conn.execute("INSERT INTO orders (id, sku, quantity, status) VALUES ($1,$2,$3,'pending')", order_id, order.sku, order.quantity)
                await conn.execute("INSERT INTO outbox (id, payload) VALUES ($1,$2)", order_id, event)
        return {"id": str(order_id), "status": "pending"}

    @app.get("/orders/{order_id}")
    async def get_order(order_id: UUID):
        row = await app.state.db.fetchrow("SELECT id,sku,quantity,status FROM orders WHERE id=$1", order_id)
        if not row:
            raise HTTPException(404, "order not found")
        return {**dict(row), "id": str(row["id"])}

elif SERVICE == "inventory":
    @app.get("/inventory/{sku}")
    async def inventory(sku: str):
        with tracer.start_as_current_span("inventory capacity wait"):
            async with slots:
                if FAULT_DELAY_MS:
                    await asyncio.sleep(FAULT_DELAY_MS / 1000)
        return {"sku": sku, "available": 100}
else:
    raise RuntimeError(f"Unknown SERVICE: {SERVICE}")
