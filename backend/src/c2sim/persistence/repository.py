"""One SQLite writer, immutable boundary checkpoints, and compare-and-swap commits."""

from copy import deepcopy
import fcntl
from pathlib import Path
import threading
from sqlalchemy import create_engine, event, ForeignKey, Integer, JSON, String, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from alembic import command
from alembic.config import Config
from c2sim.domain.records import Conflict


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    latest_seq: Mapped[int] = mapped_column(Integer)
    parent_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    parent_checkpoint_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CheckpointRow(Base):
    __tablename__ = "checkpoints"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[dict] = mapped_column(JSON)


class Repository:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(str(self.path) + ".lock.writer", "a+")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            raise RuntimeError(
                "A writer already owns this database; use one backend worker"
            ) from None
        self.mutex = threading.RLock()
        self.engine = create_engine(
            f"sqlite:///{self.path}", connect_args={"check_same_thread": False}
        )

        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")

        try:
            config = Config()
            migrations = Path(__file__).parents[1] / "migrations"
            if not migrations.is_dir():
                migrations = Path(__file__).parents[3] / "migrations"
            config.set_main_option("script_location", str(migrations))
            with self.engine.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
        except Exception:
            self.close()
            raise

    def close(self):
        self.engine.dispose()
        if not self._lock_file.closed:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()

    def create(self, state):
        with self.mutex, self.engine.begin() as connection:
            connection.execute(
                RunRow.__table__.insert().values(
                    id=state["run_id"],
                    latest_seq=state["commit_seq"],
                    parent_run_id=state.get("parent_run_id"),
                    parent_checkpoint_seq=state.get("parent_checkpoint_seq"),
                )
            )
            connection.execute(
                CheckpointRow.__table__.insert().values(
                    run_id=state["run_id"], seq=state["commit_seq"], state=state
                )
            )

    def load(self, run_id, seq=None):
        with self.engine.connect() as connection:
            if seq is None:
                seq = connection.scalar(select(RunRow.latest_seq).where(RunRow.id == run_id))
            result = connection.scalar(
                select(CheckpointRow.state).where(
                    CheckpointRow.run_id == run_id, CheckpointRow.seq == seq
                )
            )
            if result is None:
                raise KeyError(f"Unknown run/checkpoint: {run_id}/{seq}")
            return deepcopy(result)

    def commit(self, state, expected_seq):
        with self.mutex, self.engine.begin() as connection:
            result = connection.execute(
                update(RunRow)
                .where(RunRow.id == state["run_id"], RunRow.latest_seq == expected_seq)
                .values(latest_seq=state["commit_seq"])
            )
            if result.rowcount != 1:
                raise Conflict("Concurrent commit")
            connection.execute(
                CheckpointRow.__table__.insert().values(
                    run_id=state["run_id"], seq=state["commit_seq"], state=state
                )
            )

    def history(self, run_id):
        with self.engine.connect() as connection:
            return list(
                connection.scalars(
                    select(CheckpointRow.state)
                    .where(CheckpointRow.run_id == run_id)
                    .order_by(CheckpointRow.seq)
                )
            )

    def run_ids(self):
        with self.engine.connect() as connection:
            return list(connection.scalars(select(RunRow.id).order_by(RunRow.id)))
