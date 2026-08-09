from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import os
from typing import Optional

from fastapi import FastAPI, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, ForeignKey, select, func
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
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class Game(Base):
    __tablename__ = "games"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    factor = Column(Numeric(12, 6), default=Decimal("0.94"), nullable=False)
    formula1 = Column(Numeric(12, 6), default=Decimal("0.50"), nullable=False)
    formula2 = Column(Numeric(12, 6), default=Decimal("0.55"), nullable=False)
    formula3 = Column(Numeric(12, 6), default=Decimal("0.45"), nullable=False)
    formula_choice = Column(Integer, default=1, nullable=False)

class Rate(Base):
    __tablename__ = "rates"
    id = Column(Integer, primary_key=True)
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

Base.metadata.create_all(engine)

def seed():
    db = SessionLocal()
    try:
        if not db.scalar(select(User).where(User.username == "admin")):
            db.add(User(username="admin", password_hash=pwd.hash("admin123"), role="admin"))
        if not db.scalar(select(Agent)):
            db.add_all([Agent(name="示例代理A"), Agent(name="示例代理B")])
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
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "dev-only-change-me"), max_age=60*60*24*7)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "static"))

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
        "agents": [{"id": a.id, "name": a.name, "total": float(a.total or 0)} for a in db.scalars(select(Agent).order_by(Agent.name)).all()],
        "games": [{"id": g.id, "name": g.name, "formula_choice": g.formula_choice,
                   "factor": float(g.factor), "f1": float(g.formula1), "f2": float(g.formula2), "f3": float(g.formula3)}
                  for g in db.scalars(select(Game).order_by(Game.name)).all()],
        "rates": [{"id": r.id, "name": r.name, "value": float(r.value)} for r in db.scalars(select(Rate).order_by(Rate.name)).all()],
    }

@app.post("/api/agents")
def add_agent(request: Request, name: str = Form(...), db: Session = Depends(db_dep)):
    require_user(request, db)
    name = name.strip()
    if not name: raise HTTPException(400, "代理名不能为空")
    if db.scalar(select(Agent).where(Agent.name == name)): raise HTTPException(400, "代理已存在")
    db.add(Agent(name=name)); db.commit()
    return {"ok": True}

@app.delete("/api/agents/{agent_id}")
def del_agent(agent_id: int, request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    agent = db.get(Agent, agent_id)
    if not agent: raise HTTPException(404, "代理不存在")
    if db.scalar(select(Calculation).where(Calculation.agent_id == agent_id)):
        raise HTTPException(400, "该代理已有历史计算记录，不能直接删除；建议保留代理以保证历史数据完整")
    db.delete(agent); db.commit()
    return {"ok": True}

@app.post("/api/agents/clear")
def clear_agents(request: Request, agent_ids: list[int] = Form(...), db: Session = Depends(db_dep)):
    require_user(request, db)
    for aid in agent_ids:
        a = db.get(Agent, aid)
        if a: a.total = Decimal("0")
    db.commit()
    return {"ok": True}

@app.post("/api/games")
def add_game(request: Request, name: str = Form(...), formula_choice: int = Form(...),
             db: Session = Depends(db_dep)):
    require_user(request, db)
    name = name.strip()
    if db.scalar(select(Game).where(Game.name == name)): raise HTTPException(400, "游戏已存在")
    if formula_choice not in (1, 2, 3):
        raise HTTPException(400, "公式选择无效")
    g = Game(name=name, factor=Decimal("0.94"), formula1=Decimal("0.50"),
             formula2=Decimal("0.55"), formula3=Decimal("0.45"), formula_choice=formula_choice)
    db.add(g); db.commit()
    return {"ok": True}

@app.delete("/api/games/{game_id}")
def del_game(game_id: int, request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    g = db.get(Game, game_id)
    if not g: raise HTTPException(404, "游戏不存在")
    if db.scalar(select(Calculation).where(Calculation.game_id == game_id)):
        raise HTTPException(400, "该游戏已有历史计算记录，不能直接删除；建议保留游戏以保证历史数据完整")
    db.delete(g); db.commit()
    return {"ok": True}

@app.post("/api/rates")
def add_rate(request: Request, name: str = Form(...), value: str = Form(...), db: Session = Depends(db_dep)):
    require_user(request, db)
    try: v = Decimal(value)
    except Exception: raise HTTPException(400, "汇率必须是数字")
    db.add(Rate(name=name.strip(), value=v)); db.commit()
    return {"ok": True}

@app.delete("/api/rates/{rate_id}")
def del_rate(rate_id: int, request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    r = db.get(Rate, rate_id)
    if not r: raise HTTPException(404, "汇率不存在")
    if db.scalar(select(Calculation).where(Calculation.rate_id == rate_id)):
        raise HTTPException(400, "该汇率已有历史计算记录，不能直接删除；建议保留汇率以保证历史数据完整")
    db.delete(r); db.commit()
    return {"ok": True}

@app.post("/api/calculate")
def calculate(request: Request, agent_id: int = Form(...), game_id: int = Form(...),
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
                       formula_no=formula_no, input_number=n, formula_result=formula_result, result=result))
    db.commit()
    return {"ok": True, "formula_result": float(formula_result), "result": float(result), "total": float(agent.total)}

@app.delete("/api/history/{calc_id}")
def delete_calculation(calc_id: int, request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    c = db.get(Calculation, calc_id)
    if not c:
        raise HTTPException(404, "计算记录不存在")
    agent = db.get(Agent, c.agent_id)
    if agent:
        agent.total = Decimal(agent.total or 0) - Decimal(c.result or 0)
    db.delete(c)
    db.commit()
    return {"ok": True, "total": float(agent.total) if agent else 0}

@app.get("/api/history")
def history(request: Request, db: Session = Depends(db_dep), day: Optional[str] = None):
    require_user(request, db)
    q = select(Calculation, Agent.name, Game.name, Rate.name, Rate.value, User.username).join(Agent, Calculation.agent_id==Agent.id).join(Game, Calculation.game_id==Game.id).join(Rate, Calculation.rate_id==Rate.id).join(User, Calculation.user_id==User.id)
    if day:
        try:
            d = date.fromisoformat(day)
            q = q.where(Calculation.created_at >= datetime.combine(d, datetime.min.time()),
                       Calculation.created_at < datetime.combine(d, datetime.max.time()))
        except ValueError: raise HTTPException(400, "日期格式错误")
    q = q.order_by(Calculation.created_at.desc())
    rows = db.execute(q).all()
    fixed = {1:"0.94×0.5", 2:"0.94×0.55", 3:"0.94×0.45"}
    return [{"id": c.id, "time": c.created_at.strftime("%Y-%m-%d %H:%M:%S"), "agent_id": c.agent_id,
             "agent": an, "game": gn, "rate": rn, "formula": c.formula_no, "input": float(c.input_number),
             "formula_result": float(c.formula_result), "result": float(c.result), "user": un,
             "expression": f"{Decimal(c.input_number):g}×{fixed.get(c.formula_no, '')}÷{Decimal(rv):g}"}
            for c, an, gn, rn, rv, un in rows]

@app.get("/api/export/txt")
def export_txt(request: Request, db: Session = Depends(db_dep)):
    require_user(request, db)
    today = date.today()
    start = datetime.combine(today, datetime.min.time())
    end = datetime.combine(today, datetime.max.time())
    agents = db.scalars(select(Agent).where(Agent.total >= Decimal("50")).order_by(Agent.name)).all()
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
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": "attachment; filename=agent_settlement.txt"})
