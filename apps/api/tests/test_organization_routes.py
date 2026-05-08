import asyncio

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from smart_badge_api.api.routes.organization import (
    create_management_relation,
    create_management_relations_bulk,
    create_management_relations_by_unit,
    create_organization_unit,
    get_organization_overview,
    move_organization_unit_members,
    replace_organization_unit_members,
    sync_management_relations_for_manager,
)
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import Staff, StaffManagementRelation, User, WecomTenant
from smart_badge_api.schemas.organization import (
    OrganizationUnitCreate,
    OrganizationUnitMemberMove,
    OrganizationUnitMemberUpdate,
    StaffManagementRelationByUnitCreate,
    StaffManagementRelationBulkCreate,
    StaffManagementRelationCreate,
    StaffManagementRelationSync,
)


def _make_request(path: str = "/api/v1/organization") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "client": ("127.0.0.1", 8000),
        }
    )


def test_organization_units_members_and_management_relations() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = User(
                    username="admin6101",
                    hashed_password="hashed",
                    display_name="Hospital admin",
                    role="hospital_admin",
                    hospital_code="6101",
                    hospital_name="Main hospital",
                    is_active=True,
                )
                regular_user = User(
                    username="manager-user",
                    hashed_password="hashed",
                    display_name="Manager user",
                    role="staff",
                    staff_id="manager",
                    hospital_code="6101",
                    hospital_name="Main hospital",
                    is_active=True,
                )
                system_user = User(
                    username="system-admin",
                    hashed_password="hashed",
                    display_name="System admin",
                    role="system_admin",
                    is_active=True,
                )
                super_user = User(
                    username="super-admin",
                    hashed_password="hashed",
                    display_name="Super admin",
                    role="super_admin",
                    is_active=True,
                )
                manager = Staff(
                    id="manager",
                    name="Manager",
                    hospital_code="6101",
                    hospital_short_name="Main hospital",
                    permission_role="staff",
                    is_active=True,
                )
                subordinate = Staff(
                    id="subordinate",
                    name="Advisor A",
                    hospital_code="6101",
                    hospital_short_name="Main hospital",
                    permission_role="staff",
                    is_active=True,
                )
                assistant = Staff(
                    id="assistant",
                    name="Advisor B",
                    hospital_code="6101",
                    hospital_short_name="Main hospital",
                    permission_role="staff",
                    is_active=True,
                )
                receptionist = Staff(
                    id="receptionist",
                    name="Receptionist",
                    hospital_code="6101",
                    hospital_short_name="Main hospital",
                    permission_role="staff",
                    is_active=True,
                )
                system_admin_staff = Staff(
                    id="system-admin-staff",
                    name="System Admin Staff",
                    hospital_code="6101",
                    hospital_short_name="Main hospital",
                    permission_role="system_admin",
                    is_active=True,
                )
                super_admin_staff = Staff(
                    id="super-admin-staff",
                    name="Super Admin Staff",
                    hospital_code="6101",
                    hospital_short_name="Main hospital",
                    permission_role="super_admin",
                    is_active=True,
                )
                outside = Staff(
                    id="outside",
                    name="Outside staff",
                    hospital_code="6501",
                    hospital_short_name="Other hospital",
                    permission_role="staff",
                    is_active=True,
                )
                tenant = WecomTenant(
                    name="Main hospital tenant",
                    default_hospital_code="6101",
                    default_hospital_name="Main hospital tenant",
                    is_active=True,
                )
                db.add_all([
                    admin,
                    regular_user,
                    system_user,
                    super_user,
                    manager,
                    subordinate,
                    assistant,
                    receptionist,
                    system_admin_staff,
                    super_admin_staff,
                    outside,
                    tenant,
                ])
                await db.commit()

                root = await create_organization_unit(
                    OrganizationUnitCreate(name="Consulting center", hospital_code="6101"),
                    _make_request(),
                    db=db,
                    current_user=admin,
                )
                child = await create_organization_unit(
                    OrganizationUnitCreate(name="Team A", parent_id=root.id),
                    _make_request(),
                    db=db,
                    current_user=admin,
                )
                target = await create_organization_unit(
                    OrganizationUnitCreate(name="Team B", parent_id=root.id),
                    _make_request(),
                    db=db,
                    current_user=admin,
                )

                assert child.path == "Consulting center / Team A"

                members = await replace_organization_unit_members(
                    child.id,
                    OrganizationUnitMemberUpdate(staff_ids=[manager.id, subordinate.id, assistant.id]),
                    _make_request(),
                    db=db,
                    current_user=admin,
                )
                assert {item.staff_id for item in members} == {manager.id, subordinate.id, assistant.id}

                moved_members = await move_organization_unit_members(
                    child.id,
                    OrganizationUnitMemberMove(staff_ids=[manager.id, assistant.id], target_unit_id=target.id),
                    _make_request(),
                    db=db,
                    current_user=admin,
                )
                assert {item.staff_id for item in moved_members} == {manager.id, assistant.id}

                relation = await create_management_relation(
                    StaffManagementRelationCreate(
                        manager_staff_id=manager.id,
                        subordinate_staff_id=subordinate.id,
                    ),
                    _make_request(),
                    db=db,
                    current_user=admin,
                )
                assert relation.manager_name == "Manager"
                assert relation.subordinate_name == "Advisor A"

                created_by_unit = await create_management_relations_by_unit(
                    StaffManagementRelationByUnitCreate(
                        manager_staff_id=manager.id,
                        unit_id=root.id,
                        include_descendants=True,
                    ),
                    _make_request(),
                    db=db,
                    current_user=admin,
                )
                assert {item.subordinate_staff_id for item in created_by_unit} == {assistant.id}

                created_bulk = await create_management_relations_bulk(
                    StaffManagementRelationBulkCreate(
                        manager_staff_id=manager.id,
                        subordinate_staff_ids=[subordinate.id, receptionist.id],
                    ),
                    _make_request(),
                    db=db,
                    current_user=admin,
                )
                assert {item.subordinate_staff_id for item in created_bulk} == {receptionist.id}

                synced_relations = await sync_management_relations_for_manager(
                    manager.id,
                    StaffManagementRelationSync(subordinate_staff_ids=[assistant.id, receptionist.id]),
                    _make_request(),
                    db=db,
                    current_user=admin,
                )
                assert {item.subordinate_staff_id for item in synced_relations} == {
                    manager.id,
                    assistant.id,
                    receptionist.id,
                }

                db.add(
                    StaffManagementRelation(
                        hospital_code="6101",
                        manager_staff_id=system_admin_staff.id,
                        subordinate_staff_id=receptionist.id,
                    )
                )
                db.add(
                    StaffManagementRelation(
                        hospital_code="6101",
                        manager_staff_id=super_admin_staff.id,
                        subordinate_staff_id=receptionist.id,
                    )
                )
                await db.commit()

                overview = await get_organization_overview(
                    hospital_code="6101",
                    db=db,
                    current_user=admin,
                )
                assert overview.hospital_code == "6101"
                assert {item.name for item in overview.units} == {"Consulting center", "Team A", "Team B"}
                assert {item.staff_name for item in overview.memberships} == {"Manager", "Advisor A", "Advisor B"}
                assert {item.staff_id for item in overview.memberships if item.unit_id == child.id} == {subordinate.id}
                assert {item.staff_id for item in overview.memberships if item.unit_id == target.id} == {
                    manager.id,
                    assistant.id,
                }
                member_counts = {item.name: item.member_count for item in overview.units}
                assert member_counts == {"Consulting center": 3, "Team A": 1, "Team B": 2}
                assert len(overview.management_relations) == 3
                assert {item.manager_staff_id for item in overview.management_relations} == {manager.id}

                system_overview = await get_organization_overview(
                    hospital_code="6101",
                    db=db,
                    current_user=system_user,
                )
                assert {item.manager_staff_id for item in system_overview.management_relations} == {
                    manager.id,
                    system_admin_staff.id,
                }
                assert super_admin_staff.id not in {item.manager_staff_id for item in system_overview.management_relations}

                super_overview = await get_organization_overview(
                    hospital_code="6101",
                    db=db,
                    current_user=super_user,
                )
                assert {item.manager_staff_id for item in super_overview.management_relations} == {
                    manager.id,
                    system_admin_staff.id,
                    super_admin_staff.id,
                }

                regular_overview = await get_organization_overview(
                    hospital_code="6101",
                    db=db,
                    current_user=regular_user,
                )
                assert {item.manager_staff_id for item in regular_overview.management_relations} == {manager.id}
                assert {item.subordinate_staff_id for item in regular_overview.management_relations} == {
                    manager.id,
                    assistant.id,
                    receptionist.id,
                }

                try:
                    await sync_management_relations_for_manager(
                        system_admin_staff.id,
                        StaffManagementRelationSync(subordinate_staff_ids=[receptionist.id]),
                        _make_request(),
                        db=db,
                        current_user=admin,
                    )
                except HTTPException as exc:
                    assert exc.status_code == 403
                else:
                    raise AssertionError("Hospital admins should not modify higher-level managers")

                system_synced = await sync_management_relations_for_manager(
                    system_admin_staff.id,
                    StaffManagementRelationSync(subordinate_staff_ids=[receptionist.id]),
                    _make_request(),
                    db=db,
                    current_user=system_user,
                )
                assert {item.subordinate_staff_id for item in system_synced} == {
                    system_admin_staff.id,
                    receptionist.id,
                }

                try:
                    await sync_management_relations_for_manager(
                        super_admin_staff.id,
                        StaffManagementRelationSync(subordinate_staff_ids=[receptionist.id]),
                        _make_request(),
                        db=db,
                        current_user=system_user,
                    )
                except HTTPException as exc:
                    assert exc.status_code == 403
                else:
                    raise AssertionError("System admins should not modify super admin managers")

                try:
                    await sync_management_relations_for_manager(
                        manager.id,
                        StaffManagementRelationSync(subordinate_staff_ids=[system_admin_staff.id]),
                        _make_request(),
                        db=db,
                        current_user=admin,
                    )
                except HTTPException as exc:
                    assert exc.status_code == 403
                else:
                    raise AssertionError("Hospital admins should not assign higher-level staff as targets")

                try:
                    await sync_management_relations_for_manager(
                        manager.id,
                        StaffManagementRelationSync(subordinate_staff_ids=[super_admin_staff.id]),
                        _make_request(),
                        db=db,
                        current_user=system_user,
                    )
                except HTTPException as exc:
                    assert exc.status_code == 403
                else:
                    raise AssertionError("System admins should not assign super admins as targets")

                try:
                    await sync_management_relations_for_manager(
                        assistant.id,
                        StaffManagementRelationSync(subordinate_staff_ids=[assistant.id]),
                        _make_request(),
                        db=db,
                        current_user=regular_user,
                    )
                except HTTPException as exc:
                    assert exc.status_code == 403
                else:
                    raise AssertionError("Regular staff should not modify other managers")

                regular_synced = await sync_management_relations_for_manager(
                    manager.id,
                    StaffManagementRelationSync(subordinate_staff_ids=[assistant.id]),
                    _make_request(),
                    db=db,
                    current_user=regular_user,
                )
                assert {item.subordinate_staff_id for item in regular_synced} == {manager.id, assistant.id}

                try:
                    await replace_organization_unit_members(
                        child.id,
                        OrganizationUnitMemberUpdate(staff_ids=[outside.id]),
                        _make_request(),
                        db=db,
                        current_user=admin,
                    )
                except HTTPException as exc:
                    assert exc.status_code == 400
                else:
                    raise AssertionError("Cross-hospital members should be rejected")
        finally:
            await engine.dispose()

    asyncio.run(scenario())
