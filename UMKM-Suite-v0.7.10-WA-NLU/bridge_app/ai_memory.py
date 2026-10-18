"""Small, tenant-isolated conversation memory. No financial writes occur here."""
import json
import os
from datetime import datetime, timedelta, timezone

from bridge_app import db

CHANNEL = "whatsapp"
ENABLED = os.getenv("AI_MEMORY_ENABLED", "1").lower() not in {"0", "false", "no", "off"}
MAX_TURNS = max(1, min(int(os.getenv("AI_MEMORY_TURNS", "6")), 8))
TTL_MINUTES = max(10, min(int(os.getenv("AI_MEMORY_TTL_MINUTES", "90")), 720))


def _decode(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    result = []
    for entry in value[-2 * MAX_TURNS:]:
        if not isinstance(entry, dict) or entry.get("role") not in {"user", "assistant"}:
            continue
        content = str(entry.get("content") or "").strip()[:900]
        if content:
            result.append({"role": entry["role"], "content": content})
    return result


async def read(identity):
    if not ENABLED:
        return []
    async with db.tenant_tx(identity) as (conn, ctx):
        if ctx.get("support_mode"):
            return []
        row = await conn.fetchrow(
            """SELECT messages FROM ai_dialogues
               WHERE tenant_id=$1 AND user_id=$2 AND channel=$3
                 AND member_role=$4
                 AND updated_at >= NOW() - make_interval(mins => $5)""",
            ctx["tenant_id"], ctx["user_id"], CHANNEL, str(ctx["role"]), TTL_MINUTES,
        )
        return _decode(row["messages"]) if row else []


async def save(identity, user_text, assistant_text):
    if not ENABLED or not assistant_text:
        return
    new_turns = [
        {"role": "user", "content": str(user_text).strip()[:900]},
        {"role": "assistant", "content": str(assistant_text).strip()[:900]},
    ]
    if not new_turns[0]["content"]:
        return
    async with db.tenant_tx(identity) as (conn, ctx):
        if ctx.get("support_mode"):
            return
        tenant, user, role = ctx["tenant_id"], ctx["user_id"], str(ctx["role"])
        # Role and RLS restrict both reads and writes; FOR UPDATE prevents
        # lost updates when messages arrive concurrently on one channel.
        await conn.execute(
            """INSERT INTO ai_dialogues(tenant_id,user_id,channel,member_role,messages)
               VALUES ($1,$2,$3,$4,'[]'::jsonb)
               ON CONFLICT (tenant_id,user_id,channel) DO NOTHING""",
            tenant, user, CHANNEL, role,
        )
        current = await conn.fetchrow(
            """SELECT messages, member_role, updated_at
               FROM ai_dialogues
               WHERE tenant_id=$1 AND user_id=$2 AND channel=$3 FOR UPDATE""",
            tenant, user, CHANNEL,
        )
        if not current:
            return
        fresh = current["member_role"] == role and current["updated_at"] >= (
            datetime.now(timezone.utc) - timedelta(minutes=TTL_MINUTES)
        )
        history = _decode(current["messages"]) if fresh else []
        combined = (history + new_turns)[-2 * MAX_TURNS:]
        await conn.execute(
            """UPDATE ai_dialogues
               SET messages=$4::jsonb, member_role=$5, updated_at=NOW()
               WHERE tenant_id=$1 AND user_id=$2 AND channel=$3""",
            tenant, user, CHANNEL, json.dumps(combined, ensure_ascii=False), role,
        )


async def clear(identity):
    async with db.tenant_tx(identity) as (conn, ctx):
        if ctx.get("support_mode"):
            return
        await conn.execute(
            "DELETE FROM ai_dialogues WHERE tenant_id=$1 AND user_id=$2 AND channel=$3",
            ctx["tenant_id"], ctx["user_id"], CHANNEL,
        )
