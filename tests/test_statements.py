import os
import unittest
from datetime import datetime

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, Bot, Trade, User
from app.statements import _previous_month_bounds, build_user_statement


class StatementPeriodTotalsTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.user = User(email="statement@example.com", hashed_password="hash")
        self.db.add(self.user)
        self.db.flush()
        self.running_bot = Bot(
            owner_id=self.user.id,
            name="Running Bot",
            realized_pnl=999.0,
            trade_count=99,
            funds_allocated=500.0,
            running=True,
        )
        self.paused_bot = Bot(
            owner_id=self.user.id,
            name="Paused Bot",
            realized_pnl=-200.0,
            trade_count=20,
            funds_allocated=250.0,
            running=False,
        )
        self.db.add_all([self.running_bot, self.paused_bot])
        self.db.commit()
        self.period_start, self.period_end = _previous_month_bounds(datetime(2026, 9, 1, 13))

    def tearDown(self):
        self.db.close()

    def _statement(self):
        return build_user_statement(
            self.user,
            [self.running_bot, self.paused_bot],
            db=self.db,
            period_start=self.period_start,
            period_end=self.period_end,
        )

    def _trade(self, bot, side, qty, price, created_at):
        self.db.add(Trade(
            owner_id=self.user.id,
            bot_id=bot.id,
            ticker="BTC/USD",
            side=side,
            qty=qty,
            price=price,
            created_at=created_at,
        ))
        self.db.commit()

    def test_cumulative_bot_totals_are_excluded_without_period_trades(self):
        statement = self._statement()

        self.assertEqual(statement["total_pnl"], 0.0)
        self.assertTrue(all(row["pnl"] == 0.0 for row in statement["bot_rows"]))
        self.assertTrue(all(row["trades"] == 0 for row in statement["bot_rows"]))

    def test_period_buys_and_sells_use_weighted_average_cost(self):
        self._trade(self.running_bot, "buy", 2, 100, datetime(2026, 8, 3))
        self._trade(self.running_bot, "buy", 2, 120, datetime(2026, 8, 4))
        self._trade(self.running_bot, "sell", 3, 130, datetime(2026, 8, 5))

        row = next(row for row in self._statement()["bot_rows"] if row["name"] == "Running Bot")
        self.assertEqual(row["pnl"], 60.0)
        self.assertEqual(row["trades"], 3)

    def test_period_start_is_included_and_period_end_is_excluded(self):
        self._trade(self.running_bot, "buy", 1, 100, self.period_start)
        self._trade(self.running_bot, "sell", 1, 125, datetime(2026, 8, 31, 23, 59, 59))
        self._trade(self.running_bot, "buy", 5, 1, self.period_end)

        row = next(row for row in self._statement()["bot_rows"] if row["name"] == "Running Bot")
        self.assertEqual(row["pnl"], 25.0)
        self.assertEqual(row["trades"], 2)

    def test_allocations_and_statuses_are_unchanged(self):
        statement = self._statement()
        rows = {row["name"]: row for row in statement["bot_rows"]}

        self.assertEqual(statement["total_allocated"], 500.0)
        self.assertEqual(rows["Running Bot"]["status"], "Running")
        self.assertEqual(rows["Paused Bot"]["status"], "Paused")
        self.assertEqual(rows["Paused Bot"]["allocated"], 250.0)


if __name__ == "__main__":
    unittest.main()
