"""数据库模型 — 管理后台配置数据。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from smart_badge_api.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# ── 标签包 ──────────────────────────────────────────


class TagCategory(Base):
    __tablename__ = "tag_categories"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    group_name: Mapped[str | None] = mapped_column(String(100), nullable=True, default=None)
    weight_level: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    rule_group_id: Mapped[str | None] = mapped_column(ForeignKey("rule_groups.id", ondelete="SET NULL"), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    tags: Mapped[list[Tag]] = relationship(back_populates="category", cascade="all, delete-orphan")


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    category_id: Mapped[str] = mapped_column(ForeignKey("tag_categories.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    category: Mapped[TagCategory] = relationship(back_populates="tags")


# ── 热词 ──────────────────────────────────────────


class HotwordGroup(Base):
    __tablename__ = "hotword_groups"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    library_scope: Mapped[str] = mapped_column(String(20), default="public")
    source_label: Mapped[str] = mapped_column(String(100), default="行业")
    group_type: Mapped[str] = mapped_column(String(50), nullable=False)  # 竞品 / 顾虑 / 项目
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    words: Mapped[list[Hotword]] = relationship(back_populates="group", cascade="all, delete-orphan")


class Hotword(Base):
    __tablename__ = "hotwords"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    group_id: Mapped[str] = mapped_column(ForeignKey("hotword_groups.id", ondelete="CASCADE"))
    word: Mapped[str] = mapped_column(String(200), nullable=False)
    weight: Mapped[int] = mapped_column(Integer, default=10)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    group: Mapped[HotwordGroup] = relationship(back_populates="words")


class RuleGroup(Base):
    __tablename__ = "rule_groups"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100), default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    templates: Mapped[list[SummaryTemplate]] = relationship(back_populates="rule_group")
    quality_dimensions: Mapped[list[QualityDimension]] = relationship(back_populates="rule_group")


# ── 总结模板 ──────────────────────────────────────


class SummaryTemplate(Base):
    __tablename__ = "summary_templates"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    template_type: Mapped[str] = mapped_column(String(50), nullable=False)  # 一句话总结 / 详细摘要
    content: Mapped[str] = mapped_column(Text, nullable=False)
    rule_group_id: Mapped[str | None] = mapped_column(ForeignKey("rule_groups.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    rule_group: Mapped[RuleGroup | None] = relationship(back_populates="templates")


# ── 质检维度 ──────────────────────────────────────


class QualityDimension(Base):
    __tablename__ = "quality_dimensions"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    rule_group_id: Mapped[str | None] = mapped_column(ForeignKey("rule_groups.id", ondelete="SET NULL"), nullable=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    checkpoints: Mapped[list[QualityCheckpoint]] = relationship(
        back_populates="dimension", cascade="all, delete-orphan"
    )
    rule_group: Mapped[RuleGroup | None] = relationship(back_populates="quality_dimensions")


class QualityCheckpoint(Base):
    __tablename__ = "quality_checkpoints"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    dimension_id: Mapped[str] = mapped_column(ForeignKey("quality_dimensions.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    score_weight: Mapped[float] = mapped_column(Float, default=1.0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    dimension: Mapped[QualityDimension] = relationship(back_populates="checkpoints")


class RiskRule(Base):
    __tablename__ = "risk_rules"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    match_type: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default="medium")
    risk_label: Mapped[str] = mapped_column(String(100), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    match_config: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    records: Mapped[list["RiskRecord"]] = relationship(back_populates="rule")


# ── 人员 ──────────────────────────────────────────


class PreferenceProfile(Base):
    __tablename__ = "preference_profiles"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    scope_key: Mapped[str] = mapped_column(String(50), unique=True, default="default")
    name: Mapped[str] = mapped_column(String(100), default="Default Preference Profile")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class User(Base):
    """后台系统用户（可登录管理后台的账号）。"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), default="")
    staff_id: Mapped[str | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"), nullable=True)
    role: Mapped[str] = mapped_column(String(30), default="staff")
    hospital_code: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    hospital_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class PositionProfile(Base):
    __tablename__ = "position_profiles"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    position_type: Mapped[str] = mapped_column(String(50), default="staff")
    mapped_role: Mapped[str] = mapped_column(String(50), default="staff")
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    operator_name: Mapped[str] = mapped_column(String(100), nullable=False)
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    module_name: Mapped[str] = mapped_column(String(100), default="")
    action_name: Mapped[str] = mapped_column(String(100), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    device_code: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    staff_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    hospital_code: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    hospital_short_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="offline")
    battery_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dingtalk_team_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    dingtalk_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    dingtalk_binding_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class DeviceBatteryReminder(Base):
    __tablename__ = "device_battery_reminders"
    __table_args__ = (
        UniqueConstraint("device_code", "staff_id", name="uq_device_battery_reminders_device_staff"),
    )

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True)
    device_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    staff_id: Mapped[str | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"), nullable=True, index=True)
    wecom_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    wecom_corp_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_battery_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alert_active: Mapped[bool] = mapped_column(Boolean, default=False)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_notified_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    device: Mapped[Device | None] = relationship(foreign_keys=[device_id])
    staff: Mapped[Staff | None] = relationship(foreign_keys=[staff_id])


class WecomTenant(Base):
    __tablename__ = "wecom_tenants"
    __table_args__ = (
        UniqueConstraint("host", name="uq_wecom_tenants_host"),
    )

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    host: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    corp_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    agent_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    frontend_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    default_hospital_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    default_hospital_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sap_summary_template_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sap_summary_template_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sap_summary_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    sap_summary_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    department_assistant_match_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class Staff(Base):
    __tablename__ = "staff"
    __table_args__ = (
        UniqueConstraint("wecom_corp_id", "wecom_user_id", name="uq_staff_wecom_corp_user"),
    )

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    external_account: Mapped[str | None] = mapped_column(String(100), nullable=True)
    wecom_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    wecom_corp_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    gender: Mapped[str | None] = mapped_column(String(10), nullable=True)
    hospital_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    hospital_short_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    position_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    role: Mapped[str] = mapped_column(String(50), default="consultant")
    permission_role: Mapped[str] = mapped_column(String(30), default="staff")
    badge_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_doctor: Mapped[bool] = mapped_column(Boolean, default=False)
    is_nurse: Mapped[bool] = mapped_column(Boolean, default=False)
    is_anesthetist: Mapped[bool] = mapped_column(Boolean, default=False)
    is_cashier: Mapped[bool] = mapped_column(Boolean, default=False)
    is_guide: Mapped[bool] = mapped_column(Boolean, default=False)
    is_pre_advisor: Mapped[bool] = mapped_column(Boolean, default=False)
    is_onsite_advisor: Mapped[bool] = mapped_column(Boolean, default=False)
    is_advisor_assistant: Mapped[bool] = mapped_column(Boolean, default=False)
    is_doctor_assistant: Mapped[bool] = mapped_column(Boolean, default=False)
    is_vip_service: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class OrganizationUnit(Base):
    __tablename__ = "organization_units"
    __table_args__ = (
        UniqueConstraint("hospital_code", "parent_id", "name", name="uq_organization_units_hospital_parent_name"),
    )

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    hospital_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    hospital_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("organization_units.id", ondelete="SET NULL"), nullable=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    parent: Mapped[OrganizationUnit | None] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list[OrganizationUnit]] = relationship(back_populates="parent")
    members: Mapped[list[OrganizationUnitMember]] = relationship(back_populates="unit", cascade="all, delete-orphan")


class OrganizationUnitMember(Base):
    __tablename__ = "organization_unit_members"
    __table_args__ = (
        UniqueConstraint("unit_id", "staff_id", name="uq_organization_unit_members_unit_staff"),
    )

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    unit_id: Mapped[str] = mapped_column(ForeignKey("organization_units.id", ondelete="CASCADE"), nullable=False, index=True)
    staff_id: Mapped[str] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"), nullable=False, index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    unit: Mapped[OrganizationUnit] = relationship(back_populates="members")
    staff: Mapped[Staff] = relationship(foreign_keys=[staff_id])


class StaffManagementRelation(Base):
    __tablename__ = "staff_management_relations"
    __table_args__ = (
        UniqueConstraint("manager_staff_id", "subordinate_staff_id", name="uq_staff_management_relations_pair"),
    )

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    hospital_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    manager_staff_id: Mapped[str] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"), nullable=False, index=True)
    subordinate_staff_id: Mapped[str] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    manager: Mapped[Staff] = relationship(foreign_keys=[manager_staff_id])
    subordinate: Mapped[Staff] = relationship(foreign_keys=[subordinate_staff_id])


class DeviceStaffBinding(Base):
    __tablename__ = "device_staff_bindings"
    __table_args__ = (
        UniqueConstraint("device_id", "staff_id", "effective_from", name="uq_device_staff_bindings_device_staff_from"),
    )

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True)
    staff_id: Mapped[str] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"), nullable=False, index=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    device: Mapped[Device] = relationship(foreign_keys=[device_id])
    staff: Mapped[Staff] = relationship(foreign_keys=[staff_id])


# ── 到诊单（远程同步） ────────────────────────────────


class VisitOrder(Base):
    """从远程数据仓库同步的到诊单数据。"""

    __tablename__ = "visit_orders"
    __table_args__ = (UniqueConstraint("dzdh", "dzseg", name="uq_visit_orders_dzdh_dzseg"),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    dzdh: Mapped[str] = mapped_column(String(30), nullable=False, index=True)  # 到诊单号
    dzseg: Mapped[str | None] = mapped_column(String(9), nullable=True)  # 行项目
    sjrq: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)  # 数据日期
    jgbm: Mapped[str | None] = mapped_column(String(4), nullable=True)  # 机构编码
    fzuer: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # 美学顾问
    fzuer_long: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 美学顾问姓名
    advxc: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 现场美学顾问
    advxc_long: Mapped[str | None] = mapped_column(String(60), nullable=True)  # 现场咨询姓名
    advyq: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 院前美学顾问
    advyq_name: Mapped[str | None] = mapped_column(String(60), nullable=True)  # 院前美学顾问姓名
    kunr: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 客户号
    ninam: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 客户姓名
    kusex: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 性别代码: M=男, F=女
    kusex_txt: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 性别文本
    yydh: Mapped[str | None] = mapped_column(String(30), nullable=True)  # 预约单号
    yyuer: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 预约医生编码
    kutyp_dq: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 当前客户类型(T0): Q=潜客/新客, V=会员/老客
    kutyp_dq_txt: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 当前客户类型(T0)文本
    kut30_dq: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 当前客户类型(T30): Q=潜客/新客, V=会员/老客
    kut30_dq_txt: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 当前客户类型(T30)文本
    kusta_dq: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 当前客户类型2: Q1=建档未上门, Q2=上门未成交, Q3=体验会员, V1=付费会员
    kusta_dq_txt: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 当前客户类型2文本
    khlx: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 客户类型(T0)
    khlx_yg: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 客户类型(医管)
    khlx_t30: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 客户类型(T30)
    khlx2: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 当前客户类型2
    kulvl_dq: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 当前会员星级
    vipkf: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 客服编码
    d_fzuer: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 美学顾问编码(机构层)
    fzr_id_dq: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 美学顾问编码(当前)
    d_vipkf: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 客服编码(当前)
    fzdh: Mapped[str | None] = mapped_column(String(30), nullable=True)  # 分诊单号
    fzsj: Mapped[str | None] = mapped_column(String(18), nullable=True)  # 分诊时间
    fzrq: Mapped[str | None] = mapped_column(String(24), nullable=True)  # 分诊日期
    fzsta: Mapped[str | None] = mapped_column(String(3), nullable=True)  # 分诊状态: 1=待接诊, A=已接诊
    fzsta_txt: Mapped[str | None] = mapped_column(String(180), nullable=True)  # 分诊状态文本
    fzrid: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 分诊人编码
    ddsc: Mapped[str | None] = mapped_column(String(30), nullable=True)  # 等待时长
    bhkx: Mapped[str | None] = mapped_column(String(3), nullable=True)  # 补划扣标识
    assxc: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 美学顾问助理编码
    jzsj: Mapped[str | None] = mapped_column(String(18), nullable=True)  # 接诊时间
    jzrq: Mapped[str | None] = mapped_column(String(24), nullable=True)  # 接诊日期
    jgks: Mapped[str | None] = mapped_column(String(180), nullable=True)  # 机构科室: JGKS01=口腔科, JGKS02=皮肤科, JGKS03=外科, JGKS04=微整科, JGKS05=中医, JGKS06=纹绣, JGKS07=会籍, JGKS08=毛发移植科, JGKS09=非手术, JGKS10=私密中心, JGKS11=纤体中心, JGKS12=植发中心, JGKS13=形体私密中心, JGKS14=SPA中心
    jgks_txt: Mapped[str | None] = mapped_column(String(180), nullable=True)  # 机构科室文本
    dztyp: Mapped[str | None] = mapped_column(String(3), nullable=True)  # 到诊类型: 1=初诊, 2=复诊, 3=再咨, 4=诊疗, 5=未到院购买, Z=其他
    dztyp_txt: Mapped[str | None] = mapped_column(String(180), nullable=True)  # 到诊类型文本
    dzsta: Mapped[str | None] = mapped_column(String(3), nullable=True)  # 到诊状态: 1=未分诊, A=已确认, C=已分诊, D=已取消
    dzsta_txt: Mapped[str | None] = mapped_column(String(180), nullable=True)  # 到诊状态文本
    dzly: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 到诊来源代码: Y=已预约, N=未预约
    dymd: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 到院目的代码: A=咨询, B=治疗, C=手术, D=复查, X=未到院购买, Z=其他
    jcsta: Mapped[str | None] = mapped_column(String(3), nullable=True)  # 成交状态: N=未成交, Y=已成交, Z=已治疗
    jcsta_txt: Mapped[str | None] = mapped_column(String(180), nullable=True)  # 成交状态文本
    kusrc: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 渠道来源
    kusrc2: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 渠道来源2
    qdly1_txt: Mapped[str | None] = mapped_column(String(180), nullable=True)  # 渠道来源1
    qdly2_txt: Mapped[str | None] = mapped_column(String(180), nullable=True)  # 渠道来源2
    qd1jfl: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 渠道1级分类
    qd2jfl: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 渠道2级分类
    remark_dz: Mapped[str | None] = mapped_column(String(765), nullable=True)  # 到诊需求
    bjzx: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 不见咨询标识
    hylx_yg: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 高低非星级
    dymd_txt: Mapped[str | None] = mapped_column(String(180), nullable=True)  # 到院目的文本
    dzly_txt: Mapped[str | None] = mapped_column(String(180), nullable=True)  # 到诊来源文本
    crtdt: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 创建日期
    crttm: Mapped[str | None] = mapped_column(String(8), nullable=True)  # 创建时间
    fzr_name_dq: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 美学顾问姓名(当前)
    customer_gender: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 客户性别 (来自客户档案)
    customer_birthday: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 客户出生日期 (来自客户档案)
    jdrq: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 建档日期 (YYYYMMDD)


# ── 客户 ──────────────────────────────────────────


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    external_customer_code: Mapped[str | None] = mapped_column(String(50), unique=True, nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(10), nullable=True)  # male / female / unknown
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wechat_external_uid: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    source: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 来源渠道
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    visits: Mapped[list[Visit]] = relationship(back_populates="customer", cascade="all, delete-orphan")


# ── 到诊单 ──────────────────────────────────────────


class SapHanaVisitOrder(Base):
    """SAP HANA 推送的到诊分诊原始快照。"""

    __tablename__ = "sap_hana_visit_orders"
    __table_args__ = (UniqueConstraint("jgbm", "dzdh", name="uq_sap_hana_visit_orders_jgbm_dzdh"),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    jgbm: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    dzdh: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    yydh: Mapped[str | None] = mapped_column(String(30), nullable=True)
    crtdt: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    crttm: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dzsta: Mapped[str | None] = mapped_column(String(10), nullable=True)
    kunr: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    ninam: Mapped[str | None] = mapped_column(String(100), nullable=True)
    kusex: Mapped[str | None] = mapped_column(String(20), nullable=True)
    kulvl_dq: Mapped[str | None] = mapped_column(String(50), nullable=True)
    kutyp_dq: Mapped[str | None] = mapped_column(String(50), nullable=True)
    kut30_dq: Mapped[str | None] = mapped_column(String(50), nullable=True)
    kusta_dq: Mapped[str | None] = mapped_column(String(50), nullable=True)
    dzly: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dymd: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dztyp: Mapped[str | None] = mapped_column(String(20), nullable=True)
    remark_dz: Mapped[str | None] = mapped_column(Text, nullable=True)
    jgks: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 机构科室: JGKS01=口腔科, JGKS02=皮肤科, JGKS03=外科, JGKS04=微整科, JGKS05=中医, JGKS06=纹绣, JGKS07=会籍, JGKS08=毛发移植科, JGKS09=非手术, JGKS10=私密中心, JGKS11=纤体中心, JGKS12=植发中心, JGKS13=形体私密中心, JGKS14=SPA中心
    fzuer: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    fzuer_long: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vipkf: Mapped[str | None] = mapped_column(String(36), nullable=True)
    d_fzuer: Mapped[str | None] = mapped_column(String(36), nullable=True)
    d_vipkf: Mapped[str | None] = mapped_column(String(36), nullable=True)
    advyq: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kusrc: Mapped[str | None] = mapped_column(String(100), nullable=True)
    kusrc2: Mapped[str | None] = mapped_column(String(100), nullable=True)
    yyuer: Mapped[str | None] = mapped_column(String(36), nullable=True)
    bjzx: Mapped[str | None] = mapped_column(String(20), nullable=True)
    bhkx: Mapped[str | None] = mapped_column(String(20), nullable=True)
    fzdata: Mapped[list | None] = mapped_column(JSON, nullable=True)
    source_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    customer_birthday: Mapped[str | None] = mapped_column(String(10), nullable=True)
    customer_birthday_lookup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    customer_birthday_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    customer_birthday_retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class Visit(Base):
    """到诊单 — 一次客户到院服务记录。"""

    __tablename__ = "visits"
    __table_args__ = (
        UniqueConstraint("external_visit_order_no", "external_visit_order_seg", name="uq_visits_visit_order_ref"),
    )

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    external_visit_order_no: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    external_visit_order_seg: Mapped[str | None] = mapped_column(String(9), nullable=True)
    consultant_id: Mapped[str | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"), nullable=True)
    doctor_id: Mapped[str | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="created")
    # created → assigned → consulting → consulted → needs_diagnosis → diagnosing → diagnosed → closed_won / closed_lost
    visit_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    deposit_principal: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    deposit_bonus: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    deal_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    visit_time: Mapped[str | None] = mapped_column(String(8), nullable=True)
    arrival_purpose: Mapped[str | None] = mapped_column(String(180), nullable=True)
    project_needs: Mapped[str | None] = mapped_column(String(200), nullable=True)
    customer_value: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    customer: Mapped[Customer] = relationship(back_populates="visits")
    consultant: Mapped[Staff | None] = relationship(foreign_keys=[consultant_id])
    doctor: Mapped[Staff | None] = relationship(foreign_keys=[doctor_id])

    recordings: Mapped[list[Recording]] = relationship(back_populates="visit")
    recording_links: Mapped[list[RecordingVisitLink]] = relationship(back_populates="visit", cascade="all, delete-orphan")


class RecordingVisitLink(Base):
    """录音与接诊记录的关联表，支持一主多辅和一对多录音。"""

    __tablename__ = "recording_visit_links"
    __table_args__ = (
        UniqueConstraint("recording_id", "visit_id", name="uq_recording_visit_links_recording_visit"),
    )

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"), nullable=False, index=True)
    visit_id: Mapped[str] = mapped_column(ForeignKey("visits.id", ondelete="CASCADE"), nullable=False, index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    recording: Mapped[Recording] = relationship(back_populates="visit_links")
    visit: Mapped[Visit] = relationship(back_populates="recording_links")


# ── 录音 ──────────────────────────────────────────


class Recording(Base):
    """录音记录 — 来自工牌设备的原始音频文件。"""

    __tablename__ = "recordings"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    visit_id: Mapped[str | None] = mapped_column(ForeignKey("visits.id", ondelete="SET NULL"), nullable=True)
    staff_id: Mapped[str | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)  # bytes
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="uploaded")
    # uploaded → transcribing → transcribed → analyzing → analyzed / failed
    split_parent_recording_id: Mapped[str | None] = mapped_column(
        ForeignKey("recordings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    split_part_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    split_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_segments: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # ASR 分段结果
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    visit: Mapped[Visit | None] = relationship(back_populates="recordings")
    staff: Mapped[Staff | None] = relationship(foreign_keys=[staff_id])
    visit_links: Mapped[list[RecordingVisitLink]] = relationship(back_populates="recording", cascade="all, delete-orphan")
    customer_segments: Mapped[list[RecordingCustomerSegment]] = relationship(back_populates="recording", cascade="all, delete-orphan")
    visit_analyses: Mapped[list[RecordingVisitAnalysis]] = relationship(back_populates="recording", cascade="all, delete-orphan")

    transcript: Mapped[Transcript | None] = relationship(back_populates="recording", cascade="all, delete-orphan", uselist=False)
    segments: Mapped[list[Segment]] = relationship(back_populates="recording", cascade="all, delete-orphan")
    split_parent: Mapped[Recording | None] = relationship(remote_side=[id])


# ── 转写 ──────────────────────────────────────────


class Transcript(Base):
    """ASR 转写结果 — 一段录音对应一份转写。"""

    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"), unique=True)
    asr_provider: Mapped[str] = mapped_column(String(50), default="mock")  # mock / aliyun / tencent / etc.
    asr_task_id: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 外部 ASR 任务 ID
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending → processing → completed → failed
    full_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    utterances: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{speaker, text, begin_ms, end_ms}, ...]
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    recording: Mapped[Recording] = relationship(back_populates="transcript")


# ── 对话片段 ──────────────────────────────────────


class Segment(Base):
    """对话片段 — 从一段录音转写中拆分出的单次客户对话。"""

    __tablename__ = "segments"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"))
    visit_id: Mapped[str | None] = mapped_column(ForeignKey("visits.id", ondelete="SET NULL"), nullable=True)
    segment_index: Mapped[int] = mapped_column(Integer, default=0)  # 片段在录音中的顺序
    begin_ms: Mapped[int] = mapped_column(Integer, default=0)
    end_ms: Mapped[int] = mapped_column(Integer, default=0)
    speaker_label: Mapped[str | None] = mapped_column(String(50), nullable=True)  # consultant / doctor / customer / unknown
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    utterances: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 片段内的逐句列表
    status: Mapped[str] = mapped_column(String(20), default="created")
    # created → analyzing → analyzed / failed
    analysis_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    recording: Mapped[Recording] = relationship(back_populates="segments")
    visit: Mapped[Visit | None] = relationship(foreign_keys=[visit_id])


# ── 分析任务 ──────────────────────────────────────


class AnalysisTask(Base):
    """录音分析任务 — 记录上传、分析状态和结果。"""

    __tablename__ = "analysis_tasks"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / running / done / failed
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    segment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RecordingCustomerSegment(Base):
    """一条录音中候选的客户段，用于多客户录音人工确认。"""

    __tablename__ = "recording_customer_segments"
    __table_args__ = (
        UniqueConstraint("recording_id", "segment_index", name="uq_recording_customer_segments_recording_index"),
    )

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"), nullable=False, index=True)
    segment_index: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    label: Mapped[str] = mapped_column(String(50), default="客户1", nullable=False)
    begin_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    utterance_indexes: Mapped[list] = mapped_column(JSON, default=list)
    utterance_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="detected", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    recording: Mapped[Recording] = relationship(back_populates="customer_segments")
    visit_analyses: Mapped[list[RecordingVisitAnalysis]] = relationship(back_populates="customer_segment")


class RecordingVisitAnalysis(Base):
    """录音与到诊单维度的独立分析结果。"""

    __tablename__ = "recording_visit_analysis_results"
    __table_args__ = (
        UniqueConstraint("recording_id", "visit_id", name="uq_recording_visit_analysis_recording_visit"),
    )

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"), nullable=False, index=True)
    visit_id: Mapped[str] = mapped_column(ForeignKey("visits.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_segment_id: Mapped[str | None] = mapped_column(
        ForeignKey("recording_customer_segments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    mapping_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    analysis_status: Mapped[str] = mapped_column(String(20), default="idle", nullable=False)
    analysis_task_id: Mapped[str | None] = mapped_column(ForeignKey("analysis_tasks.id", ondelete="SET NULL"), nullable=True)
    analysis_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    analysis_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sap_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sap_push_log_id: Mapped[str | None] = mapped_column(ForeignKey("sap_push_logs.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    recording: Mapped[Recording] = relationship(back_populates="visit_analyses")
    visit: Mapped[Visit] = relationship()
    customer_segment: Mapped[RecordingCustomerSegment | None] = relationship(back_populates="visit_analyses")
    analysis_task: Mapped[AnalysisTask | None] = relationship()
    sap_push_log: Mapped[SapPushLog | None] = relationship()


class SapPushLog(Base):
    """SAP 咨询单回传日志。"""

    __tablename__ = "sap_push_logs"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    recording_id: Mapped[str | None] = mapped_column(ForeignKey("recordings.id", ondelete="SET NULL"), nullable=True)
    visit_id: Mapped[str | None] = mapped_column(ForeignKey("visits.id", ondelete="SET NULL"), nullable=True)
    visit_order_no: Mapped[str | None] = mapped_column(String(50), nullable=True)
    visit_order_seg: Mapped[str | None] = mapped_column(String(20), nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    customer_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    advisor_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    trigger_mode: Mapped[str] = mapped_column(String(20), default="manual")
    status: Mapped[str] = mapped_column(String(20), default="prepared")
    send_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    initiated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    request_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_payloads: Mapped[list | None] = mapped_column(JSON, nullable=True)
    gateway_requests: Mapped[list | None] = mapped_column(JSON, nullable=True)
    response_items: Mapped[list | None] = mapped_column(JSON, nullable=True)
    http_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    business_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    business_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message_success_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message_failure_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message_notify_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    recording: Mapped[Recording | None] = relationship()
    visit: Mapped[Visit | None] = relationship()


class RiskRecord(Base):
    __tablename__ = "risk_records"
    __table_args__ = (UniqueConstraint("rule_id", "task_id", name="uq_risk_records_rule_task"),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_id)
    rule_id: Mapped[str | None] = mapped_column(ForeignKey("risk_rules.id", ondelete="SET NULL"), nullable=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("analysis_tasks.id", ondelete="CASCADE"))
    recording_id: Mapped[str | None] = mapped_column(ForeignKey("recordings.id", ondelete="SET NULL"), nullable=True)
    visit_id: Mapped[str | None] = mapped_column(ForeignKey("visits.id", ondelete="SET NULL"), nullable=True)
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True)
    staff_id: Mapped[str | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"), nullable=True)
    source_type: Mapped[str] = mapped_column(String(20), default="recording")
    rule_name: Mapped[str] = mapped_column(String(100), default="")
    risk_label: Mapped[str] = mapped_column(String(100), default="")
    severity: Mapped[str] = mapped_column(String(20), default="medium")
    status: Mapped[str] = mapped_column(String(20), default="open")
    matched_dimension_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    matched_keywords: Mapped[list] = mapped_column(JSON, default=list)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    hit_excerpt: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    rule: Mapped[RiskRule | None] = relationship(back_populates="records")
    task: Mapped[AnalysisTask] = relationship()
    recording: Mapped[Recording | None] = relationship()
    visit: Mapped[Visit | None] = relationship()
    customer: Mapped[Customer | None] = relationship()
    staff: Mapped[Staff | None] = relationship()
