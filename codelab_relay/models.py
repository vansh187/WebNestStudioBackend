"""ORM tables for the relay's daily quotas (section 5). Only
infra/sql_daily_counter.py queries them; app.py imports this module so
create_all builds the tables.

User ids are UUIDs here, so the all-users daily cap has its own one-row-per-day
table instead of a user_id = 0 row (which the users foreign key would refuse)."""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from database.session import Base


class RelayDailyUsage(Base):
    __tablename__ = "codelab_relay_daily_usage"
    __table_args__ = (Index("ix_codelab_relay_daily_usage_day", "day"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RelayGlobalDailyUsage(Base):
    __tablename__ = "codelab_relay_global_daily_usage"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
