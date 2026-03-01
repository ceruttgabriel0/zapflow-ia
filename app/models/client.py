from sqlalchemy import String, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column
from ..database import Base

class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    waha_session_name: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    gcal_calendar_id: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    def __repr__(self) -> str:
        return f"<Client(name={self.name}, session={self.waha_session_name})>"
