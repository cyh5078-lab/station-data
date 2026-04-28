"""
텔레그램 봇 - 역사명/호선별 검색 + 즐겨찾기 + 알림
====================================================
설치:  pip install -r requirements.txt
실행:  python telegram_bot.py
"""

import logging
import os
import sqlite3
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

# ── 환경변수 / 설정 ────────────────────────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN", "여기에_봇_토큰_입력")
GITHUB_API       = "https://api.github.com/repos/cyh5078-lab/station-data/contents/"
BASE_URL         = "https://cyh5078-lab.github.io/station-data/"
VIEWER_URL       = "https://cyh5078-lab.github.io/station-data/viewer.html?url="
DB_PATH          = os.getenv("DB_PATH", "bot_data.db")
CACHE_TTL        = 3600
MONITOR_INTERVAL = 1800
LIST_PAGE        = 20
MAX_FAV          = 10

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── SQLite 초기화 ─────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id   INTEGER,
                station   TEXT,
                line      TEXT,
                added_at  TEXT,
                PRIMARY KEY (user_id, station, line)
            );
            CREATE TABLE IF NOT EXISTS subscribers (
                user_id   INTEGER PRIMARY KEY,
                joined_at TEXT
            );
            CREATE TABLE IF NOT EXISTS file_shas (
                filename  TEXT PRIMARY KEY,
                sha       TEXT
            );
        """)

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()

# ── 호선명 변환 (딱 1개!) ─────────────────────────────────────────

def _line_name(line: str) -> str:
    mapping = {
        "1": "1호선", "2": "2호선", "3": "3호선", "4": "4호선",
        "5": "5호선", "6": "6호선", "7": "7호선", "8": "8호선", "9": "9호선",
        "과선": "과천선", "분선": "분당선", "신선": "신분당선",
        "경선": "경의중앙선", "공선": "공항철도", "우선": "우이신설선",
        "별선": "별내선", "진선": "진접선", "수선": "수인분당선",
        "GTX": "GTX-A", "경강": "경강선", "서해": "서해선",
        "인천1": "인천1호선", "인천2": "인천2호선",
    }
    return mapping.get(line, line + "선")

# ── GitHub 파일 목록 캐시 ─────────────────────────────────────────

_cache: list = []
_cache_time: float = 0.0


def _parse_filename(raw: str):
    if not raw.endswith("_장비현황.html"):
        return None
    stem  = raw[: -len("_장비현황.html")]
    parts = stem.rsplit("_", 1)
    if len(parts) != 2:
        return None
    station, line = parts
    encoded      = urllib.parse.quote(raw, safe="")
    original_url = BASE_URL + encoded
    viewer_url   = VIEWER_URL + urllib.parse.quote(original_url, safe="")
    linename     = _line_name(line)
    return {
        "name"    : station,
        "line"    : line,
        "linename": linename,
        "display" : f"{station} ({linename})",
        "url"     : viewer_url,          # ← wrapper 통해 열기 (자동 가로 회전)
        "file"    : raw,
    }


async def fetch_stations(force=False) -> list:
    global _cache, _cache_time
    if not force and _cache and (time.time() - _cache_time < CACHE_TTL):
        return _cache
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(GITHUB_API)
            r.raise_for_status()
            files = r.json()
        result = []
        for f in files:
            if f.get("type") == "file" and f["name"].endswith(".html"):
                p = _parse_filename(f["name"])
                if p:
                    p["sha"] = f.get("sha", "")
                    result.append(p)
        _cache      = sorted(result, key=lambda x: x["name"])
        _cache_time = time.time()
        log.info(f"역사 목록 갱신: {len(_cache)}개")
    except Exception as e:
        log.error(f"GitHub API 오류: {e}")
    return _cache


def search_stations(stations: list, query: str) -> list:
    """역사명 부분 검색"""
    q = query.strip().replace(" ", "")
    if not q:
        return stations
    return [s for s in stations if q in s["name"].replace(" ", "")]


def search_by_line(stations: list, linename: str) -> list:
    """호선명 검색"""
    q = linename.strip().replace(" ", "")
    return [s for s in stations if q in s["linename"].replace(" ", "")]


def get_unique_lines(stations: list) -> list:
    """중복 없는 호선 목록"""
    seen  = set()
    lines = []
    for s in stations:
        if s["linename"] not in seen:
            seen.add(s["linename"])
            lines.append(s["linename"])
    return sorted(lines)


def station_btn(s: dict) -> list:
    return [InlineKeyboardButton(
        text=f"🗺️ {s['display']}",
        web_app=WebAppInfo(url=s["url"]),
    )]

# ── 메인 키보드 ───────────────────────────────────────────────────

def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔍 역사 검색"), KeyboardButton("🚇 호선별 검색")],
            [KeyboardButton("📋 전체 목록"), KeyboardButton("⭐ 즐겨찾기")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

# ── /start ────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stations = await fetch_stations()
    await update.message.reply_text(
        "🚇 장비현황 검색 봇\n\n"
        f"총 {len(stations)}개 역사 등록\n\n"
        "• 역사명 입력 → 역사 검색\n"
        "• 🚇 호선별 검색 → 호선 선택\n"
        "• /list → 전체 목록\n"
        "• /fav → 즐겨찾기\n"
        "• /subscribe → 변경 알림 구독",
        reply_markup=main_keyboard(),
    )

# ── /list ─────────────────────────────────────────────────────────

async def _send_list(update_or_query, stations, page):
    total_pages = max(1, (len(stations) + LIST_PAGE - 1) // LIST_PAGE)
    page        = max(1, min(page, total_pages))
    chunk       = stations[(page-1)*LIST_PAGE : page*LIST_PAGE]

    buttons = [station_btn(s) for s in chunk]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(f"◀ {page-1}p", callback_data=f"list_{page-1}"))
    nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(f"{page+1}p ▶", callback_data=f"list_{page+1}"))
    buttons.append(nav)

    text = f"📋 전체 역사 목록 [{page}/{total_pages}페이지] — 총 {len(stations)}개"
    kb   = InlineKeyboardMarkup(buttons)

    if hasattr(update_or_query, "message"):
        await update_or_query.message.reply_text(text, reply_markup=kb)
    else:
        await update_or_query.edit_message_text(text, reply_markup=kb)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stations = await fetch_stations()
    page = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
    await _send_list(update, stations, page)


async def cb_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "noop":
        return
    page     = int(q.data.split("_")[1])
    stations = await fetch_stations()
    await _send_list(q, stations, page)

# ── 호선별 검색 ───────────────────────────────────────────────────

async def cmd_lines(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stations = await fetch_stations()
    lines    = get_unique_lines(stations)

    buttons = []
    row = []
    for ln in lines:
        row.append(InlineKeyboardButton(f"🚇 {ln}", callback_data=f"line_{ln}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await update.message.reply_text(
        f"🚇 호선을 선택하세요 (총 {len(lines)}개)",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_line(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q        = update.callback_query
    await q.answer()
    linename = q.data[len("line_"):]
    stations = await fetch_stations()
    matches  = search_by_line(stations, linename)

    if not matches:
        await q.edit_message_text(f"❌ {linename} 역사가 없습니다.")
        return

    buttons = [station_btn(s) for s in matches]
    buttons.append([InlineKeyboardButton("◀ 호선 목록으로", callback_data="back_lines")])

    await q.edit_message_text(
        f"🚇 {linename} 역사 목록 ({len(matches)}개)",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_back_lines(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q        = update.callback_query
    await q.answer()
    stations = await fetch_stations()
    lines    = get_unique_lines(stations)

    buttons = []
    row = []
    for ln in lines:
        row.append(InlineKeyboardButton(f"🚇 {ln}", callback_data=f"line_{ln}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await q.edit_message_text(
        f"🚇 호선을 선택하세요 (총 {len(lines)}개)",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

# ── /fav ─────────────────────────────────────────────────────────

async def cmd_fav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with db() as con:
        rows = con.execute(
            "SELECT station, line FROM favorites WHERE user_id=? ORDER BY added_at",
            (uid,)
        ).fetchall()

    if not rows:
        await update.message.reply_text(
            "⭐ 즐겨찾기가 비어있습니다.\n\n역사 검색 후 ⭐ 버튼으로 추가하세요."
        )
        return

    buttons = []
    for r in rows:
        url = BASE_URL + urllib.parse.quote(
            f"{r['station']}_{r['line']}_장비현황.html", safe=""
        )
        buttons.append([
            InlineKeyboardButton(
                f"🗺️ {r['station']} ({_line_name(r['line'])})",
                web_app=WebAppInfo(url=url)
            ),
            InlineKeyboardButton("🗑️", callback_data=f"delfav_{r['station']}_{r['line']}"),
        ])

    await update.message.reply_text(
        f"⭐ 내 즐겨찾기 ({len(rows)}/{MAX_FAV}개)",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_addfav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    _, station, line = q.data.split("_", 2)

    with db() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM favorites WHERE user_id=?", (uid,)
        ).fetchone()[0]
        if count >= MAX_FAV:
            await q.answer(f"즐겨찾기는 최대 {MAX_FAV}개까지 가능합니다.", show_alert=True)
            return
        con.execute(
            "INSERT OR IGNORE INTO favorites VALUES (?,?,?,?)",
            (uid, station, line, datetime.now().isoformat()),
        )
    await q.answer(f"⭐ {station} 즐겨찾기 추가!")


async def cb_delfav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    _, station, line = q.data.split("_", 2)

    with db() as con:
        con.execute(
            "DELETE FROM favorites WHERE user_id=? AND station=? AND line=?",
            (uid, station, line),
        )
    await q.answer(f"🗑️ {station} 즐겨찾기 삭제")
    await cmd_fav(update, context)

# ── /subscribe /unsubscribe ───────────────────────────────────────

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO subscribers VALUES (?,?)",
            (uid, datetime.now().isoformat()),
        )
    await update.message.reply_text(
        "🔔 장비 변경 알림을 구독했습니다!\n"
        f"GitHub 파일 변경 시 {MONITOR_INTERVAL//60}분 이내에 알려드립니다.\n"
        "취소: /unsubscribe"
    )


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with db() as con:
        con.execute("DELETE FROM subscribers WHERE user_id=?", (uid,))
    await update.message.reply_text("🔕 알림 구독을 취소했습니다.")

# ── 변경 감지 모니터링 ────────────────────────────────────────────

async def monitor_changes(context: ContextTypes.DEFAULT_TYPE):
    log.info("변경 감지 실행 중...")
    stations = await fetch_stations(force=True)

    changed = []
    with db() as con:
        for s in stations:
            row = con.execute(
                "SELECT sha FROM file_shas WHERE filename=?", (s["file"],)
            ).fetchone()
            old_sha = row["sha"] if row else None
            if old_sha is None:
                con.execute(
                    "INSERT OR REPLACE INTO file_shas VALUES (?,?)",
                    (s["file"], s["sha"]),
                )
            elif old_sha != s["sha"]:
                changed.append(s)
                con.execute(
                    "INSERT OR REPLACE INTO file_shas VALUES (?,?)",
                    (s["file"], s["sha"]),
                )

    if not changed:
        log.info("변경된 파일 없음")
        return

    with db() as con:
        subs = con.execute("SELECT user_id FROM subscribers").fetchall()

    for s in changed:
        kb   = InlineKeyboardMarkup([station_btn(s)])
        text = (
            f"🔔 장비현황 업데이트 감지!\n\n"
            f"역사: {s['display']}\n"
            f"시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        for sub in subs:
            try:
                await context.bot.send_message(
                    chat_id=sub["user_id"],
                    text=text,
                    reply_markup=kb,
                )
            except Exception as e:
                log.warning(f"알림 전송 실패 uid={sub['user_id']}: {e}")

# ── 텍스트 검색 ───────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text     = update.message.text.strip()
    stations = await fetch_stations()

    if text == "📋 전체 목록":
        await _send_list(update, stations, 1)
        return
    if text == "⭐ 즐겨찾기":
        await cmd_fav(update, context)
        return
    if text == "🚇 호선별 검색":
        await cmd_lines(update, context)
        return
    if text == "🔍 역사 검색":
        await update.message.reply_text("검색할 역사명을 입력하세요.")
        return

    # 역사명 검색
    matches = search_stations(stations, text)
    # 없으면 호선명으로 검색
    if not matches:
        matches = search_by_line(stations, text)

    if not matches:
        await update.message.reply_text(
            f"❌ '{text}' 역사 또는 호선을 찾을 수 없습니다.\n"
            "🚇 호선별 검색 버튼을 눌러보세요!"
        )
        return

    def make_buttons(s):
        return [
            InlineKeyboardButton(f"🗺️ {s['display']}", web_app=WebAppInfo(url=s["url"])),
            InlineKeyboardButton("⭐", callback_data=f"addfav_{s['name']}_{s['line']}"),
        ]

    if len(matches) == 1:
        s  = matches[0]
        kb = InlineKeyboardMarkup([make_buttons(s)])
        await update.message.reply_text(f"✅ {s['display']}", reply_markup=kb)
    else:
        buttons = [make_buttons(s) for s in matches[:10]]
        kb = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(
            f"🔍 '{text}' 검색 결과: {len(matches)}개",
            reply_markup=kb,
        )

# ── 인라인 모드 ───────────────────────────────────────────────────

async def handle_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.inline_query.query
    stations = await fetch_stations()
    matches  = search_stations(stations, query)[:20]

    results = []
    for s in matches:
        kb = InlineKeyboardMarkup([station_btn(s)])
        results.append(InlineQueryResultArticle(
            id=s["url"],
            title=s["display"],
            description="🗺️ 장비현황 열기",
            input_message_content=InputTextMessageContent(
                message_text=f"🗺️ {s['display']} 장비현황"
            ),
            reply_markup=kb,
        ))

    await update.inline_query.answer(results, cache_time=30)

# ── 메인 ─────────────────────────────────────────────────────────

def main():
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("list",        cmd_list))
    app.add_handler(CommandHandler("lines",       cmd_lines))
    app.add_handler(CommandHandler("fav",         cmd_fav))
    app.add_handler(CommandHandler("subscribe",   cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))

    app.add_handler(CallbackQueryHandler(cb_list,       pattern=r"^list_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_line,       pattern=r"^line_"))
    app.add_handler(CallbackQueryHandler(cb_back_lines, pattern=r"^back_lines$"))
    app.add_handler(CallbackQueryHandler(cb_addfav,     pattern=r"^addfav_"))
    app.add_handler(CallbackQueryHandler(cb_delfav,     pattern=r"^delfav_"))
    app.add_handler(CallbackQueryHandler(lambda u,c: u.callback_query.answer(), pattern="^noop$"))

    app.add_handler(InlineQueryHandler(handle_inline))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_repeating(monitor_changes, interval=MONITOR_INTERVAL, first=60)

    log.info("봇 시작!")
    app.run_polling()


if __name__ == "__main__":
    main()
