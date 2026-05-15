from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


API_ROOT = Path(__file__).resolve().parents[3]
PROJECT_ROOT = API_ROOT.parent.parent


class Settings(BaseSettings):
    app_name: str = "朗姿智能工牌 API"
    app_env: str = "local"
    api_v1_prefix: str = "/api/v1"
    frontend_url: str = "http://127.0.0.1:5173"
    wecom_corp_id: str = ""
    wecom_agent_id: str = ""
    wecom_agent_secret: str = ""
    wecom_callback_token: str = ""
    wecom_callback_aes_key: str = ""
    wecom_api_base_url: str = "https://qyapi.weixin.qq.com"
    wecom_oauth_base_url: str = "https://open.weixin.qq.com"
    wecom_default_redirect_path: str = "/wecom/badge"

    dingtalk_corp_id: str = ""
    dingtalk_app_key: str = ""
    dingtalk_app_secret: str = ""
    dingtalk_api_base_url: str = "https://api.dingtalk.com"
    dingtalk_iot_enabled: bool = True
    dingtalk_iot_app_id: str = ""
    dingtalk_iot_app_secret: str = ""
    dingtalk_iot_base_url: str = "https://ecard-api-141770.eapps.dingtalkcloud.com/iot-cloud-open/v2"
    dingtalk_iot_hospital_codes: str = "6501"
    dingtalk_iot_timeout_seconds: float = 30.0
    dingtalk_iot_audio_default_lookback_days: int = 7
    dingtalk_audio_sync_enabled: bool = False
    dingtalk_audio_sync_interval_seconds: int = 180
    dingtalk_audio_sync_lookback_minutes: int = 240
    dingtalk_audio_sync_page_size: int = 20
    dingtalk_audio_archive_sync_enabled: bool = False
    dingtalk_audio_archive_sync_interval_seconds: int = 180
    dingtalk_audio_archive_sync_lookback_minutes: int = 60
    dingtalk_audio_archive_sync_workers: int = 4
    dingtalk_audio_archive_backfill_enabled: bool = False
    dingtalk_audio_archive_backfill_interval_hours: int = 24
    dingtalk_audio_archive_backfill_days: int = 3
    dingtalk_audio_backlog_sync_enabled: bool = False
    dingtalk_audio_backlog_sync_interval_seconds: int = 900
    dingtalk_audio_backlog_sync_workers: int = 2
    dingtalk_audio_backlog_retry_failed_enabled: bool = True
    dingtalk_audio_backlog_sync_limit_per_run: int = 0
    archive_recording_index_refresh_interval_seconds: int = 45
    dingtalk_audio_pipeline_workers: int = 2
    dingtalk_audio_stale_processing_timeout_seconds: int = 900
    dingtalk_audio_stage_dir: str = "dingtalk_staging"
    dingtalk_audio_min_duration_seconds: int = 60
    dingtalk_audio_max_duration_seconds: int = 18000
    dingtalk_audio_min_utterance_count: int = 4
    dingtalk_audio_min_transcript_chars: int = 40
    dingtalk_audio_require_multi_speaker: bool = True
    dingtalk_audio_require_customer_role: bool = True
    dingtalk_audio_internal_keyword_threshold: int = 2
    dingtalk_audio_auto_analyze: bool = True

    database_url: str = f"sqlite+aiosqlite:///{(API_ROOT / 'smart_badge.db').as_posix()}"
    database_echo: bool = False
    database_pool_size: int = 20
    database_max_overflow: int = 40
    database_pool_timeout_seconds: float = 30.0
    database_pool_recycle_seconds: int = 1800
    database_pool_pre_ping: bool = True
    hot_read_cache_enabled: bool = True
    hot_read_cache_ttl_seconds: float = 5.0
    hot_read_cache_badge_ttl_seconds: float = 2.0
    hot_read_cache_max_items: int = 512
    hot_read_cache_max_body_bytes: int = 2_000_000
    auth_user_cache_ttl_seconds: float = 15.0
    auth_user_cache_max_items: int = 2048
    redis_url: str = "redis://127.0.0.1:6379/0"
    staff_directory_dsn: str = ""
    pg_host: str = ""
    pg_port: int = 5432
    pg_db: str = "datahub"
    pg_user: str = ""
    pg_password: str = ""
    staff_refresh_interval_seconds: int = 86400

    storage_endpoint: str = "http://127.0.0.1:9000"
    storage_access_key: str = "minioadmin"
    storage_secret_key: str = "minioadmin"
    storage_bucket: str = "smart-badge"

    upload_dir: str = "uploads"
    results_dir: str = "results"
    batch_import_allowed_dirs: str = ""
    sap_hana_push_api_key: str = ""
    visit_order_auto_sync_enabled: bool = False
    visit_order_auto_sync_interval_seconds: int = 300
    visit_order_auto_sync_lookback_days: int = 7

    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-5.2-chat-latest"
    llm_timeout_seconds: float = 120.0
    task_dispatch_mode: Literal["dramatiq", "background", "eager"] = "dramatiq"

    sap_rfc_app_id: str = "ai"
    sap_rfc_secret: str = ""
    sap_rfc_gateway_url: str = "http://123.56.3.143/prod-api/ext/api/v1/rfc/sap"
    sap_rfc_send_enabled: bool = False
    sap_rfc_dispatch_mode: Literal["dramatiq", "background", "eager"] = "background"
    sap_rfc_timeout_seconds: float = 20.0
    sap_rfc_auto_push_on_bind: bool = False
    sap_rfc_auto_push_interval_seconds: int = 60
    sap_rfc_auto_push_stable_seconds: int = 300
    sap_rfc_auto_push_retry_delay_seconds: int = 1800
    sap_rfc_auto_push_stale_seconds: int = 1800
    sap_rfc_auto_push_limit_per_run: int = 20
    sap_rfc_auto_push_ignore_before: str = ""
    sap_rfc_summary_disabled_hospital_codes: str = ""
    sap_rfc_use_yybm_as_jgbm: bool = True
    sap_rfc_mode: str = "C"
    sap_rfc_override_kunr: str = ""
    sap_rfc_override_zxdh: str = ""
    sap_rfc_override_user: str = ""
    sap_rfc_override_advxc: str = ""

    asr_provider: Literal[
        "mock",
        "whisper",
        "sensevoice_3dspeaker",
        "high_precision_3dspeaker",
        "tencent_asr",
        "xfyun_asr",
    ] = "mock"
    asr_dispatch_mode: Literal["dramatiq", "background", "eager"] = "dramatiq"
    asr_runtime_dir: str = "asr_runtime"
    asr_audio_quality_diagnostics_enabled: bool = True
    asr_audio_quality_log_path: str = ""
    asr_low_volume_gain_enabled: bool = True
    asr_low_volume_mean_db_threshold: float = -32.0
    asr_low_volume_target_mean_db: float = -26.0
    asr_low_volume_max_gain_db: float = 8.0
    asr_low_volume_min_gain_db: float = 2.0
    asr_low_volume_headroom_db: float = 1.0
    asr_low_volume_output_bitrate_kbps: int = 40
    asr_independent_diarization_enabled: bool = False
    asr_independent_diarization_providers: str = "tencent_asr,xfyun_asr"
    asr_independent_diarization_min_speakers: int = 2
    asr_independent_diarization_max_speakers: int = 5
    asr_independent_diarization_split_mixed_utterances: bool = True
    asr_independent_diarization_min_split_duration_ms: int = 2000
    asr_independent_diarization_min_speaker_overlap_ms: int = 500
    tencent_asr_secret_id: str = ""
    tencent_asr_secret_key: str = ""
    tencent_asr_session_token: str = ""
    tencent_asr_region: str = "ap-shanghai"
    tencent_asr_endpoint: str = "asr.tencentcloudapi.com"
    tencent_asr_engine_model_type: str = "16k_zh"
    tencent_asr_channel_num: int = 1
    tencent_asr_res_text_format: int = 2
    tencent_asr_speaker_diarization: int = 0
    tencent_asr_speaker_number: int = 0
    tencent_asr_poll_interval_seconds: int = 5
    tencent_asr_timeout_seconds: int = 3600
    tencent_asr_public_media_ttl_seconds: int = 14400
    tencent_asr_public_media_base_url: str = ""
    tencent_asr_max_concurrency: int = 8
    tencent_asr_hotword_list: str = ""
    tencent_asr_hotword_vocab_sync_enabled: bool = False
    tencent_asr_hotword_vocab_id: str = ""
    tencent_asr_hotword_vocab_name: str = "smart-badge-hotwords"
    tencent_asr_hotword_vocab_description: str = "Smart Badge ASR hotwords"
    tencent_asr_dynamic_hotwords_enabled: bool = True
    tencent_asr_replace_text_id: str = ""
    tencent_asr_local_diarization_enabled: bool = True
    tencent_asr_url_upload_enabled: bool = True
    tencent_asr_direct_upload_max_bytes: int = 5_000_000
    tencent_asr_direct_upload_segment_seconds: int = 1200
    tencent_asr_direct_upload_bitrate_kbps: int = 40
    tencent_asr_silence_split_enabled: bool = True
    tencent_asr_silence_split_window_seconds: int = 45
    tencent_asr_silence_split_noise_db: int = -35
    tencent_asr_silence_split_min_duration_seconds: float = 0.5
    tencent_asr_request_audit_log_path: str = ""
    tencent_asr_cloud_audit_log_path: str = ""
    tencent_asr_task_registry_path: str = ""
    xfyun_asr_app_id: str = ""
    xfyun_asr_access_key_id: str = ""
    xfyun_asr_access_key_secret: str = ""
    xfyun_asr_base_url: str = "https://office-api-ist-dx.iflyaisol.com"
    xfyun_asr_language: str = "autodialect"
    xfyun_asr_domain: str = ""
    xfyun_asr_role_type: int = 0
    xfyun_asr_role_num: int = 0
    xfyun_asr_duration_check_disable: bool = True
    xfyun_asr_eng_smoothproc: bool = True
    xfyun_asr_eng_colloqproc: bool = False
    xfyun_asr_request_timeout_seconds: float = 300.0
    xfyun_asr_poll_interval_seconds: int = 5
    xfyun_asr_timeout_seconds: int = 3600
    xfyun_asr_max_concurrency: int = 4
    asr_medical_term_normalization_enabled: bool = True
    asr_hotword_auto_sync_enabled: bool = False
    asr_hotword_auto_sync_interval_seconds: int = 86400
    asr_hotword_sync_timeout_seconds: int = 1800
    whisper_model_size: str = "large-v3"
    whisper_device: str = "auto"
    whisper_compute_type: str = "auto"
    whisper_cache_dir: str = ""
    whisper_local_files_only: bool = True
    whisper_beam_size: int = 8
    whisper_best_of: int = 8
    whisper_patience: float = 1.2
    whisper_length_penalty: float = 1.0
    whisper_repetition_penalty: float = 1.02
    whisper_word_timestamps: bool = True
    whisper_hotwords_enabled: bool = False
    whisper_hotword_file: str = ""
    whisper_vad_min_silence_duration_ms: int = 300
    sensevoice_model_id: str = "iic/SenseVoiceSmall"
    sensevoice_language: str = "zh"
    sensevoice_use_itn: bool = True
    sensevoice_batch_size_s: int = 60
    sensevoice_merge_length_s: int = 15
    sensevoice_enable_vad: bool = True
    sensevoice_vad_model: str = "fsmn-vad"
    sensevoice_vad_max_single_segment_time_ms: int = 30000
    sensevoice_device: str = "auto"
    sensevoice_diarization_first_enabled: bool = False
    sensevoice_speaker_window_seconds: float = 40.0
    sensevoice_speaker_merge_gap_seconds: float = 0.45
    sensevoice_speaker_padding_ms: int = 120
    sensevoice_role_classification_enabled: bool = True
    sensevoice_utterance_gap_seconds: float = 0.7
    sensevoice_punctuation_pause_seconds: float = 0.35
    threed_speaker_repo_path: str = ""
    threed_speaker_model_cache_dir: str = ""
    threed_speaker_device: str = "auto"
    threed_speaker_include_overlap: bool = False
    speaker_verification_model_id: str = "iic/speech_eres2netv2_sv_zh-cn_16k-common"
    speaker_voiceprint_enabled: bool = True
    speaker_voiceprint_auto_enroll_enabled: bool = True
    speaker_voiceprint_auto_enroll_threshold: float = 0.82
    speaker_voiceprint_registry_path: str = ""
    speaker_voiceprint_review_queue_path: str = ""
    speaker_voiceprint_min_duration_ms: int = 12000
    speaker_voiceprint_match_threshold: float = 0.68
    speaker_voiceprint_match_margin: float = 0.05
    device_heartbeat_timeout_seconds: int = 900
    device_low_battery_alert_enabled: bool = True
    device_low_battery_threshold: int = 30
    device_low_battery_recovery_threshold: int = 30
    iot_callback_api_key: str = ""
    message_push_base_url: str = ""
    message_push_timeout_seconds: float = 10.0
    message_push_auth_codes: str = ""
    message_push_low_battery_biz_user_id: str = "smart_badge_low_battery"
    message_push_sap_result_enabled: bool = True
    message_push_sap_result_biz_user_id: str = "smart_badge_sap_push"

    hf_token: str = ""

    secret_key: str = "CHANGE-ME-IN-PRODUCTION"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    model_config = SettingsConfigDict(
        env_file=str(API_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def resolve_path(self, value: str | Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = API_ROOT / path
        return path.resolve()

    @property
    def upload_path(self) -> Path:
        return self.resolve_path(self.upload_dir)

    @property
    def results_path(self) -> Path:
        return self.resolve_path(self.results_dir)

    @property
    def resolved_batch_import_allowed_paths(self) -> list[Path]:
        return [
            self.resolve_path(item.strip())
            for item in self.batch_import_allowed_dirs.replace(";", ",").split(",")
            if item.strip()
        ]

    @property
    def asr_runtime_path(self) -> Path:
        runtime_dir = Path(self.asr_runtime_dir)
        if runtime_dir.is_absolute():
            return runtime_dir.resolve()
        return (self.upload_path / runtime_dir).resolve()

    @property
    def dingtalk_audio_stage_path(self) -> Path:
        stage_dir = Path(self.dingtalk_audio_stage_dir)
        if stage_dir.is_absolute():
            return stage_dir.resolve()
        return (self.upload_path / stage_dir).resolve()

    @property
    def resolved_threed_speaker_repo_path(self) -> Path:
        explicit = self.threed_speaker_repo_path.strip()
        if explicit:
            return self.resolve_path(explicit)
        return (self.asr_runtime_path / "3D-Speaker").resolve()

    @property
    def resolved_threed_speaker_model_cache_path(self) -> Path:
        explicit = self.threed_speaker_model_cache_dir.strip()
        if explicit:
            return self.resolve_path(explicit)
        return (self.asr_runtime_path / "modelscope").resolve()

    @property
    def resolved_whisper_cache_path(self) -> Path:
        explicit = self.whisper_cache_dir.strip()
        if explicit:
            return self.resolve_path(explicit)
        return (self.asr_runtime_path / "whisper").resolve()

    @property
    def resolved_speaker_voiceprint_registry_path(self) -> Path:
        explicit = self.speaker_voiceprint_registry_path.strip()
        if explicit:
            return self.resolve_path(explicit)
        return (self.asr_runtime_path / "voiceprints" / "staff_registry.json").resolve()

    @property
    def resolved_speaker_voiceprint_review_queue_path(self) -> Path:
        explicit = self.speaker_voiceprint_review_queue_path.strip()
        if explicit:
            return self.resolve_path(explicit)
        return (self.asr_runtime_path / "voiceprints" / "review_queue.json").resolve()

    @property
    def resolved_tencent_asr_request_audit_log_path(self) -> Path:
        explicit = self.tencent_asr_request_audit_log_path.strip()
        if explicit:
            return self.resolve_path(explicit)
        return (self.asr_runtime_path / "tencent_asr_requests.jsonl").resolve()

    @property
    def resolved_asr_audio_quality_log_path(self) -> Path:
        explicit = self.asr_audio_quality_log_path.strip()
        if explicit:
            return self.resolve_path(explicit)
        return (self.asr_runtime_path / "audio_quality_diagnostics.jsonl").resolve()

    @property
    def resolved_tencent_asr_cloud_audit_log_path(self) -> Path:
        explicit = self.tencent_asr_cloud_audit_log_path.strip()
        if explicit:
            return self.resolve_path(explicit)
        return (PROJECT_ROOT / "log.csv").resolve()

    @property
    def resolved_tencent_asr_task_registry_path(self) -> Path:
        explicit = self.tencent_asr_task_registry_path.strip()
        if explicit:
            return self.resolve_path(explicit)
        return (self.asr_runtime_path / "tencent_asr_task_registry.json").resolve()

    @property
    def resolved_staff_directory_dsn(self) -> str:
        explicit = self.staff_directory_dsn.strip()
        if explicit:
            return explicit
        if self.pg_host and self.pg_db and self.pg_user:
            user = quote_plus(self.pg_user)
            credentials = user
            if self.pg_password:
                credentials = f"{user}:{quote_plus(self.pg_password)}"
            return f"postgresql://{credentials}@{self.pg_host}:{self.pg_port}/{self.pg_db}"
        return ""

    def resolve_file_path(self, stored: str) -> Path:
        p = Path(stored)
        if p.is_absolute():
            return p
        return self.upload_path / p

    def make_relative_path(self, absolute: Path) -> str:
        try:
            return str(absolute.relative_to(self.upload_path))
        except ValueError:
            return str(absolute)


    @property
    def dingtalk_enabled(self) -> bool:
        return bool(
            self.dingtalk_corp_id.strip()
            and self.dingtalk_app_key.strip()
            and self.dingtalk_app_secret.strip()
        )

    @property
    def wecom_enabled(self) -> bool:
        return bool(
            self.wecom_corp_id.strip()
            and self.wecom_agent_id.strip()
            and self.wecom_agent_secret.strip()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
