import inspect

import pytest

from app.models.appointment import Appointment
from app.models.bot import Bot
from app.pipeline import booking, call_context

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _Params:
    result = None

    async def result_callback(self, result):
        self.result = result


def test_the_pipeline_accepts_org_id_so_the_worker_cannot_crash_on_it():
    """call_worker runs run_voice_pipeline(**bot_config). A key it doesn't
    accept is a TypeError swallowed by 'pipeline crashed' — a silent call."""
    from app.pipeline.voice_pipeline import run_voice_pipeline
    assert "org_id" in inspect.signature(run_voice_pipeline).parameters


def test_connect_puts_org_id_in_bot_config():
    from app.api import connect
    assert '"org_id": bot.org_id' in inspect.getsource(connect.connect)


async def test_a_booking_is_stamped_with_the_calls_org():
    bot = Bot(user_id="u", org_id="org-call-1", name="b", timezone="Asia/Kolkata")
    await bot.insert()
    call_context.set_call(bot_id=str(bot.id), session_id="s", org_id="org-call-1")
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    date = (datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(days=3)).strftime("%Y-%m-%d")

    p = _Params()
    await booking.book_appointment(p, date=date, time="10:00", purpose="t")

    appt = await Appointment.find_one(Appointment.reference == p.result["reference"])
    assert appt.org_id == "org-call-1"
    from app.db.mongo import database
    slot = await database["booking_slots"].find_one({"_id": appt.slot_key})
    assert slot["org_id"] == "org-call-1"
    call_context.clear()


async def test_ice_candidates_for_someone_elses_call_are_ignored(client):
    from app.api import connect
    from tests.conftest import auth_headers, make_user

    token = await make_user("ice-1@voiceagent-test.com")

    class _Q:
        items = []

        def put(self, item):
            self.items.append(item)

    q = _Q()
    connect._active_calls["pc-not-yours"] = type("C", (), {"user_id": "someone-else", "ice_queue": q})()
    try:
        r = await client.post(
            "/connect/ice", json={"pc_id": "pc-not-yours", "candidates": []}, headers=auth_headers(token)
        )
        assert r.status_code == 200 and q.items == []
    finally:
        connect._active_calls.pop("pc-not-yours", None)
