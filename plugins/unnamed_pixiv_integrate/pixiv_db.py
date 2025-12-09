import sqlmodel
from .pixiv_sqlmodel import *
import random


class PixivDB:
    def __init__(self, db_url: str):
        self.engine = sqlmodel.create_engine(db_url)
        sqlmodel.SQLModel.metadata.create_all(self.engine)
        self.session = sqlmodel.Session(self.engine)
        # 启用外键约束
        self.session.connection().execute(sqlmodel.text("PRAGMA foreign_keys=ON"))

    def insert_daily_illust_source_rows(self, rows: list[DailyIllustSource]):
        for row in rows:
            self.session.merge(row)
        self.session.commit()

    def get_daily_illust_nums(self):
        statement = sqlmodel.select(sqlmodel.func.count()).select_from(DailyIllustSource)
        return self.session.exec(statement).first()

    def get_random_daily_illust(self) -> DailyIllustSource | None:
        cnt_statement = sqlmodel.select(sqlmodel.func.count()).where(DailyIllustSource.user_id != 0).select_from(DailyIllustSource)
        rows_cnt = self.session.exec(cnt_statement).first()
        if rows_cnt < 1:
            return None
        elif rows_cnt == 1:
            return self.session.exec(sqlmodel.select(DailyIllustSource)).first()
        random_offset = random.randint(0, rows_cnt - 1)
        # 3. 跳过前 random_offset 行，取下一行
        # SQL: SELECT * FROM hero LIMIT 1 OFFSET 12345
        statement = sqlmodel.select(DailyIllustSource).where(DailyIllustSource.user_id != 0).offset(random_offset).limit(1)
        return self.session.exec(statement).first()

    def get_daily_illust_source_row(self, work_id: int) -> DailyIllustSource | None:
        return self.session.get(DailyIllustSource, work_id)

    def shutdown(self):
        self.session.close()
