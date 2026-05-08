import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.risk_rules import create_risk_rule, delete_risk_rule, list_risk_rules, update_risk_rule
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import AnalysisTask, RiskRecord, RiskRule
from smart_badge_api.schemas.risk import RiskRuleCreate, RiskRuleUpdate


def test_risk_rule_defaults_are_seeded() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                rows = await list_risk_rules(db=db)
                assert len(rows) >= 4
                assert {item.match_type for item in rows} >= {
                    "overall_score_below",
                    "dimension_score_below",
                    "concern_keyword",
                }
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_risk_rule_update_and_delete_purge_linked_records() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                rule = await create_risk_rule(
                    RiskRuleCreate(
                        name="custom-risk-rule",
                        match_type="overall_score_below",
                        severity="medium",
                        risk_label="Custom Risk",
                        description="Custom test rule",
                        match_config={"threshold": 5.0},
                        note="test",
                        is_active=True,
                    ),
                    db=db,
                )

                db.add(
                    AnalysisTask(
                        id="task001",
                        file_name="recording_001.json",
                        file_path="uploads/analysis_input/recording_001.json",
                        status="done",
                        overall_score=4.5,
                    )
                )
                await db.flush()

                db.add(
                    RiskRecord(
                        rule_id=rule.id,
                        task_id="task001",
                        source_type="uploaded_json",
                        rule_name=rule.name,
                        risk_label=rule.risk_label,
                        severity=rule.severity,
                        summary="old record",
                        evidence={"source": "test"},
                    )
                )
                await db.commit()

                updated = await update_risk_rule(
                    rule.id,
                    RiskRuleUpdate(
                        severity="high",
                        risk_label="Escalated Risk",
                        match_config={"threshold": 4.0},
                    ),
                    db=db,
                )
                assert updated.severity == "high"
                assert updated.risk_label == "Escalated Risk"

                linked_records = (
                    await db.execute(select(RiskRecord).where(RiskRecord.rule_id == rule.id))
                ).scalars().all()
                assert linked_records == []

                db.add(
                    RiskRecord(
                        rule_id=rule.id,
                        task_id="task001",
                        source_type="uploaded_json",
                        rule_name=updated.name,
                        risk_label=updated.risk_label,
                        severity=updated.severity,
                        summary="record after update",
                        evidence={"source": "test"},
                    )
                )
                await db.commit()

                await delete_risk_rule(rule.id, db=db)

                assert await db.get(RiskRule, rule.id) is None
                remaining_records = (
                    await db.execute(select(RiskRecord).where(RiskRecord.rule_id == rule.id))
                ).scalars().all()
                assert remaining_records == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())
