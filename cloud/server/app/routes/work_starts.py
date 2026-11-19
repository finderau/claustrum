from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.auth import current_user
from app.models import WorkStartRequest, WorkStartResponse, WorkStartMatch

router = APIRouter()


@router.post("/work-start", response_model=WorkStartResponse)
async def work_start(req: WorkStartRequest, user_email: str = Depends(current_user)) -> WorkStartResponse:
    async with db.conn() as c:
        async with c.cursor() as cur:
            keys = sorted({f"prompt:{req.prompt_hash}"} | (
                {f"plan:{req.plan_key}"} if req.plan_key else set()
            ))
            for key in keys:
                await cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))

            await cur.execute(
                "SELECT user_email FROM work_starts WHERE uid = %s AND opening_id = %s",
                (req.uid, req.opening_id),
            )
            existing = await cur.fetchone()
            if existing:
                if existing[0] != user_email:
                    raise HTTPException(status_code=409, detail="Session ID already belongs to another user")
                if req.acknowledge:
                    await cur.execute(
                        "UPDATE work_starts SET acknowledged = true "
                        "WHERE uid = %s AND opening_id = %s", (req.uid, req.opening_id),
                    )
                await c.commit()
                return WorkStartResponse()

            match = None
            if not req.acknowledge:
                await cur.execute(
                    """
                    SELECT uid, user_email, machine, label, repo, branch, first_at
                    FROM work_starts
                    WHERE acknowledged AND first_at >= now() - interval '7 days'
                      AND (prompt_hash = %(prompt_hash)s
                           OR (%(plan_key)s IS NOT NULL AND plan_key = %(plan_key)s))
                    ORDER BY first_at DESC LIMIT 1
                    """,
                    {"prompt_hash": req.prompt_hash, "plan_key": req.plan_key},
                )
                row = await cur.fetchone()
                if row:
                    match = WorkStartMatch(
                        uid=row[0], user_email=row[1], machine=row[2],
                        label=row[3], repo=row[4], branch=row[5], first_at=row[6],
                    )

            await cur.execute(
                """
                INSERT INTO work_starts
                    (uid, opening_id, user_email, machine, label, prompt_hash, plan_key,
                     repo, branch, acknowledged)
                VALUES (%(uid)s, %(opening_id)s, %(user_email)s, %(machine)s, %(label)s,
                        %(prompt_hash)s, %(plan_key)s, %(repo)s, %(branch)s,
                        %(acknowledged)s)
                """,
                {**req.model_dump(), "user_email": user_email,
                 "acknowledged": match is None},
            )
        await c.commit()
    return WorkStartResponse(match=match)
