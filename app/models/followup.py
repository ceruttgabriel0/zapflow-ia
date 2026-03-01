from sqlalchemy import String, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from datetime import datetime
from typing import Optional
from ..database import Base


class FollowUp(Base):
    __tablename__ = "followups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    contact_number: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    chat_id: Mapped[str] = mapped_column(String(100), nullable=False)
    session_name: Mapped[str] = mapped_column(String(100), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # 1, 2, 3, 4
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")  # pending, sent, cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<FollowUp(client_id={self.client_id}, contact={self.contact_number}, attempt={self.attempt}, status={self.status})>"
