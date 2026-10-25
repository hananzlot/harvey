"""Contact data model — a person at a company."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


def _utcnow() -> datetime:
    """Naive UTC now (consistent with DB storage; avoids deprecated utcnow)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Prospect(BaseModel):
    id: str = ""
    company_id: str = ""       # FK to companies table
    first_name: str = ""       # required for a valid contact
    last_name: str = ""        # required for a valid contact
    email: str = ""
    email_verified: bool = False
    # verified / risky (catch-all) / guess (never auto-sent) / invalid / ""
    email_status: str = ""
    phone: str = ""
    phone_verified: bool = False
    linkedin_url: str = ""
    title: str = ""            # required for a valid contact
    seniority: str = ""        # c_suite, vp, director, manager, individual
    department: str = ""
    source: str = ""           # how we found them
    source_url: str = ""       # where we found the info
    status: str = "new"        # new/contacted/replied/meeting/closed/lost
    score: int = 0
    personalization_notes: str = ""
    # legacy fields kept for backwards compat with existing code
    company: str = ""
    industry: str = ""
    company_size: str = ""
    # 'person' = a named human; 'business' = a published business address
    # (office@, info@, an owner's gmail) with no named person behind it.
    contact_kind: str = "person"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        """Emails are dedup keys — normalize so 'Jane@X.com' == 'jane@x.com'."""
        return (v or "").strip().lower()

    @field_validator("linkedin_url")
    @classmethod
    def _strip_linkedin(cls, v: str) -> str:
        return (v or "").strip()

    def is_business(self) -> bool:
        return self.contact_kind == "business"

    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def display_name(self) -> str:
        """A readable label even when no person is named."""
        name = self.full_name()
        if name:
            return name
        return f"{self.title or 'Owner'} of {self.company}" if self.company else (self.title or "Owner")

    def greeting_name(self) -> str:
        """What {{first_name}} renders as: the first name, or "there"."""
        return self.first_name.strip() or "there"

    def is_valid(self) -> bool:
        """A person needs a name and title. A business contact (a published
        business address) needs no personal name, but must have a title, a
        company and an email."""
        if self.is_business():
            return bool(self.title and (self.company or self.company_id) and self.email)
        return bool(self.first_name and self.last_name and self.title)
