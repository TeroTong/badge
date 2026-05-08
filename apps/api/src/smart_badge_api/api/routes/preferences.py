from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.db.models import PreferenceProfile
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.preferences import (
    PreferenceProfileOut,
    PreferenceProfileUpdate,
    build_default_preference_settings,
    normalize_preference_settings,
)

router = APIRouter(prefix="/preferences", tags=["偏好设置"])


def _to_out(profile: PreferenceProfile) -> PreferenceProfileOut:
    return PreferenceProfileOut(
        id=profile.id,
        scope_key=profile.scope_key,
        name=profile.name,
        settings=normalize_preference_settings(profile.config),
        created_at=profile.created_at.isoformat() if profile.created_at else "",
        updated_at=profile.updated_at.isoformat() if profile.updated_at else "",
    )


async def _ensure_default_profile(db: AsyncSession) -> PreferenceProfile:
    profile = (
        await db.execute(
            select(PreferenceProfile).where(PreferenceProfile.scope_key == "default")
        )
    ).scalar_one_or_none()
    if profile:
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


@router.get("/profile", response_model=PreferenceProfileOut)
async def get_preference_profile(db: AsyncSession = Depends(get_db)):
    profile = await _ensure_default_profile(db)
    return _to_out(profile)


@router.put("/profile", response_model=PreferenceProfileOut)
async def update_preference_profile(body: PreferenceProfileUpdate, db: AsyncSession = Depends(get_db)):
    profile = await _ensure_default_profile(db)
    next_settings = normalize_preference_settings(body.settings).model_dump(mode="json")
    existing_config = profile.config if isinstance(profile.config, dict) else {}
    if not next_settings.get("iot_capabilities") and isinstance(existing_config.get("iot_capabilities"), dict):
        next_settings["iot_capabilities"] = existing_config["iot_capabilities"]
    profile.config = next_settings
    await db.commit()
    await db.refresh(profile)
    return _to_out(profile)
