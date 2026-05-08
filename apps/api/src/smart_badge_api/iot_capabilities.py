from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.db.models import PreferenceProfile
from smart_badge_api.schemas.preferences import build_default_preference_settings


@dataclass(frozen=True, slots=True)
class IotCapabilityDefinition:
    key: str
    title: str
    group: str
    description: str
    risk_level: str = "medium"


IOT_CAPABILITY_DEFINITIONS: tuple[IotCapabilityDefinition, ...] = (
    IotCapabilityDefinition(
        key="gps_control",
        title="GPS 控制与定位请求",
        group="设备控制",
        description="允许远程开启/关闭 GPS，并请求设备上报当前定位。",
        risk_level="high",
    ),
    IotCapabilityDefinition(
        key="device_settings",
        title="设备参数设置",
        group="设备控制",
        description="允许批量修改切片、音量、GPS、自动上传等设备参数。",
        risk_level="high",
    ),
    IotCapabilityDefinition(
        key="employee_binding",
        title="员工绑定同步",
        group="设备管理",
        description="允许把系统中的员工、部门、门店绑定关系同步到 IOT 平台。",
    ),
    IotCapabilityDefinition(
        key="audio_task",
        title="云端录音切片任务",
        group="录音能力",
        description="允许按设备号与时间段在 IOT 平台创建并查询录音切片任务。",
    ),
    IotCapabilityDefinition(
        key="voice_print",
        title="IOT 声纹任务",
        group="声纹能力",
        description="允许创建、修改、查询和删除 IOT 平台声纹任务。",
        risk_level="high",
    ),
    IotCapabilityDefinition(
        key="callback_device_status",
        title="设备状态回调",
        group="平台回调",
        description="接收设备在线、离线、录音、电量、存储和心跳状态变化。",
    ),
    IotCapabilityDefinition(
        key="callback_audio",
        title="录音上传回调",
        group="平台回调",
        description="接收录音上传完成事件，并触发录音同步、归档和后续处理。",
    ),
    IotCapabilityDefinition(
        key="callback_gps",
        title="GPS 定位回调",
        group="平台回调",
        description="接收设备 GPS 点位数据，用于定位和轨迹扩展。",
        risk_level="high",
    ),
    IotCapabilityDefinition(
        key="callback_gps_status",
        title="GPS 状态回调",
        group="平台回调",
        description="接收 GPS 开启或关闭状态变化。",
    ),
    IotCapabilityDefinition(
        key="callback_device_exception",
        title="设备异常回调",
        group="平台回调",
        description="接收 SD 卡、录音文件丢失、固件升级等设备异常。",
    ),
    IotCapabilityDefinition(
        key="callback_developer",
        title="开发者配置回调",
        group="平台回调",
        description="接收录音加密、链接有效期、设备绑定等平台配置变化。",
    ),
    IotCapabilityDefinition(
        key="callback_voice_print",
        title="声纹任务回调",
        group="平台回调",
        description="接收声纹创建或更新任务的异步处理结果。",
        risk_level="high",
    ),
)

IOT_CAPABILITY_KEYS = frozenset(item.key for item in IOT_CAPABILITY_DEFINITIONS)


def default_iot_capabilities() -> dict[str, bool]:
    return {item.key: False for item in IOT_CAPABILITY_DEFINITIONS}


def normalize_iot_capabilities(raw: object) -> dict[str, bool]:
    source = raw if isinstance(raw, dict) else {}
    return {item.key: bool(source.get(item.key, False)) for item in IOT_CAPABILITY_DEFINITIONS}


def iot_capability_definitions_payload() -> list[dict[str, str]]:
    return [asdict(item) for item in IOT_CAPABILITY_DEFINITIONS]


async def ensure_default_preference_profile(db: AsyncSession) -> PreferenceProfile:
    profile = (
        await db.execute(
            select(PreferenceProfile).where(PreferenceProfile.scope_key == "default")
        )
    ).scalar_one_or_none()
    if profile is not None:
        return profile

    profile = PreferenceProfile(
        scope_key="default",
        name="Default Preference Profile",
        config=build_default_preference_settings().model_dump(mode="json"),
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return profile


def iot_capabilities_from_profile(profile: PreferenceProfile) -> dict[str, bool]:
    config = profile.config if isinstance(profile.config, dict) else {}
    return normalize_iot_capabilities(config.get("iot_capabilities"))


async def get_iot_capabilities(db: AsyncSession) -> dict[str, bool]:
    profile = await ensure_default_preference_profile(db)
    return iot_capabilities_from_profile(profile)


async def update_iot_capabilities(db: AsyncSession, values: dict[str, bool]) -> dict[str, bool]:
    profile = await ensure_default_preference_profile(db)
    current = iot_capabilities_from_profile(profile)
    for key, value in values.items():
        if key in IOT_CAPABILITY_KEYS:
            current[key] = bool(value)

    config: dict[str, Any] = dict(profile.config or {})
    config["iot_capabilities"] = current
    profile.config = config
    await db.commit()
    await db.refresh(profile)
    return iot_capabilities_from_profile(profile)


async def require_iot_capability(db: AsyncSession, key: str) -> None:
    capabilities = await get_iot_capabilities(db)
    if capabilities.get(key):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"IOT 能力未开启：{key}",
    )
