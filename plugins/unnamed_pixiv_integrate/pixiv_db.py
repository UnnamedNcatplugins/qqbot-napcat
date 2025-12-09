import sqlmodel
from .pixiv_sqlmodel import *


class PixivDB:
    def __init__(self, db_url: str):
        self.engine = sqlmodel.create_engine(db_url)
        sqlmodel.SQLModel.metadata.create_all(self.engine)
        self.session = sqlmodel.Session(self.engine)
        # 启用外键约束
        self.session.connection().execute(sqlmodel.text("PRAGMA foreign_keys=ON"))

    def insert_daily_illust_source_rows(self, rows: list[DailyIllustSource]):
        for row in rows:
            self.session.add(row)
        self.session.commit()

    def shutdown(self):
        self.session.close()
