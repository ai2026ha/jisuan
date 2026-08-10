from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import quote
import os
from typing import Optional


def beijing_time(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("Asia/Shanghai"))

from fastapi import FastAPI, Depends, Form, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, Boolean, ForeignKey, select, func, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from passlib.context import CryptContext
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
    elif DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
else:
    DB_PATH = BASE_DIR.parent / "calculator.db"
    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

def business_round(value: Decimal) -> Decimal:
    """业务取整：小数第一位 >= 4 取整数；小数第一位 < 4，整数减 1。"""
    value = Decimal(value)
    integer = int(value)
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
    """业务取整：小数第一位 >= 4 取整数；< 4 则整数减 1。"""
    d = Decimal(str(value))
    integer = int(d)
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

app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "dev-only-change-me"), max_age=60*60*24*7)
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
    require_user(request, db)
    return {
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
    require_user(request, db)
    agent = db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "代理不存在")
    value = str(payload.get("value", "")).strip()
    try:
        old_total = Decimal(agent.total or 0)
        if value.startswith("+") or value.startswith("-"):
            delta = Decimal(value)
            agent.manual_adjust = Decimal(agent.manual_adjust or 0) + delta
            agent.total = old_total + delta
        else:
            new_total = Decimal(value)
            delta = new_total - old_total
            agent.manual_adjust = Decimal(agent.manual_adjust or 0) + delta
            agent.total = new_total
        db.commit()
    except Exception:
        raise HTTPException(400, "金额格式错误")
    await ws_manager.broadcast("agent_updated")
    return {"ok": True, "total": float(agent.total)}

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
    agent.total = Decimal(agent.total or 0) + result
    db.add(Calculation(user_id=user.id, agent_id=agent.id, game_id=game.id, rate_id=rate.id,
                       formula_no=formula_no, input_number=n, formula_result=formula_result, result=result,
                       agent_name_snapshot=agent.name,
                       game_name_snapshot=game.name,
                       formula_snapshot=f"{n}×{factor}×{multiplier}÷{rate.value}"))
    db.commit()
    await ws_manager.broadcast("calculation_updated")
    return {"ok": True, "formula_result": float(formula_result), "result": float(result), "total": float(agent.total)}

@app.delete("/api/history/delete/{calc_id}")
async def delete_calculation(calc_id: int, request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    c = db.get(Calculation, calc_id)
    if not c:
        raise HTTPException(404, "计算记录不存在")
    agent = db.get(Agent, c.agent_id)
    if agent:
        agent.total = Decimal(agent.total or 0) - Decimal(c.result or 0)
    db.delete(c)
    db.commit()
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

@app.get("/api/export/txt")
def export_txt(request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    today = date.today()
    start = datetime.combine(today, datetime.min.time())
    end = datetime.combine(today, datetime.max.time())
    agents = db.scalars(select(Agent).where(Agent.is_deleted == False, Agent.total >= Decimal("50")).order_by(Agent.sort_order.asc(), Agent.id.asc())).all()
    lines = []
    for a in agents:
        has_today = db.scalar(select(func.count(Calculation.id)).where(
            Calculation.agent_id == a.id,
            Calculation.created_at >= start,
            Calculation.created_at <= end
        ))
        if has_today:
            lines.append(f"{a.name}    {format(Decimal(a.total), 'f').rstrip('0').rstrip('.')}")
    content = "\n".join(lines) + ("\n" if lines else "")
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8", headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(f'结算{today}.txt')}"})
