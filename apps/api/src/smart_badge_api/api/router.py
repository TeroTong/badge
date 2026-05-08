from fastapi import APIRouter, Depends

from smart_badge_api.api.deps import (
    get_current_user,
    require_hospital_admin_or_above,
    require_system_admin_or_above,
)
from smart_badge_api.api.routes.audit_logs import router as audit_logs_router
from smart_badge_api.api.routes.analysis import router as analysis_router
from smart_badge_api.api.routes.account import router as account_router
from smart_badge_api.api.routes.asr_public import router as asr_public_router
from smart_badge_api.api.routes.auth import router as auth_router
from smart_badge_api.api.routes.asr_monitoring import router as asr_monitoring_router
from smart_badge_api.api.routes.customers import router as customers_router
from smart_badge_api.api.routes.positions import router as positions_router
from smart_badge_api.api.routes.sap_push_monitoring import router as sap_push_monitoring_router
from smart_badge_api.api.routes.dashboard import router as dashboard_router
from smart_badge_api.api.routes.export import router as export_router
from smart_badge_api.api.routes.health import router as health_router
from smart_badge_api.api.routes.hotwords import router as hotwords_router
from smart_badge_api.api.routes.iot import callback_router as iot_callback_router
from smart_badge_api.api.routes.iot import router as iot_router
from smart_badge_api.api.routes.organization import router as organization_router
from smart_badge_api.api.routes.preferences import router as preferences_router
from smart_badge_api.api.routes.quality import router as quality_router
from smart_badge_api.api.routes.quality_results import router as quality_results_router
from smart_badge_api.api.routes.recordings import router as recordings_router
from smart_badge_api.api.routes.risk_records import router as risk_records_router
from smart_badge_api.api.routes.risk_rules import router as risk_rules_router
from smart_badge_api.api.routes.rule_groups import router as rule_groups_router
from smart_badge_api.api.routes.sap_hana_visit_orders import router as sap_hana_visit_orders_router
from smart_badge_api.api.routes.segments import router as segments_router
from smart_badge_api.api.routes.staff import router as staff_router
from smart_badge_api.api.routes.transcripts import router as transcripts_router
from smart_badge_api.api.routes.tags import router as tags_router
from smart_badge_api.api.routes.tasks import router as tasks_router
from smart_badge_api.api.routes.templates import router as templates_router
from smart_badge_api.api.routes.visits import router as visits_router
from smart_badge_api.api.routes.visit_order_push import router as visit_order_push_router
from smart_badge_api.api.routes.visit_orders import router as visit_orders_router
from smart_badge_api.api.routes.voiceprints import router as voiceprints_router
from smart_badge_api.api.routes.wecom_menu import router as wecom_menu_router
from smart_badge_api.api.routes.wecom_sdk import router as wecom_sdk_router
from smart_badge_api.api.routes.wecom_tenants import router as wecom_tenants_router
from smart_badge_api.api.routes.dingtalk import router as dingtalk_router

api_router = APIRouter()

# ── 公开路由（无需认证）──
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(asr_public_router)
api_router.include_router(visit_order_push_router)
api_router.include_router(iot_callback_router)

# ── 所有已认证用户可访问（viewer / manager / admin）──
_any_auth = APIRouter(dependencies=[Depends(get_current_user)])
_any_auth.include_router(account_router)
_any_auth.include_router(dashboard_router)
_any_auth.include_router(analysis_router)
_any_auth.include_router(tasks_router)
_any_auth.include_router(export_router)
_any_auth.include_router(customers_router)
_any_auth.include_router(visits_router)
_any_auth.include_router(recordings_router)
_any_auth.include_router(transcripts_router)
_any_auth.include_router(segments_router)
_any_auth.include_router(quality_results_router)
_any_auth.include_router(risk_records_router)
_any_auth.include_router(sap_hana_visit_orders_router)
_any_auth.include_router(visit_orders_router)
_any_auth.include_router(wecom_sdk_router)

# ── 机构管理及以上 ──
_management = APIRouter(dependencies=[Depends(require_hospital_admin_or_above)])
_management.include_router(staff_router)
_management.include_router(organization_router)
_management.include_router(dingtalk_router)
_management.include_router(positions_router)
_management.include_router(wecom_tenants_router)

# ── 系统配置（系统管理员及以上）──
_admin = APIRouter(dependencies=[Depends(require_system_admin_or_above)])
_admin.include_router(preferences_router)
_admin.include_router(tags_router)
_admin.include_router(hotwords_router)
_admin.include_router(templates_router)
_admin.include_router(quality_router)
_admin.include_router(risk_rules_router)
_admin.include_router(rule_groups_router)
_admin.include_router(audit_logs_router)
_admin.include_router(asr_monitoring_router)
_admin.include_router(sap_push_monitoring_router)
_admin.include_router(wecom_menu_router)
_admin.include_router(voiceprints_router)
_admin.include_router(iot_router)

api_router.include_router(_any_auth)
api_router.include_router(_management)
api_router.include_router(_admin)
