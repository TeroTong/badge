from pydantic import BaseModel


class AuditLogOut(BaseModel):
    id: str
    operator_name: str
    ip_address: str
    module_name: str
    action_name: str
    content: str
    created_at: str

    model_config = {"from_attributes": True}
