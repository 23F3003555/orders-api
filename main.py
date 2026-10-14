
import base64
import time
import uuid
from collections import defaultdict, deque
from threading import Lock
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


app = FastAPI(title="Orders API")

TOTAL_ORDERS = 46
RATE_LIMIT = 16
WINDOW_SECONDS = 10

idempotency_store = {}
rate_buckets = defaultdict(deque)
state_lock = Lock()


class OrderInput(BaseModel):
    product: str = "sample-product"
    quantity: int = Field(default=1, ge=1)


def check_rate_limit(client_id: str):
    now = time.monotonic()

    with state_lock:
        bucket = rate_buckets[client_id]

        while bucket and now - bucket[0] >= WINDOW_SECONDS:
            bucket.popleft()

        if len(bucket) >= RATE_LIMIT:
            retry_after = max(
                1,
                int(WINDOW_SECONDS - (now - bucket[0]) + 0.999),
            )
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

        bucket.append(now)


@app.middleware("http")
async def client_rate_limit(request: Request, call_next):
    # CORS preflight requests do not consume rate-limit quota.
    if request.method != "OPTIONS":
        client_id = request.headers.get("X-Client-Id", "anonymous")

        try:
            check_rate_limit(client_id)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )

    return await call_next(request)


@app.get("/")
def root():
    return {
        "service": "orders-api",
        "total_orders": TOTAL_ORDERS,
        "rate_limit": RATE_LIMIT,
        "window_seconds": WINDOW_SECONDS,
    }


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.post("/orders", status_code=201)
def create_order(
    body: OrderInput,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
    ),
):
    with state_lock:
        if idempotency_key in idempotency_store:
            return idempotency_store[idempotency_key]

        order = {
            "id": str(uuid.uuid4()),
            "product": body.product,
            "quantity": body.quantity,
        }

        idempotency_store[idempotency_key] = order
        return order


def encode_cursor(offset: int) -> str:
    raw = str(offset).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        offset = int(
            base64.urlsafe_b64decode(
                padded.encode("ascii")
            ).decode("utf-8")
        )

        if offset < 0 or offset > TOTAL_ORDERS:
            raise ValueError("Invalid cursor")

        return offset

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid cursor",
        )


@app.get("/orders")
def list_orders(
    limit: int = Query(default=10, ge=1, le=46),
    cursor: Optional[str] = None,
):
    offset = decode_cursor(cursor) if cursor else 0

    catalog = [
        {
            "id": order_id,
            "product": f"product-{order_id}",
            "quantity": 1,
        }
        for order_id in range(1, TOTAL_ORDERS + 1)
    ]

    items = catalog[offset:offset + limit]
    next_offset = offset + len(items)

    next_cursor = (
        encode_cursor(next_offset)
        if next_offset < TOTAL_ORDERS
        else None
    )

    return {
        "items": items,
        "next_cursor": next_cursor,
    }


# Wrap the complete app so CORS headers also apply to error responses.
app = CORSMiddleware(
    app=app,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
