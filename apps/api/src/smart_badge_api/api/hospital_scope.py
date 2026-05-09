from __future__ import annotations

from sqlalchemy import exists, or_, select
from sqlalchemy.sql.elements import ColumnElement

from smart_badge_api.db.models import Customer, Recording, RecordingVisitLink, Staff, Visit, VisitOrder


def normalize_hospital_code(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _column(model, name: str):
    columns = getattr(model, "c", model)
    return getattr(columns, name)


def visit_hospital_condition(hospital_code: str, *, visit_model=Visit) -> ColumnElement[bool]:
    visit_order = VisitOrder.__table__.alias("hospital_scope_visit_order")
    consultant_staff = Staff.__table__.alias("hospital_scope_consultant_staff")
    doctor_staff = Staff.__table__.alias("hospital_scope_doctor_staff")
    return or_(
        exists(
            select(visit_order.c.id).where(
                visit_order.c.dzdh == _column(visit_model, "external_visit_order_no"),
                visit_order.c.jgbm == hospital_code,
            )
        ),
        exists(
            select(consultant_staff.c.id).where(
                consultant_staff.c.id == _column(visit_model, "consultant_id"),
                consultant_staff.c.hospital_code == hospital_code,
            )
        ),
        exists(
            select(doctor_staff.c.id).where(
                doctor_staff.c.id == _column(visit_model, "doctor_id"),
                doctor_staff.c.hospital_code == hospital_code,
            )
        ),
    )


def customer_hospital_condition(hospital_code: str) -> ColumnElement[bool]:
    return exists(
        select(Visit.id).where(
            Visit.customer_id == Customer.id,
            visit_hospital_condition(hospital_code),
        )
    )


def recording_hospital_condition(hospital_code: str, *, recording_model=Recording) -> ColumnElement[bool]:
    direct_visit = Visit.__table__.alias("hospital_scope_direct_visit")
    linked_visit = Visit.__table__.alias("hospital_scope_linked_visit")
    recording_staff = Staff.__table__.alias("hospital_scope_recording_staff")
    return or_(
        exists(
            select(recording_staff.c.id).where(
                recording_staff.c.id == _column(recording_model, "staff_id"),
                recording_staff.c.hospital_code == hospital_code,
            )
        ),
        exists(
            select(direct_visit.c.id).where(
                direct_visit.c.id == _column(recording_model, "visit_id"),
                visit_hospital_condition(hospital_code, visit_model=direct_visit.c),
            )
        ),
        exists(
            select(RecordingVisitLink.id)
            .join(linked_visit, linked_visit.c.id == RecordingVisitLink.visit_id)
            .where(
                RecordingVisitLink.recording_id == _column(recording_model, "id"),
                visit_hospital_condition(hospital_code, visit_model=linked_visit.c),
            )
        ),
    )
