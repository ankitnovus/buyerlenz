"""Database tables: accounts and sessions, the activity log, reference lists, and uploaded survey data."""
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Company(Base):
    """A subscribing client company, or Novus Insights itself."""
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    is_novus: Mapped[bool] = mapped_column(Boolean, default=False)
    brand_id: Mapped[Optional[int]] = mapped_column(ForeignKey("brands.id"))  # the client's own brand ("Yours")
    seat_limit: Mapped[int] = mapped_column(Integer, default=1, server_default="1")  # how many active logins
    # Clients see every published quarter except the ones removed here, e.g. ["2025Q4"]
    removed_quarters: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    renewal_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)  # always stored lower-case
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(200))  # scrambled; the real password is never stored
    role: Mapped[str] = mapped_column(String(20))  # "admin" (Novus staff) or "client"
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")  # after a temporary one
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    company: Mapped[Company] = relationship(lazy="joined")


class LoginSession(Base):
    """One signed-in device. Only the scrambled token is stored, so a database leak cannot be replayed."""
    __tablename__ = "login_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    device: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[Optional[str]] = mapped_column(String(40))  # signed_out, replaced, expired
    view_as_brand_id: Mapped[Optional[int]] = mapped_column(ForeignKey("brands.id"))  # Novus staff "View as client"

    user: Mapped[User] = relationship(lazy="joined")
    view_as_brand: Mapped[Optional["Brand"]] = relationship(lazy="joined")


class SeatRequest(Base):
    """'Ask for another seat': picked up by the Novus account team."""
    __tablename__ = "seat_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    handled: Mapped[bool] = mapped_column(Boolean, default=False)


class ActivityLog(Base):
    """Everything users do that matters for security or billing. Rows are only ever added, never changed."""
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    company_id: Mapped[Optional[int]] = mapped_column(ForeignKey("companies.id"), index=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[Optional[str]] = mapped_column(String(64))


# ---------- reference lists: brands, models, questions ----------

class Brand(Base):
    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)


class CarModel(Base):
    """A model buyers can be reported on. CNG versions are rolled into their petrol twin."""
    __tablename__ = "car_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"))
    segment: Mapped[Optional[str]] = mapped_column(String(40))  # body type; filled in by Novus staff
    e5_codes: Mapped[list] = mapped_column(JSON, default=list)  # codes in the "models considered" grid

    brand: Mapped[Brand] = relationship(lazy="joined")


class ModelName(Base):
    """Every way a purchased model is written in the files (E8), pointing to one CarModel."""
    __tablename__ = "model_names"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("car_models.id"))


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    wording: Mapped[str] = mapped_column(String(1000), default="")
    theme: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(20))
    sort: Mapped[int] = mapped_column(Integer, default=0)


class AnswerOption(Base):
    """One possible answer to a question. New options found in later files are added automatically."""
    __tablename__ = "answer_options"

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), index=True)
    key: Mapped[str] = mapped_column(String(300))  # column code for several-answer questions, else the label
    label: Mapped[str] = mapped_column(String(300))
    sort: Mapped[int] = mapped_column(Integer, default=0)


# ---------- uploaded survey data ----------

class Upload(Base):
    """One uploaded file. Its buyers stay invisible to clients until it is published."""
    __tablename__ = "uploads"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)  # processing, failed, checked, published, rolled_back
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    rolled_back_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Respondent(Base):
    """One interviewed buyer. No name, phone or location is ever stored."""
    __tablename__ = "respondents"

    id: Mapped[int] = mapped_column(primary_key=True)
    upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id", ondelete="CASCADE"), index=True)
    record: Mapped[str] = mapped_column(String(40), index=True)  # the survey tool's interview number
    model_name: Mapped[str] = mapped_column(String(100))  # as written in the file (E8)
    purchase_month: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False))
    quarter: Mapped[Optional[str]] = mapped_column(String(6), index=True)  # e.g. 2025Q4


class Asked(Base):
    """Which buyers were asked which question: the base for every percentage."""
    __tablename__ = "asked"

    respondent_id: Mapped[int] = mapped_column(ForeignKey("respondents.id", ondelete="CASCADE"), primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), primary_key=True, index=True)


class Answer(Base):
    """One chosen answer."""
    __tablename__ = "answers"

    respondent_id: Mapped[int] = mapped_column(ForeignKey("respondents.id", ondelete="CASCADE"), primary_key=True)
    option_id: Mapped[int] = mapped_column(ForeignKey("answer_options.id"), primary_key=True, index=True)


class NumberAnswer(Base):
    """An exact number (age, income, price paid...)."""
    __tablename__ = "number_answers"

    respondent_id: Mapped[int] = mapped_column(ForeignKey("respondents.id", ondelete="CASCADE"), primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), primary_key=True)
    value: Mapped[float] = mapped_column()


class Consideration(Base):
    """A model the buyer considered (E5), with its rank and whether they test drove it or visited a dealer."""
    __tablename__ = "considerations"

    respondent_id: Mapped[int] = mapped_column(ForeignKey("respondents.id", ondelete="CASCADE"), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, primary_key=True)  # 1-3, in order of E5 code (see ingest/reader.py)
    e5_code: Mapped[str] = mapped_column(String(20))
    model_id: Mapped[Optional[int]] = mapped_column(ForeignKey("car_models.id"), index=True)  # empty for "Other"
    test_drive: Mapped[bool] = mapped_column(Boolean, default=False)
    dealer_visit: Mapped[bool] = mapped_column(Boolean, default=False)


class Rejection(Base):
    """A reason the buyer gave for rejecting their rank 1, 2 or 3 considered model (E7)."""
    __tablename__ = "rejections"

    respondent_id: Mapped[int] = mapped_column(ForeignKey("respondents.id", ondelete="CASCADE"), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, primary_key=True)
    option_id: Mapped[int] = mapped_column(ForeignKey("answer_options.id"), primary_key=True)


class SavedCut(Base):
    """A data cut a client saved. It holds the choices, not the numbers, so it re-runs on any quarter."""
    __tablename__ = "saved_cuts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    questions: Mapped[list] = mapped_column(JSON)
    columns: Mapped[list] = mapped_column(JSON)
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserLayout(Base):
    """One person's own Home and Buyer Profile layout. Empty (null) = the standard Novus layout."""
    __tablename__ = "user_layouts"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    home: Mapped[Optional[dict]] = mapped_column(JSON)
    profile: Mapped[Optional[dict]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
