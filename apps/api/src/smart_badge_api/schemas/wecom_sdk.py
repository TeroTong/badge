from pydantic import BaseModel


class WecomJsSdkConfigOut(BaseModel):
    corp_id: str
    agent_id: str | None = None
    timestamp: int
    nonceStr: str
    signature: str
