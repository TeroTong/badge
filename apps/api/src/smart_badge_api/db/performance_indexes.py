from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from smart_badge_api.core.config import get_settings

logger = logging.getLogger("smart_badge.performance_indexes")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class IndexSpec:
    name: str
    table: str
    expressions: tuple[str, ...]
    required_columns: tuple[str, ...] | None = None


INDEXES: tuple[IndexSpec, ...] = (
    IndexSpec("ix_perf_staff_hospital_active_name", "staff", ("hospital_code", "is_active", "name")),
    IndexSpec("ix_perf_staff_hospital_active_code", "staff", ("hospital_code", "is_active", "external_account")),
    IndexSpec("ix_perf_staff_hospital_badge", "staff", ("hospital_code", "badge_id")),
    IndexSpec(
        "ix_perf_management_manager_hospital",
        "staff_management_relations",
        ("manager_staff_id", "hospital_code"),
    ),
    IndexSpec(
        "ix_perf_management_subordinate_hospital",
        "staff_management_relations",
        ("subordinate_staff_id", "hospital_code"),
    ),
    IndexSpec(
        "ix_perf_device_binding_staff_active",
        "device_staff_bindings",
        ("staff_id", "effective_to", "effective_from DESC"),
    ),
    IndexSpec("ix_perf_devices_staff_updated", "devices", ("staff_id", "updated_at DESC", "created_at DESC")),
    IndexSpec("ix_perf_recordings_staff_created", "recordings", ("staff_id", "created_at DESC")),
    IndexSpec("ix_perf_recordings_visit_created", "recordings", ("visit_id", "created_at DESC")),
    IndexSpec("ix_perf_recordings_status_created", "recordings", ("status", "created_at DESC")),
    IndexSpec("ix_perf_recordings_device_created", "recordings", ("device_id", "created_at DESC")),
    IndexSpec("ix_perf_recordings_file_name", "recordings", ("file_name",)),
    IndexSpec("ix_perf_recordings_created", "recordings", ("created_at DESC", "id")),
    IndexSpec("ix_perf_transcripts_recording_status", "transcripts", ("recording_id", "status")),
    IndexSpec("ix_perf_segments_recording_index", "segments", ("recording_id", "segment_index")),
    IndexSpec("ix_perf_analysis_tasks_file_status", "analysis_tasks", ("file_name", "status")),
    IndexSpec("ix_perf_analysis_tasks_status_created", "analysis_tasks", ("status", "created_at DESC")),
    IndexSpec(
        "ix_perf_analysis_tasks_status_completed_created",
        "analysis_tasks",
        ("status", "completed_at DESC", "created_at DESC", "file_name"),
    ),
    IndexSpec(
        "ix_perf_analysis_tasks_status_score_file",
        "analysis_tasks",
        ("status", "overall_score DESC", "file_name"),
    ),
    IndexSpec("ix_perf_visits_customer_created", "visits", ("customer_id", "created_at DESC")),
    IndexSpec("ix_perf_visits_customer_date", "visits", ("customer_id", "visit_date DESC", "created_at DESC")),
    IndexSpec("ix_perf_visits_consultant_date", "visits", ("consultant_id", "visit_date DESC", "created_at DESC")),
    IndexSpec("ix_perf_visits_doctor_date", "visits", ("doctor_id", "visit_date DESC", "created_at DESC")),
    IndexSpec("ix_perf_visits_date_time_created", "visits", ("visit_date DESC", "visit_time DESC", "created_at DESC")),
    IndexSpec("ix_perf_visits_status_date_created", "visits", ("status", "visit_date DESC", "created_at DESC")),
    IndexSpec("ix_perf_visits_external_order", "visits", ("external_visit_order_no", "external_visit_order_seg")),
    IndexSpec("ix_perf_visit_orders_jgbm_sjrq", "visit_orders", ("jgbm", "sjrq DESC")),
    IndexSpec("ix_perf_visit_orders_jgbm_crtdt", "visit_orders", ("jgbm", "crtdt DESC")),
    IndexSpec("ix_perf_visit_orders_jgbm_sjrq_fzsj", "visit_orders", ("jgbm", "sjrq DESC", "fzsj DESC", "dzdh")),
    IndexSpec("ix_perf_visit_orders_jgbm_crtdt_fzsj", "visit_orders", ("jgbm", "crtdt DESC", "fzsj DESC", "dzdh")),
    IndexSpec("ix_perf_visit_orders_kunr_sjrq_fzsj", "visit_orders", ("kunr", "sjrq DESC", "fzsj DESC", "dzseg")),
    IndexSpec("ix_perf_visit_orders_jgbm_fzuer_sjrq", "visit_orders", ("jgbm", "fzuer", "sjrq DESC", "fzsj DESC")),
    IndexSpec("ix_perf_visit_orders_jgbm_advxc_sjrq", "visit_orders", ("jgbm", "advxc", "sjrq DESC", "fzsj DESC")),
    IndexSpec("ix_perf_visit_orders_jgbm_assxc_sjrq", "visit_orders", ("jgbm", "assxc", "sjrq DESC", "fzsj DESC")),
    IndexSpec("ix_perf_visit_orders_kunr_jgbm", "visit_orders", ("kunr", "jgbm")),
    IndexSpec("ix_perf_visit_orders_fzuer_jgbm", "visit_orders", ("fzuer", "jgbm")),
    IndexSpec("ix_perf_visit_orders_dfzuer_jgbm", "visit_orders", ("d_fzuer", "jgbm")),
    IndexSpec("ix_perf_visit_orders_advxc_jgbm", "visit_orders", ("advxc", "jgbm")),
    IndexSpec("ix_perf_visit_orders_advyq_jgbm", "visit_orders", ("advyq", "jgbm")),
    IndexSpec("ix_perf_rvl_visit_recording", "recording_visit_links", ("visit_id", "recording_id")),
    IndexSpec("ix_perf_rva_status_updated", "recording_visit_analysis_results", ("analysis_status", "updated_at DESC")),
    IndexSpec("ix_perf_rva_task", "recording_visit_analysis_results", ("analysis_task_id",)),
    IndexSpec("ix_perf_sap_logs_order_created", "sap_push_logs", ("visit_order_no", "visit_order_seg", "created_at DESC")),
    IndexSpec("ix_perf_sap_logs_recording_created", "sap_push_logs", ("recording_id", "created_at DESC")),
    IndexSpec("ix_perf_sap_logs_recording_visit_created", "sap_push_logs", ("recording_id", "visit_id", "created_at DESC")),
    IndexSpec("ix_perf_sap_logs_visit_created", "sap_push_logs", ("visit_id", "created_at DESC")),
    IndexSpec("ix_perf_sap_logs_status_updated", "sap_push_logs", ("status", "updated_at DESC")),
    IndexSpec(
        "ix_perf_sap_logs_send_status_activity",
        "sap_push_logs",
        ("send_enabled", "status", "coalesce(updated_at, sent_at, created_at)", "created_at", "id"),
        required_columns=("send_enabled", "status", "updated_at", "sent_at", "created_at", "id"),
    ),
    IndexSpec("ix_perf_sap_hana_jgbm_crtdt", "sap_hana_visit_orders", ("jgbm", "crtdt DESC")),
    IndexSpec("ix_perf_sap_hana_jgbm_updated", "sap_hana_visit_orders", ("jgbm", "updated_at DESC", "created_at DESC")),
    IndexSpec(
        "ix_perf_sap_hana_jgbm_crtdt_updated",
        "sap_hana_visit_orders",
        ("jgbm", "crtdt", "updated_at DESC", "created_at DESC"),
    ),
    IndexSpec("ix_perf_sap_hana_fzuer_jgbm", "sap_hana_visit_orders", ("fzuer", "jgbm")),
    IndexSpec("ix_perf_sap_hana_retry_kunr", "sap_hana_visit_orders", ("customer_birthday_retry_at", "kunr")),
    IndexSpec("ix_perf_audit_logs_module_created", "audit_logs", ("module_name", "created_at DESC")),
    IndexSpec("ix_perf_audit_logs_operator_created", "audit_logs", ("operator_name", "created_at DESC")),
)


def _column_name(expression: str) -> str:
    return expression.strip().split()[0]


def _validate_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.match(value):
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return value


def _format_expression(expression: str) -> str:
    cleaned = expression.strip()
    if not cleaned:
        raise ValueError("empty index expression")
    order = ""
    match = re.match(r"^(?P<expr>.+?)\s+(?P<order>ASC|DESC)$", cleaned, flags=re.IGNORECASE)
    if match:
        cleaned = match.group("expr").strip()
        order = f" {match.group('order').upper()}"
    if _IDENTIFIER_RE.match(cleaned):
        return f"{_validate_identifier(cleaned)}{order}"
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\([A-Za-z0-9_,\s]+\)", cleaned):
        return f"{cleaned}{order}"
    raise ValueError(f"unsupported index expression: {expression!r}")


async def _load_columns(connection: AsyncConnection, table: str, *, is_postgres: bool) -> set[str]:
    _validate_identifier(table)
    if is_postgres:
        rows = await connection.execute(
            text(
                """
                select column_name
                from information_schema.columns
                where table_schema = current_schema()
                  and table_name = :table_name
                """
            ),
            {"table_name": table},
        )
        return {str(row[0]) for row in rows}

    rows = await connection.execute(text(f'PRAGMA table_info("{table}")'))
    return {str(row[1]) for row in rows}


def _create_index_sql(spec: IndexSpec, *, is_postgres: bool) -> str:
    name = _validate_identifier(spec.name)
    table = _validate_identifier(spec.table)
    expressions = ", ".join(_format_expression(expression) for expression in spec.expressions)
    concurrently = " CONCURRENTLY" if is_postgres else ""
    return f"CREATE INDEX{concurrently} IF NOT EXISTS {name} ON {table} ({expressions})"


async def ensure_performance_indexes() -> None:
    settings = get_settings()
    is_postgres = settings.database_url.startswith("postgresql")
    engine_options: dict[str, object] = {}
    if is_postgres:
        engine_options["isolation_level"] = "AUTOCOMMIT"
    engine = create_async_engine(settings.database_url, **engine_options)
    try:
        async with engine.connect() as connection:
            created = 0
            skipped = 0
            columns_by_table: dict[str, set[str]] = {}
            for spec in INDEXES:
                columns = columns_by_table.get(spec.table)
                if columns is None:
                    columns = await _load_columns(connection, spec.table, is_postgres=is_postgres)
                    columns_by_table[spec.table] = columns
                required_columns = set(spec.required_columns or tuple(_column_name(expression) for expression in spec.expressions))
                missing = sorted(required_columns - columns)
                if missing:
                    skipped += 1
                    logger.info("skip index %s because columns are missing: %s", spec.name, ", ".join(missing))
                    continue
                await connection.execute(text(_create_index_sql(spec, is_postgres=is_postgres)))
                created += 1
                logger.info("ensured index %s", spec.name)
            if not is_postgres:
                await connection.commit()
    finally:
        await engine.dispose()
    logger.info("performance index check finished: ensured=%d skipped=%d", created, skipped)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(ensure_performance_indexes())


if __name__ == "__main__":
    main()
