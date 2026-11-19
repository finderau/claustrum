"""Company-wide duplicate opening briefs, with optional Postgres integration."""

import asyncio
import hashlib
import os
import uuid

import pytest
from pydantic import ValidationError

from app.models import WorkStartRequest


def test_work_start_accepts_only_opaque_fingerprints():
    good = hashlib.sha256(b"substantial opening brief").hexdigest()
    assert WorkStartRequest(uid="session", machine="host", prompt_hash=good).plan_key is None
    with pytest.raises(ValidationError):
        WorkStartRequest(uid="session", machine="host", prompt_hash="a raw prompt")
    with pytest.raises(ValidationError):
        WorkStartRequest(uid="session", machine="host", prompt_hash=good, plan_key="docs/plan.md")


DB_URL = os.environ.get("CLAUSTRUM_TEST_DB_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="CLAUSTRUM_TEST_DB_URL not set")


@pytest.fixture
async def api(monkeypatch):
    import httpx

    from app import db, migrate
    from app.main import app

    monkeypatch.setenv("CLAUSTRUM_DB_URL", DB_URL)
    migrate.main()
    await db.open_pool()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    await db.close_pool()


@needs_db
async def test_cross_user_match_and_intentional_retry(api):
    run = uuid.uuid4().hex
    prompt_hash = hashlib.sha256(run.encode()).hexdigest()
    plan_key = hashlib.sha256((run + ":plan").encode()).hexdigest()
    first = f"first-{run}@finder.com"
    second = f"second-{run}@finder.com"

    async def post(user, uid, *, prompt=prompt_hash, plan=plan_key, acknowledge=False):
        response = await api.post(
            "/v1/work-start", headers={"X-Claustrum-User-Email": user},
            json={"uid": uid, "machine": "laptop", "label": uid,
                  "prompt_hash": prompt, "plan_key": plan, "repo": "workspace-automation",
                  "branch": "docs/bot-migration", "acknowledge": acknowledge},
        )
        assert response.status_code == 200, response.text
        return response.json()

    try:
        assert (await post(first, f"first-{run}"))["match"] is None
        changed = hashlib.sha256((run + ":changed wording").encode()).hexdigest()
        match = (await post(second, f"second-{run}", prompt=changed))["match"]
        assert match["uid"] == f"first-{run}" and match["user_email"] == first
        assert (await post(second, f"second-{run}", prompt=changed, acknowledge=True))["match"] is None
        third = (await post(first, f"third-{run}", prompt=changed))["match"]
        assert third["uid"] == f"second-{run}"
    finally:
        for user in (first, second):
            await api.post("/v1/reset", headers={"X-Claustrum-User-Email": user})


@needs_db
async def test_simultaneous_cross_user_start_blocks_one(api):
    run = uuid.uuid4().hex
    prompt_hash = hashlib.sha256(run.encode()).hexdigest()

    async def start(n):
        return await api.post(
            "/v1/work-start", headers={"X-Claustrum-User-Email": f"{n}-{run}@finder.com"},
            json={"uid": f"{n}-{run}", "machine": "laptop", "prompt_hash": prompt_hash},
        )

    try:
        replies = await asyncio.gather(start("one"), start("two"))
        assert [r.status_code for r in replies] == [200, 200]
        assert sum(r.json()["match"] is not None for r in replies) == 1
    finally:
        for n in ("one", "two"):
            await api.post("/v1/reset", headers={"X-Claustrum-User-Email": f"{n}-{run}@finder.com"})
