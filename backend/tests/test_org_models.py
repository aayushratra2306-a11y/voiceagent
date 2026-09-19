"""Every stored record type carries org_id, or says in writing why not.

This is what catches a new model — or the unmerged phase-6 branch's
knowledge_sources / consent_records / guardrail_incidents — being added
without organisation scoping. A model not on the exempt list and without
an org_id field fails here, before it can leak across customers.
"""

from app.models.registry import ALL_MODELS, ORG_EXEMPT_MODELS


def test_every_model_has_org_id_or_a_written_exemption():
    missing = [
        m.__name__ for m in ALL_MODELS
        if m not in ORG_EXEMPT_MODELS and "org_id" not in m.model_fields
    ]
    assert missing == [], f"add org_id (or an exemption with a reason) to: {missing}"


def test_every_exemption_gives_a_reason():
    assert all(isinstance(r, str) and len(r) > 10 for r in ORG_EXEMPT_MODELS.values())


def test_the_app_and_the_tests_register_the_same_models():
    import inspect

    import main
    from tests import conftest

    assert "ALL_MODELS" in inspect.getsource(main.lifespan)
    assert "ALL_MODELS" in inspect.getsource(conftest._test_db)
