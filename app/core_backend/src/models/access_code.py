from pydantic import BaseModel, Field


class AccessCodeRequest(BaseModel):
    # Field is optional at the schema level so that ANY failure (missing,
    # empty, wrong) collapses to a single 403 response from the route, rather
    # than splitting into 422 (validation) and 403 (invalid).
    accessCode: str = Field(default="")


class AccessCodeResponse(BaseModel):
    valid: bool
