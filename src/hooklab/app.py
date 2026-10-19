"""FastAPI application. No real network deliveries or third-party data."""

from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from hooklab.database import connect, initialize


class EventInput(BaseModel):
    event_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)


class DeliveryInput(BaseModel):
    behavior: Literal["success", "fail-once", "always-fail"] = "success"


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize()
    yield


app = FastAPI(title="HookLab", version="0.1.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/events", status_code=201)
def create_event(
    data: EventInput,
    response: Response,
    idempotency_key: str | None = Header(default=None, min_length=1, max_length=128),
):
    """Create an event; replays with an identical idempotency key are safe.

    SQLite's unique index enforces uniqueness even under concurrent requests.
    A repeated key with different content is a conflict, not a silent overwrite.
    """
    payload_json = json.dumps(data.payload, sort_keys=True)
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key:
            raise HTTPException(status_code=422, detail="Idempotency-Key cannot be blank")

    with connect() as conn:
        if idempotency_key is None:
            cursor = conn.execute(
                "INSERT INTO events (event_type, payload) VALUES (?, ?)",
                (data.event_type, payload_json),
            )
            event_id = cursor.lastrowid
        else:
            cursor = conn.execute(
                """INSERT INTO events (event_type, payload, idempotency_key)
                   VALUES (?, ?, ?) ON CONFLICT DO NOTHING""",
                (data.event_type, payload_json, idempotency_key),
            )
            if cursor.rowcount:
                event_id = cursor.lastrowid
            else:
                existing = conn.execute(
                    "SELECT * FROM events WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("Idempotency conflict without a matching event")
                if (existing["event_type"] != data.event_type
                        or json.loads(existing["payload"]) != data.payload):
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency-Key already used for a different event",
                    )
                event_id = existing["id"]
                response.status_code = 200
    return get_event(event_id)

def _event_as_dict(row):
    return {"id": row["id"], "event_type": row["event_type"],
            "payload": json.loads(row["payload"]), "created_at": row["created_at"]}


@app.get("/api/events")
def list_events(
    limit: int = 50,
    event_type: str | None = Query(default=None, min_length=1, max_length=100),
):
    """List events, optionally matching an exact event type.

    The filter uses a bound SQL parameter so user input is never SQL code.
    """
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    with connect() as conn:
        if event_type is None:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events WHERE event_type = ? ORDER BY id DESC LIMIT ?",
                (event_type, limit),
            ).fetchall()
    return [_event_as_dict(row) for row in rows]


@app.get("/api/events/{event_id}")
def get_event(event_id: int):
    with connect() as conn:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return _event_as_dict(row)


def _delivery_as_dict(row):
    return {key: row[key] for key in row.keys()}


@app.post("/api/events/{event_id}/deliveries", status_code=201)
def create_delivery(event_id: int, data: DeliveryInput):
    """Create and run one synthetic delivery attempt, never send real requests."""
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Event not found")
        cursor = conn.execute(
            "INSERT INTO deliveries (event_id, behavior, status) VALUES (?, ?, 'pending')",
            (event_id, data.behavior),
        )
        delivery_id = cursor.lastrowid
    return retry_delivery(delivery_id)


@app.post("/api/deliveries/{delivery_id}/retry")
def retry_delivery(delivery_id: int):
    """Deterministic success/failure simulator for testing retry behavior."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Delivery not found")
        attempts = row["attempts"] + 1
        success = row["behavior"] == "success" or (
            row["behavior"] == "fail-once" and attempts > 1
        )
        conn.execute(
            """UPDATE deliveries SET status = ?, attempts = ?, error = ?,
               updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?""",
            ("delivered" if success else "failed", attempts,
             None if success else "Simulated delivery failure", delivery_id),
        )
        result = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
    return _delivery_as_dict(result)


@app.get("/api/deliveries")
def list_deliveries():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM deliveries ORDER BY id DESC LIMIT 100").fetchall()
    return [_delivery_as_dict(row) for row in rows]
