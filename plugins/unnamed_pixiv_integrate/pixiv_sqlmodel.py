import sqlmodel
from sqlalchemy import func
from datetime import datetime


class DailyIllustSource(sqlmodel.SQLModel, table=True):
    work_id: int | None = sqlmodel.Field(default=None, primary_key=True)
    last_post: datetime | None = sqlmodel.Field(default=None, sa_column=sqlmodel.Column(sqlmodel.DateTime(timezone=True,)))
    user_id: int | None = sqlmodel.Field(nullable=False)
