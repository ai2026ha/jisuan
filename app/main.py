from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import quote
import os
import io
import csv
import json
import zipfile
from typing import Optional


def beijing_time(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("Asia/Shanghai"))

from fastapi import FastAPI, Depends, Form, File, UploadFile, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, Boolean, ForeignKey, select, func, text, update
from sqlalchemy.engine import URL
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from passlib.context import CryptContext
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).resolve().parent

# Render 继续优先使用 DATABASE_URL。
# 自建服务器 Docker Compose 改用分离的 DB_* / POSTGRES_* 环境变量构造 SQLAlchemy URL，
# 避免数据库密码包含 @ / : # 等特殊字符时被连接字符串错误解析。
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
    elif DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
    DB_CONNECT = DATABASE_URL
elif os.getenv("DB_HOST", "").strip():
    DB_CONNECT = URL.create(
        drivername="postgresql+psycopg2",
        username=os.getenv("DB_USER", os.getenv("POSTGRES_USER", "agent_calc")),
        password=os.getenv("DB_PASSWORD", os.getenv("POSTGRES_PASSWORD", "")),
        host=os.getenv("DB_HOST", "db"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", os.getenv("POSTGRES_DB", "agent_calculator")),
    )
    # 现有代码以 DATABASE_URL 的真值判断 PostgreSQL / SQLite。
    DATABASE_URL = "selfhost-postgresql"
else:
    DB_CONNECT = None

if DB_CONNECT is not None:
    engine = create_engine(
        DB_CONNECT,
        pool_pre_ping=True,
        pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
        pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
    )
else:
    DB_PATH = BASE_DIR.parent / "calculator.db"
    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

def business_round(value: Decimal) -> Decimal:
    """业务取整：未满100直接去掉小数取整数；满100后沿用原规则（首位小数>=4取整数，否则整数减1）。"""
    value = Decimal(value)
    integer = int(value)
    if value < Decimal("100"):
        return Decimal(integer)
    first_decimal = int((abs(value) - abs(integer)) * 10)
    return Decimal(integer if first_decimal >= 4 else integer - 1)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="user", nullable=False)

class Agent(Base):
    __tablename__ = "agents"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    total = Column(Numeric(18, 4), default=0, nullable=False)
    manual_adjust = Column(Numeric(18, 4), default=0, nullable=False)
    note = Column(String(500), default="")
    is_deleted = Column(Boolean, default=False, nullable=False)
    # 显示顺序：严格记录“新增/重新新增”代理的先后顺序，不使用名称排序。
    sort_order = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class Game(Base):
    __tablename__ = "games"
    id = Column(Integer, primary_key=True)
    is_deleted = Column(Boolean, default=False, nullable=False)
    name = Column(String(120), unique=True, nullable=False)
    factor = Column(Numeric(12, 6), default=Decimal("0.94"), nullable=False)
    formula1 = Column(Numeric(12, 6), default=Decimal("0.50"), nullable=False)
    formula2 = Column(Numeric(12, 6), default=Decimal("0.55"), nullable=False)
    formula3 = Column(Numeric(12, 6), default=Decimal("0.45"), nullable=False)
    formula_choice = Column(Integer, default=1, nullable=False)

class Rate(Base):
    __tablename__ = "rates"
    id = Column(Integer, primary_key=True)
    is_deleted = Column(Boolean, default=False, nullable=False)
    name = Column(String(120), nullable=False)
    value = Column(Numeric(18, 8), nullable=False)

class Calculation(Base):
    __tablename__ = "calculations"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    game_id = Column(Integer, ForeignKey("games.id"), nullable=False)
    rate_id = Column(Integer, ForeignKey("rates.id"), nullable=False)
    formula_no = Column(Integer, nullable=False)
    input_number = Column(Numeric(18, 6), nullable=False)
    formula_result = Column(Numeric(18, 6), nullable=False)
    result = Column(Numeric(18, 6), nullable=False)
    cleared = Column(Boolean, default=False, nullable=False)
    agent_name_snapshot = Column(String(120), nullable=True)
    game_name_snapshot = Column(String(120), nullable=True)
    formula_snapshot = Column(String(255), nullable=True)

class ManualAdjustment(Base):
    __tablename__ = "manual_adjustments"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    input_value = Column(String(80), nullable=False)
    old_total = Column(Numeric(18, 4), nullable=False)
    new_total = Column(Numeric(18, 4), nullable=False)
    delta = Column(Numeric(18, 4), nullable=False)
    cleared = Column(Boolean, default=False, nullable=False)

Base.metadata.create_all(engine)

# 兼容已有生产数据库：补充软删除字段
def ensure_soft_delete_columns():
    try:
        with engine.begin() as conn:
            if DATABASE_URL:
                conn.execute(text("ALTER TABLE agents ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE"))
                conn.execute(text("ALTER TABLE games ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE"))
                conn.execute(text("ALTER TABLE rates ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE"))
    except Exception:
        pass

ensure_soft_delete_columns()

# 兼容已有生产数据库：增加代理显示顺序字段。
# 旧数据首次升级时按 id 回填，后续每次新增/重新新增代理都会追加到列表末尾。
def ensure_agent_sort_order_column():
    try:
        with engine.begin() as conn:
            if DATABASE_URL:
                conn.execute(text("ALTER TABLE agents ADD COLUMN IF NOT EXISTS sort_order INTEGER"))
                conn.execute(text("UPDATE agents SET sort_order = id WHERE sort_order IS NULL"))
            else:
                cols = [r[1] for r in conn.execute(text("PRAGMA table_info(agents)")).fetchall()]
                if "sort_order" not in cols:
                    conn.execute(text("ALTER TABLE agents ADD COLUMN sort_order INTEGER"))
                conn.execute(text("UPDATE agents SET sort_order = id WHERE sort_order IS NULL"))
    except Exception:
        pass

ensure_agent_sort_order_column()

# 兼容已有生产数据库：为旧 agents 表补充备注字段
def ensure_agent_note_column():
    try:
        with engine.begin() as conn:
            if DATABASE_URL:
                conn.execute(text("ALTER TABLE agents ADD COLUMN IF NOT EXISTS note VARCHAR(500) DEFAULT ''"))
            else:
                cols = [r[1] for r in conn.execute(text("PRAGMA table_info(agents)")).fetchall()]
                if "note" not in cols:
                    conn.execute(text("ALTER TABLE agents ADD COLUMN note VARCHAR(500) DEFAULT ''"))
    except Exception:
        pass

ensure_agent_note_column()

try:
    with engine.begin() as conn:
        if DATABASE_URL:
            conn.execute(text("ALTER TABLE calculations ADD COLUMN IF NOT EXISTS agent_name_snapshot VARCHAR(120)"))
            conn.execute(text("ALTER TABLE calculations ADD COLUMN IF NOT EXISTS game_name_snapshot VARCHAR(120)"))
            conn.execute(text("ALTER TABLE calculations ADD COLUMN IF NOT EXISTS formula_snapshot VARCHAR(255)"))
        else:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(calculations)")).fetchall()]
            for name in ["agent_name_snapshot","game_name_snapshot","formula_snapshot"]:
                if name not in cols:
                    conn.execute(text(f"ALTER TABLE calculations ADD COLUMN {name} VARCHAR(255)"))
except Exception:
    pass

# 兼容已有生产数据库：增加代理人工调整金额字段
try:
    with engine.begin() as conn:
        if DATABASE_URL:
            conn.execute(text("ALTER TABLE agents ADD COLUMN IF NOT EXISTS manual_adjust NUMERIC(18,4) DEFAULT 0"))
        else:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(agents)")).fetchall()]
            if "manual_adjust" not in cols:
                conn.execute(text("ALTER TABLE agents ADD COLUMN manual_adjust NUMERIC(18,4) DEFAULT 0"))
except Exception:
    pass

# 兼容已有生产数据库：增加清零状态字段（清零隐藏当前账单，但保留历史查询）
try:
    with engine.begin() as conn:
        if DATABASE_URL:
            conn.execute(text("ALTER TABLE calculations ADD COLUMN IF NOT EXISTS cleared BOOLEAN DEFAULT FALSE"))
        else:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(calculations)")).fetchall()]
            if "cleared" not in cols:
                conn.execute(text("ALTER TABLE calculations ADD COLUMN cleared BOOLEAN DEFAULT 0"))
except Exception:
    pass

def seed():
    db = SessionLocal()
    try:
        # 登录账号只更新现有管理员这一行，保留原 User.id。
        # Calculation.user_id 等历史关联因此完全不变；代理/游戏/汇率/计算记录均不会被修改。
        configured_username = os.getenv("ADMIN_USERNAME", "").strip()
        configured_password = os.getenv("ADMIN_PASSWORD", "")

        admin = db.scalar(select(User).where(User.role == "admin").order_by(User.id.asc()))
        if admin:
            if configured_username and configured_username != admin.username:
                duplicate = db.scalar(
                    select(User).where(User.username == configured_username, User.id != admin.id)
                )
                if duplicate:
                    raise RuntimeError("ADMIN_USERNAME 已被其他账号使用")
                admin.username = configured_username
            if configured_password and not pwd.verify(configured_password, admin.password_hash):
                admin.password_hash = pwd.hash(configured_password)
        else:
            # 仅全新数据库没有管理员时才创建默认管理员。
            db.add(User(
                username=configured_username or "admin",
                password_hash=pwd.hash(configured_password or "admin123"),
                role="admin",
            ))

        if not db.scalar(select(Agent)):
            db.add_all([Agent(name="示例代理A", sort_order=1), Agent(name="示例代理B", sort_order=2)])
        if not db.scalar(select(Game)):
            db.add(Game(name="示例游戏", factor=Decimal("0.94"),
                        formula1=Decimal("0.50"), formula2=Decimal("0.55"), formula3=Decimal("0.45"),
                        formula_choice=1))
        if not db.scalar(select(Rate)):
            db.add_all([Rate(name="示例汇率", value=Decimal("1"))])
        db.commit()
    finally:
        db.close()
seed()

def business_round(value: Decimal) -> int:
    """业务取整：未满100直接去掉小数取整数；满100后沿用原规则（首位小数>=4取整数，否则整数减1）。"""
    d = Decimal(str(value))
    integer = int(d)
    if d < Decimal("100"):
        return integer
    fraction_first = int((abs(d) - abs(integer)) * 10)
    return integer if fraction_first >= 4 else integer - 1

app = FastAPI(title="代理计算管理系统")

# WebSocket realtime sync base (v29.1 fixed)
class ConnectionManager:
    def __init__(self):
        self.connections = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.connections.discard(websocket)

    async def broadcast(self, message: str):
        for ws in list(self.connections):
            try:
                await ws.send_text(message)
            except Exception:
                self.disconnect(ws)


ws_manager = ConnectionManager()

SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes", "on"}
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "dev-only-change-me"),
    max_age=60*60*24*7,
    same_site="lax",
    https_only=SESSION_COOKIE_SECURE,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "static"))

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


def db_dep():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def current_user(request: Request, db: Session):
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.get(User, uid)

def require_user(request, db):
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=401)
    return user

def require_admin(request, db):
    user = require_user(request, db)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return user

@app.get("/healthz")
def healthz(db: Session = Depends(db_dep)):
    # Docker / Nginx / 监控系统可用此接口确认应用和数据库都可用。
    db.execute(text("SELECT 1"))
    return {"ok": True, "database": "postgresql" if DATABASE_URL else "sqlite"}

@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(db_dep)):
    if not current_user(request, db):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(db_dep)):
    user = db.scalar(select(User).where(User.username == username.strip()))
    if not user or not pwd.verify(password, user.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "用户名或密码错误"})
    request.session["uid"] = user.id
    return RedirectResponse("/", status_code=303)

@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

@app.get("/api/bootstrap")
def bootstrap(request: Request, db: Session = Depends(db_dep)):
    user = require_user(request, db)
    return {
        "current_user": {"id": user.id, "username": user.username, "role": user.role},
        "agents": [{"id": a.id, "name": a.name, "total": float(a.total or 0), "note": a.note or "", "sort_order": int(a.sort_order or a.id)} for a in db.scalars(select(Agent).where(Agent.is_deleted == False).order_by(Agent.sort_order.asc(), Agent.id.asc())).all()],
        "games": [{"id": g.id, "name": g.name, "formula_choice": g.formula_choice,
                   "factor": float(g.factor), "f1": float(g.formula1), "f2": float(g.formula2), "f3": float(g.formula3)}
                  for g in db.scalars(select(Game).where(Game.is_deleted == False).order_by(Game.name)).all()],
        "rates": [{"id": r.id, "name": r.name, "value": float(r.value)} for r in db.scalars(select(Rate).where(Rate.is_deleted == False).order_by(Rate.name)).all()],
    }

def next_agent_sort_order(db: Session) -> int:
    current_max = db.scalar(select(func.max(Agent.sort_order)))
    return int(current_max or 0) + 1

@app.post("/api/agents")
async def add_agent(request: Request, name: str = Form(...), db: Session = Depends(db_dep)):
    require_user(request, db)
    name = name.strip()
    if not name: raise HTTPException(400, "代理名不能为空")
    if db.scalar(select(Agent).where(Agent.name == name, Agent.is_deleted == False)):
        raise HTTPException(400, "代理已存在")
    old_agent = db.scalar(select(Agent).where(Agent.name == name, Agent.is_deleted == True))
    if old_agent:
        old_agent.is_deleted = False
        # “重新新增”视为一次新的新增操作，放到当前代理列表末尾。
        old_agent.sort_order = next_agent_sort_order(db)
        db.commit()
        await ws_manager.broadcast("agent_updated")
        return {"ok": True}
    db.add(Agent(name=name, sort_order=next_agent_sort_order(db))); db.commit()
    await ws_manager.broadcast("agent_updated")
    return {"ok": True}

@app.put("/api/agents/{agent_id}/name")
async def update_agent_name(agent_id: int, payload: dict, request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    agent = db.get(Agent, agent_id)
    if not agent or agent.is_deleted:
        raise HTTPException(404, "代理不存在")
    name = str(payload.get("name", "") or "").strip()
    if not name:
        raise HTTPException(400, "代理名不能为空")
    if len(name) > 120:
        raise HTTPException(400, "代理名不能超过120个字符")
    if name == agent.name:
        return {"ok": True, "name": agent.name}
    exists = db.scalar(select(Agent).where(Agent.name == name, Agent.id != agent_id))
    if exists:
        raise HTTPException(400, "代理名称已存在")
    agent.name = name
    db.commit()
    await ws_manager.broadcast("agent_updated")
    return {"ok": True, "name": agent.name}

@app.put("/api/agents/{agent_id}/note")
async def update_agent_note(agent_id: int, payload: dict, request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    agent = db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "代理不存在")
    agent.note = str(payload.get("note", "") or "").strip()
    db.commit()
    await ws_manager.broadcast("agent_updated")
    return {"ok": True, "note": agent.note}

@app.put("/api/agents/{agent_id}/adjust")
async def adjust_agent_total(agent_id: int, payload: dict, request: Request, db: Session = Depends(db_dep)):
    user = require_user(request, db)
    # 与并发计算串行化：PostgreSQL 会锁定该代理行，避免手动调整覆盖正在提交的计算。
    # SQLite 会忽略 FOR UPDATE，但单条写事务仍由数据库自身串行化。
    agent = db.scalar(select(Agent).where(Agent.id == agent_id).with_for_update())
    if not agent:
        raise HTTPException(404, "代理不存在")
    value = str(payload.get("value", "")).strip()
    try:
        old_total = Decimal(agent.total or 0)
        if value.startswith("+") or value.startswith("-"):
            delta = Decimal(value)
            new_total = old_total + delta
            agent.manual_adjust = Decimal(agent.manual_adjust or 0) + delta
            agent.total = new_total
        else:
            new_total = Decimal(value)
            delta = new_total - old_total
            agent.manual_adjust = Decimal(agent.manual_adjust or 0) + delta
            agent.total = new_total
        db.add(ManualAdjustment(
            user_id=user.id, agent_id=agent.id, input_value=value,
            old_total=old_total, new_total=new_total, delta=delta
        ))
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(400, "金额格式错误")
    await ws_manager.broadcast("agent_updated")
    return {"ok": True, "total": float(agent.total)}

@app.get("/api/manual-adjustments")
def manual_adjustments(request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    rows = db.scalars(select(ManualAdjustment).order_by(ManualAdjustment.created_at.desc())).all()
    result = []
    for r in rows:
        old_total = Decimal(r.old_total or 0)
        new_total = Decimal(r.new_total or 0)
        delta = Decimal(r.delta or 0)
        sign = "+" if delta >= 0 else "-"
        old_text = format(old_total.normalize(), "f")
        delta_text = format(abs(delta).normalize(), "f")
        new_text = format(new_total.normalize(), "f")
        detail = f"{old_text}{sign}{delta_text}={new_text}"
        result.append({
            "id": r.id,
            "time": beijing_time(r.created_at).strftime("%Y-%m-%d %H:%M:%S"),
            "agent_id": r.agent_id,
            "record_type": "manual",
            "title": "手动调整",
            "detail": detail,
            "delta": float(delta),
            "old_total": float(old_total),
            "new_total": float(new_total),
            "cleared": bool(r.cleared),
        })
    return result

@app.delete("/api/manual-adjustments/{adjustment_id}")
async def delete_manual_adjustment(adjustment_id: int, request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    row = db.get(ManualAdjustment, adjustment_id)
    if not row:
        raise HTTPException(404, "手动调整记录不存在")

    agent = db.get(Agent, row.agent_id)
    if agent:
        delta = Decimal(row.delta or 0)
        # 原子撤销这一笔人工调整，避免与其他设备同时计算时互相覆盖。
        db.execute(
            update(Agent)
            .where(Agent.id == row.agent_id)
            .values(
                total=func.coalesce(Agent.total, Decimal("0")) - delta,
                manual_adjust=func.coalesce(Agent.manual_adjust, Decimal("0")) - delta,
            )
            .execution_options(synchronize_session=False)
        )

    db.delete(row)
    db.commit()
    if agent:
        db.refresh(agent)
    await ws_manager.broadcast("agent_updated")
    return {"ok": True, "total": float(agent.total) if agent else 0}

@app.delete("/api/agents/{agent_id}")
async def del_agent(agent_id: int, request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    agent = db.get(Agent, agent_id)
    if not agent: raise HTTPException(404, "代理不存在")
    agent.is_deleted = True; db.commit()
    await ws_manager.broadcast("agent_updated")
    return {"ok": True}

@app.post("/api/agents/clear")
async def clear_agents(request: Request, agent_ids: list[int] = Form(...), db: Session = Depends(db_dep)):
    require_user(request, db)
    ids = set(agent_ids)
    for aid in ids:
        a = db.get(Agent, aid)
        if a:
            a.total = Decimal("0")
            a.manual_adjust = Decimal("0")
        for c in db.scalars(select(Calculation).where(Calculation.agent_id == aid, Calculation.cleared == False)).all():
            c.cleared = True
        for m in db.scalars(select(ManualAdjustment).where(ManualAdjustment.agent_id == aid, ManualAdjustment.cleared == False)).all():
            m.cleared = True
    db.commit()
    await ws_manager.broadcast("agent_updated")
    return {"ok": True}

@app.post("/api/games")
async def add_game(request: Request, name: str = Form(...), formula_choice: int = Form(...),
             db: Session = Depends(db_dep)):
    require_user(request, db)
    name = name.strip()
    if db.scalar(select(Game).where(Game.name == name, Game.is_deleted == False)):
        raise HTTPException(400, "游戏已存在")
    old_game = db.scalar(select(Game).where(Game.name == name, Game.is_deleted == True))
    if old_game:
        old_game.is_deleted = False
        old_game.formula_choice = formula_choice
        db.commit()
        await ws_manager.broadcast("game_updated")
        return {"ok": True}
    if formula_choice not in (1, 2, 3):
        raise HTTPException(400, "公式选择无效")
    g = Game(name=name, factor=Decimal("0.94"), formula1=Decimal("0.50"),
             formula2=Decimal("0.55"), formula3=Decimal("0.45"), formula_choice=formula_choice)
    db.add(g); db.commit()
    await ws_manager.broadcast("game_updated")
    return {"ok": True}

@app.delete("/api/games/{game_id}")
async def del_game(game_id: int, request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    g = db.get(Game, game_id)
    if not g: raise HTTPException(404, "游戏不存在")
    g.is_deleted = True
    db.commit()
    await ws_manager.broadcast("game_updated")
    return {"ok": True}

@app.post("/api/rates")
async def add_rate(request: Request, name: str = Form(...), value: str = Form(...), db: Session = Depends(db_dep)):
    require_user(request, db)
    try: v = Decimal(value)
    except Exception: raise HTTPException(400, "汇率必须是数字")
    old_rate = db.scalar(select(Rate).where(Rate.name == name.strip(), Rate.is_deleted == True))
    if old_rate:
        old_rate.is_deleted = False
        old_rate.value = v
    else:
        db.add(Rate(name=name.strip(), value=v))
    db.commit()
    await ws_manager.broadcast("rate_updated")
    return {"ok": True}

@app.delete("/api/rates/{rate_id}")
async def del_rate(rate_id: int, request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    r = db.get(Rate, rate_id)
    if not r: raise HTTPException(404, "汇率不存在")
    r.is_deleted = True
    db.commit()
    await ws_manager.broadcast("rate_updated")
    return {"ok": True}

@app.post("/api/calculate")
async def calculate(request: Request, agent_id: int = Form(...), game_id: int = Form(...),
              rate_id: int = Form(...), formula_no: int = Form(...), input_number: str = Form(...),
              db: Session = Depends(db_dep)):
    user = require_user(request, db)
    agent, game, rate = db.get(Agent, agent_id), db.get(Game, game_id), db.get(Rate, rate_id)
    if not agent or not game or not rate: raise HTTPException(400, "选择项不存在")
    formula_no = int(game.formula_choice)
    try:
        n = Decimal(input_number)
        if n != n.to_integral_value():
            raise ValueError
        n = n.to_integral_value()
    except Exception:
        raise HTTPException(400, "自定义数字必须是整数")
    factor = Decimal(game.factor)
    multiplier = [None, Decimal(game.formula1), Decimal(game.formula2), Decimal(game.formula3)][formula_no]
    formula_result = n * factor * multiplier
    if Decimal(rate.value) == 0:
        raise HTTPException(400, "汇率不能为0")
    raw_result = formula_result / Decimal(rate.value)
    result = business_round(raw_result)
    # 多设备并发安全：累计金额必须在数据库内原子递增。
    # 不能使用“读取旧 total -> Python 相加 -> 写回”的方式，否则两台设备同时计算同一代理时
    # 可能发生后提交覆盖先提交，导致少累计一笔。数据库 UPDATE 会对同一行串行化更新，
    # 每一笔计算结果都会在提交时基于当时最新 total 继续累加。
    result_decimal = Decimal(result)
    updated = db.execute(
        update(Agent)
        .where(Agent.id == agent.id, Agent.is_deleted == False)
        .values(total=func.coalesce(Agent.total, Decimal("0")) + result_decimal)
        .execution_options(synchronize_session=False)
    )
    if updated.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "代理状态已变化，请重新选择后再计算")

    db.add(Calculation(user_id=user.id, agent_id=agent.id, game_id=game.id, rate_id=rate.id,
                       formula_no=formula_no, input_number=n, formula_result=formula_result, result=result_decimal,
                       agent_name_snapshot=agent.name,
                       game_name_snapshot=game.name,
                       formula_snapshot=f"{n}×{factor}×{multiplier}÷{rate.value}"))
    db.commit()
    db.refresh(agent)
    await ws_manager.broadcast("calculation_updated")
    return {"ok": True, "formula_result": float(formula_result), "result": float(result_decimal), "total": float(agent.total)}

@app.delete("/api/history/delete/{calc_id}")
async def delete_calculation(calc_id: int, request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    c = db.get(Calculation, calc_id)
    if not c:
        raise HTTPException(404, "计算记录不存在")
    agent = db.get(Agent, c.agent_id)
    if agent:
        # 原子扣回该计算结果；与其他设备同时新增计算也不会发生覆盖。
        db.execute(
            update(Agent)
            .where(Agent.id == c.agent_id)
            .values(total=func.coalesce(Agent.total, Decimal("0")) - Decimal(c.result or 0))
            .execution_options(synchronize_session=False)
        )
    db.delete(c)
    db.commit()
    if agent:
        db.refresh(agent)
    await ws_manager.broadcast("calculation_updated")
    return {"ok": True, "total": float(agent.total) if agent else 0}

@app.get("/api/history")
def history(request: Request, db: Session = Depends(db_dep), day: Optional[str] = None):
    require_user(request, db)
    q = select(Calculation, Agent.name, Game.name, Rate.name, Rate.value, User.username).outerjoin(Agent, Calculation.agent_id==Agent.id).outerjoin(Game, Calculation.game_id==Game.id).outerjoin(Rate, Calculation.rate_id==Rate.id).outerjoin(User, Calculation.user_id==User.id)
    if day:
        try:
            d = date.fromisoformat(day)
            q = q.where(Calculation.created_at >= datetime.combine(d, datetime.min.time()),
                       Calculation.created_at < datetime.combine(d, datetime.max.time()))
        except ValueError: raise HTTPException(400, "日期格式错误")
    q = q.order_by(Calculation.created_at.desc())
    rows = db.execute(q).all()
    fixed = {1:"0.94×0.5", 2:"0.94×0.55", 3:"0.94×0.45"}
    return [{"id": c.id, "time": beijing_time(c.created_at).strftime("%Y-%m-%d %H:%M:%S"), "agent_id": c.agent_id,
             "agent": c.agent_name_snapshot or an or "", "game": c.game_name_snapshot or gn or "", "rate": rn or "", "formula": c.formula_snapshot or f"公式{c.formula_no}", "input": float(c.input_number),
             "formula_result": float(c.formula_result), "result": float(c.result), "user": un,
             "expression": c.formula_snapshot or f"{Decimal(c.input_number):g}×{fixed.get(c.formula_no, '')}÷{Decimal(rv):g}", "cleared": bool(c.cleared)}
            for c, an, gn, rn, rv, un in rows]

@app.delete("/api/history/clear")
async def clear_history(request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    # 仅清除历史记录，不修改代理金额、代理状态、游戏、汇率等数据
    deleted = db.query(Calculation).delete(synchronize_session=False)
    db.commit()
    await ws_manager.broadcast("calculation_updated")
    return {"ok": True, "deleted": deleted}

def _backup_value(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        # 金额/汇率使用字符串，避免 JSON 浮点精度损失。
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value

def _dump_model_rows(db: Session, model):
    """仅导出当前有效数据：排除软删除记录、已清零历史及依赖已删除对象的历史。"""
    columns = [column.name for column in model.__table__.columns]
    stmt = select(model)
    if hasattr(model, "is_deleted"):
        stmt = stmt.where(model.is_deleted.is_(False))
    if hasattr(model, "cleared"):
        stmt = stmt.where(model.cleared.is_(False))
    if model is Calculation:
        stmt = (stmt
                .join(Agent, Calculation.agent_id == Agent.id)
                .join(Game, Calculation.game_id == Game.id)
                .join(Rate, Calculation.rate_id == Rate.id)
                .where(Agent.is_deleted.is_(False), Game.is_deleted.is_(False), Rate.is_deleted.is_(False)))
    elif model is ManualAdjustment:
        stmt = (stmt
                .join(Agent, ManualAdjustment.agent_id == Agent.id)
                .where(Agent.is_deleted.is_(False)))
    rows = db.scalars(stmt.order_by(model.id.asc())).all()
    return columns, [
        {column: _backup_value(getattr(row, column)) for column in columns}
        for row in rows
    ]

BACKUP_MODELS = [
    ("users", User),
    ("agents", Agent),
    ("games", Game),
    ("rates", Rate),
    ("calculations", Calculation),
    ("manual_adjustments", ManualAdjustment),
]
BACKUP_MODEL_MAP = dict(BACKUP_MODELS)
BACKUP_UPLOAD_MAX_BYTES = int(os.getenv("BACKUP_UPLOAD_MAX_MB", "100")) * 1024 * 1024
BACKUP_JSON_MAX_BYTES = int(os.getenv("BACKUP_JSON_MAX_MB", "300")) * 1024 * 1024


def _backup_error(message: str, status_code: int = 400):
    raise HTTPException(status_code=status_code, detail=message)


def _validate_backup_bytes(raw: bytes):
    """读取并严格校验网页导出的当前数据 ZIP，不解压到磁盘。"""
    if not raw:
        _backup_error("备份文件为空")
    if len(raw) > BACKUP_UPLOAD_MAX_BYTES:
        _backup_error(f"备份 ZIP 超过上传上限 {BACKUP_UPLOAD_MAX_BYTES // 1024 // 1024} MB", 413)

    try:
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            try:
                info = zf.getinfo("all_data.json")
            except KeyError:
                _backup_error("不是有效备份：缺少 all_data.json")
            if info.flag_bits & 0x1:
                _backup_error("不支持加密 ZIP")
            if info.file_size > BACKUP_JSON_MAX_BYTES:
                _backup_error(f"all_data.json 超过解析上限 {BACKUP_JSON_MAX_BYTES // 1024 // 1024} MB", 413)
            payload = json.loads(zf.read(info).decode("utf-8"))
    except HTTPException:
        raise
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError):
        _backup_error("备份文件损坏或格式不正确")

    if not isinstance(payload, dict):
        _backup_error("备份内容格式错误")
    manifest = payload.get("manifest") or {}
    data = payload.get("data")
    if not isinstance(data, dict):
        _backup_error("备份缺少 data 数据区")
    if int(manifest.get("format_version") or 0) != 1:
        _backup_error("备份版本不支持，请使用本系统导出的当前数据 ZIP")

    counts = {}
    id_sets = {}
    normalized = {}
    for table_name, model in BACKUP_MODELS:
        rows = data.get(table_name)
        if not isinstance(rows, list):
            _backup_error(f"备份缺少数据表：{table_name}")
        allowed_columns = {column.name for column in model.__table__.columns}
        required_columns = {column.name for column in model.__table__.columns if not column.nullable and column.default is None}
        required_columns.add("id")
        seen_ids = set()
        normalized_rows = []
        for idx, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                _backup_error(f"{table_name} 第 {idx} 条记录格式错误")
            unknown = set(row) - allowed_columns
            if unknown:
                _backup_error(f"{table_name} 包含未知字段：{', '.join(sorted(unknown))}")
            missing = required_columns - set(row)
            if missing:
                _backup_error(f"{table_name} 缺少必要字段：{', '.join(sorted(missing))}")
            try:
                row_id = int(row.get("id"))
            except (TypeError, ValueError):
                _backup_error(f"{table_name} 第 {idx} 条记录 ID 无效")
            if row_id <= 0 or row_id in seen_ids:
                _backup_error(f"{table_name} 存在无效或重复 ID：{row_id}")
            seen_ids.add(row_id)
            if "is_deleted" in row and row.get("is_deleted") not in (False, 0, None):
                _backup_error(f"{table_name} 含已删除数据，按当前备份规则禁止导入")
            if "cleared" in row and row.get("cleared") not in (False, 0, None):
                _backup_error(f"{table_name} 含已清零历史，按当前备份规则禁止导入")

            converted = {}
            for column in model.__table__.columns:
                if column.name not in row:
                    continue
                value = row.get(column.name)
                if value is None:
                    converted[column.name] = None
                elif isinstance(column.type, DateTime):
                    try:
                        converted[column.name] = datetime.fromisoformat(str(value))
                    except ValueError:
                        _backup_error(f"{table_name}.{column.name} 时间格式无效")
                elif isinstance(column.type, Numeric):
                    try:
                        converted[column.name] = Decimal(str(value))
                    except Exception:
                        _backup_error(f"{table_name}.{column.name} 数值格式无效")
                elif isinstance(column.type, Integer):
                    try:
                        converted[column.name] = int(value)
                    except (TypeError, ValueError):
                        _backup_error(f"{table_name}.{column.name} 整数格式无效")
                elif isinstance(column.type, Boolean):
                    if value not in (True, False, 0, 1):
                        _backup_error(f"{table_name}.{column.name} 布尔值无效")
                    converted[column.name] = bool(value)
                else:
                    converted[column.name] = str(value)
            normalized_rows.append(converted)

        counts[table_name] = len(rows)
        id_sets[table_name] = seen_ids
        normalized[table_name] = normalized_rows

    if not id_sets["users"]:
        _backup_error("备份中没有账号，恢复后将无法登录")
    if not any(str(row.get("role", "")) == "admin" for row in data["users"]):
        _backup_error("备份中没有管理员账号，恢复后将无法管理系统")

    # 外键完整性：当前数据备份必须能独立恢复，不能引用已删除而未导出的父记录。
    for row in data["calculations"]:
        refs = [
            ("user_id", "users"),
            ("agent_id", "agents"),
            ("game_id", "games"),
            ("rate_id", "rates"),
        ]
        for field, parent in refs:
            try:
                ref_id = int(row.get(field))
            except (TypeError, ValueError):
                _backup_error(f"calculations.{field} 无效")
            if ref_id not in id_sets[parent]:
                _backup_error(f"备份存在无法恢复的关联：calculations.{field}={ref_id} 不在 {parent} 中")
    for row in data["manual_adjustments"]:
        for field, parent in [("user_id", "users"), ("agent_id", "agents")]:
            try:
                ref_id = int(row.get(field))
            except (TypeError, ValueError):
                _backup_error(f"manual_adjustments.{field} 无效")
            if ref_id not in id_sets[parent]:
                _backup_error(f"备份存在无法恢复的关联：manual_adjustments.{field}={ref_id} 不在 {parent} 中")

    return manifest, normalized, counts


def _replace_database_from_backup(normalized):
    """单事务覆盖六张业务表；失败自动整体回滚。"""
    child_first = [ManualAdjustment, Calculation, Rate, Game, Agent, User]
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            # 防止两次恢复操作并发执行；固定 advisory lock 只用于本应用恢复任务。
            conn.execute(text("SELECT pg_advisory_xact_lock(8675309001)"))
            conn.execute(text(
                "TRUNCATE TABLE manual_adjustments, calculations, rates, games, agents, users RESTART IDENTITY CASCADE"
            ))
        else:
            for model in child_first:
                conn.execute(model.__table__.delete())

        for table_name, model in BACKUP_MODELS:
            rows = normalized[table_name]
            if rows:
                conn.execute(model.__table__.insert(), rows)

        if engine.dialect.name == "postgresql":
            for table_name, _model in BACKUP_MODELS:
                max_id = max((int(row["id"]) for row in normalized[table_name]), default=0)
                seq = conn.execute(
                    text("SELECT pg_get_serial_sequence(:table_name, 'id')"),
                    {"table_name": table_name},
                ).scalar()
                if seq:
                    conn.execute(
                        text("SELECT setval(CAST(:seq AS regclass), :value, :called)"),
                        {"seq": seq, "value": max(max_id, 1), "called": bool(max_id)},
                    )


@app.post("/api/import/preview")
async def import_preview(
    request: Request,
    backup: UploadFile = File(...),
    db: Session = Depends(db_dep),
):
    require_admin(request, db)
    filename = (backup.filename or "").strip()
    if filename and not filename.lower().endswith(".zip"):
        _backup_error("请选择系统导出的 ZIP 备份文件")
    raw = await backup.read(BACKUP_UPLOAD_MAX_BYTES + 1)
    if len(raw) > BACKUP_UPLOAD_MAX_BYTES:
        _backup_error(f"备份 ZIP 超过上传上限 {BACKUP_UPLOAD_MAX_BYTES // 1024 // 1024} MB", 413)
    manifest, _normalized, counts = _validate_backup_bytes(raw)
    return {
        "ok": True,
        "filename": filename or "backup.zip",
        "size_bytes": len(raw),
        "exported_at": manifest.get("exported_at"),
        "source": manifest.get("source"),
        "counts": counts,
        "warning": "确认后将覆盖当前六张业务表；恢复成功后需要重新登录。",
    }


@app.post("/api/import/restore")
async def import_restore(
    request: Request,
    backup: UploadFile = File(...),
    confirm: str = Form(...),
    db: Session = Depends(db_dep),
):
    require_admin(request, db)
    if confirm != "REPLACE_CURRENT_DATA":
        _backup_error("缺少覆盖确认，未执行恢复")
    filename = (backup.filename or "").strip()
    if filename and not filename.lower().endswith(".zip"):
        _backup_error("请选择系统导出的 ZIP 备份文件")
    raw = await backup.read(BACKUP_UPLOAD_MAX_BYTES + 1)
    if len(raw) > BACKUP_UPLOAD_MAX_BYTES:
        _backup_error(f"备份 ZIP 超过上传上限 {BACKUP_UPLOAD_MAX_BYTES // 1024 // 1024} MB", 413)
    _manifest, normalized, counts = _validate_backup_bytes(raw)
    # require_admin() 的 SELECT 会让当前 Session 保持一个读事务。先结束该事务，
    # 否则 PostgreSQL TRUNCATE 等待本请求自己的 ACCESS SHARE 锁，会造成自锁。
    db.rollback()
    try:
        _replace_database_from_backup(normalized)
    except HTTPException:
        raise
    except Exception as exc:
        _backup_error(f"恢复失败，数据库已自动回滚：{type(exc).__name__}", 500)

    # 当前管理员可能已经被备份中的 users 表替换，主动退出当前会话，避免沿用旧身份。
    request.session.clear()
    await ws_manager.broadcast("database_restored")
    return {"ok": True, "counts": counts, "message": "数据恢复完成，请重新登录"}

@app.get("/api/export/all-data")
def export_all_data(request: Request, db: Session = Depends(db_dep)):
    """管理员导出当前有效数据库数据；兼容 Render DATABASE_URL 与自建 PostgreSQL。"""
    admin = require_admin(request, db)

    models = BACKUP_MODELS

    all_data = {}
    counts = {}
    max_ids = {}
    csv_files = {}
    for table_name, model in models:
        columns, rows = _dump_model_rows(db, model)
        all_data[table_name] = rows
        counts[table_name] = len(rows)
        max_ids[table_name] = max((int(row["id"]) for row in rows if row.get("id") is not None), default=0)

        sio = io.StringIO(newline="")
        writer = csv.DictWriter(sio, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if value is None else value for key, value in row.items()})
        csv_files[table_name] = sio.getvalue()

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    manifest = {
        "exported_at": now.isoformat(),
        "source": "postgresql" if DATABASE_URL else "local_sqlite",
        "database_name": os.getenv("DATABASE_NAME", "postgresql") if DATABASE_URL else "calculator.db",
        "exported_by_user_id": admin.id,
        "exported_by_username": admin.username,
        "tables": counts,
        "max_ids": max_ids,
        "format_version": 1,
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("all_data.json", json.dumps({"manifest": manifest, "data": all_data}, ensure_ascii=False, indent=2))
        for table_name, content in csv_files.items():
            zf.writestr(f"csv/{table_name}.csv", "\ufeff" + content)
        zf.writestr(
            "README.txt",
            "这是代理计算中心的当前有效数据备份。\n"
            "本文件直接从当前运行环境正在使用的 PostgreSQL 导出（兼容 Render 与自建服务器）。\n"
            "包含 users、agents、games、rates、calculations、manual_adjustments 六张表的当前有效记录。\n"
            "不包含 is_deleted=true 的已删除数据，也不包含 cleared=true 的已清零历史数据。\n"
            "all_data.json 用于数据迁移/恢复；csv/ 目录方便人工查看。\n"
            "注意：users 表包含密码哈希，请将此备份视为敏感文件并妥善保管。\n"
        )
    payload = buffer.getvalue()
    filename = f"当前数据_{now.strftime('%Y%m%d_%H%M%S')}.zip"
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )

@app.get("/api/export/txt")
def export_txt(request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    today = date.today()
    # TXT 按代理管理/控制台当前显示的代理总金额导出；未满 50 的代理不导出。
    agents = db.scalars(
        select(Agent)
        .where(Agent.is_deleted == False, Agent.total >= Decimal("50"))
        .order_by(Agent.sort_order.asc(), Agent.id.asc())
    ).all()
    lines = [
        f"{a.name}    {format(Decimal(a.total), 'f').rstrip('0').rstrip('.')}"
        for a in agents
    ]
    content = "\n\n".join(lines) + ("\n" if lines else "")
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8", headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(f'结算{today}.txt')}"})
