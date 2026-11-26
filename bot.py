# -*- coding: utf-8 -*-
"""بوت الطالب الذكي - smart student bot backed by Gemini, with admin panel."""
import os, io, re, json, base64, time, logging, asyncio, sqlite3, threading, hmac, hashlib
from urllib.parse import parse_qsl

import httpx
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M, InputFile, WebAppInfo
from telegram.constants import ChatAction
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, ContextTypes, filters)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("studentbot")

TOKEN = os.environ["BOT_TOKEN"]
GKEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GURL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(",", " ").split() if x.strip().isdigit()}
ADMIN_USERNAMES = {x.strip().lstrip("@").lower() for x in os.environ.get("ADMIN_USERNAMES", "").replace(",", " ").split() if x.strip()}
ADMIN_BACKUP_CHAT = int(os.environ.get("ADMIN_BACKUP_CHAT", next(iter(ADMIN_IDS), 0)))

FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Amiri-Regular.ttf")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "student.db")

BASE_SYS = (
    "أنت «الطالب الذكي»: مساعد تعليمي للطلاب. أجب باللغة العربية الواضحة والبسيطة "
    "ما لم يطلب الطالب لغة أخرى صراحة. نظّم إجابتك بعناوين قصيرة ونقاط عندما يكون ذلك مفيداً، "
    "وتجنّب الحشو. إذا كان السؤال خارج الدراسة أجب عنه باختصار مفيد."
)

DEFAULT_WELCOME = "🎓 أهلاً بك في بوت الطالب الذكي!\n\nاختر ماذا تريد من القائمة، أو أرسل سؤالك مباشرة وسأجيبك:"
DEFAULT_GATE_MSG = "🔐 لاستخدام البوت اشترك أولاً في القناة ثم اضغط تحقق."
DEFAULT_MAINT_MSG = "🛠 البوت في وضع الصيانة حالياً، عد لاحقاً."

# ---------------- store ----------------

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.q("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, name TEXT, username TEXT,
            joined REAL, last_seen REAL, banned INTEGER DEFAULT 0)""")
        self.q("""CREATE TABLE IF NOT EXISTS events(
            ts REAL, user_id INTEGER, kind TEXT)""")
        self.q("""CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)""")
        self.q("""CREATE TABLE IF NOT EXISTS h_stages(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)""")
        self.q("""CREATE TABLE IF NOT EXISTS h_subjects(id INTEGER PRIMARY KEY AUTOINCREMENT, stage_id INTEGER, name TEXT)""")
        self.q("""CREATE TABLE IF NOT EXISTS h_files(id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER,
            title TEXT, file_id TEXT, kind TEXT, downloads INTEGER DEFAULT 0, added REAL)""")
        self.q("""CREATE TABLE IF NOT EXISTS searches(ts REAL, user_id INTEGER, q TEXT)""")
        for col in ("teacher TEXT", "file_uid TEXT", "year TEXT", "quality TEXT", "fname TEXT", "fsize INTEGER", "ftype TEXT", "stamped INTEGER DEFAULT 0", "orig_fid TEXT"):
            try:
                self.q("ALTER TABLE h_files ADD COLUMN " + col)
            except sqlite3.OperationalError:
                pass

    def q(self, sql, args=(), fetch=None):
        with self.lock:
            cur = self.db.execute(sql, args)
            rows = cur.fetchall() if fetch else None
            self.db.commit()
            return rows

    def get(self, key, default=""):
        r = self.q("SELECT value FROM settings WHERE key=?", (key,), fetch=True)
        return r[0][0] if r else default

    def set(self, key, value):
        self.q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        backup_soon()

    def on(self, key):
        return self.get(key, "0") == "1"

    def upsert_user(self, u):
        r = self.q("SELECT id FROM users WHERE id=?", (u.id,), fetch=True)
        now = time.time()
        if r:
            self.q("UPDATE users SET name=?, username=?, last_seen=? WHERE id=?",
                   (u.full_name or "", u.username or "", now, u.id))
            return False
        self.q("INSERT INTO users(id,name,username,joined,last_seen) VALUES(?,?,?,?,?)",
               (u.id, u.full_name or "", u.username or "", now, now))
        backup_soon()
        return True

    def event(self, user_id, kind):
        self.q("INSERT INTO events(ts,user_id,kind) VALUES(?,?,?)", (time.time(), user_id, kind))
        backup_soon()

    def is_banned(self, uid):
        r = self.q("SELECT banned FROM users WHERE id=?", (uid,), fetch=True)
        return bool(r and r[0][0])

    def set_ban(self, uid, flag):
        self.q("UPDATE users SET banned=? WHERE id=?", (1 if flag else 0, uid))
        backup_soon()

    def stats(self):
        day = time.time() - 86400
        total = self.q("SELECT COUNT(*) FROM users", fetch=True)[0][0]
        new_today = self.q("SELECT COUNT(*) FROM users WHERE joined>?", (day,), fetch=True)[0][0]
        active = self.q("SELECT COUNT(*) FROM users WHERE last_seen>?", (day,), fetch=True)[0][0]
        banned = self.q("SELECT COUNT(*) FROM users WHERE banned=1", fetch=True)[0][0]
        feats = self.q("SELECT kind, COUNT(*) FROM events GROUP BY kind ORDER BY 2 DESC", fetch=True)
        feats_day = self.q("SELECT kind, COUNT(*) FROM events WHERE ts>? GROUP BY kind ORDER BY 2 DESC", (day,), fetch=True)
        return total, new_today, active, banned, feats, feats_day

    def recent_users(self, limit=10):
        return self.q("SELECT id,name,username,last_seen FROM users WHERE banned=0 ORDER BY last_seen DESC LIMIT ?", (limit,), fetch=True)

    def banned_users(self):
        return self.q("SELECT id,name,username FROM users WHERE banned=1 ORDER BY last_seen DESC LIMIT 30", fetch=True)

    def all_user_ids(self):
        return [r[0] for r in self.q("SELECT id FROM users WHERE banned=0", fetch=True)]

store = None

# ---------------- backup (pinned doc in admin chat) ----------------

_backup_dirty = False
_last_backup = 0.0
BACKUP_EVERY = 300   # silent; keeps settings/library across restarts (Render restarts lose anything newer than the last backup)
APP = None

def backup_soon():
    # يعلّم أن هناك تغييرات؛ النسخ الفعلي يتم كل 48 ساعة فقط
    global _backup_dirty
    _backup_dirty = True

async def backup_loop():
    while True:
        await asyncio.sleep(120)
        try:
            if _backup_dirty and time.time() - _last_backup >= BACKUP_EVERY:
                await do_backup()
        except Exception:
            log.exception("backup tick")

async def do_backup():
    global _backup_dirty, _last_backup
    if not APP or not ADMIN_BACKUP_CHAT or not store:
        return
    try:
        _backup_dirty = False
        store.db.commit()
        old_id = None
        try:
            ch = await APP.bot.get_chat(ADMIN_BACKUP_CHAT)
            if ch.pinned_message and ch.pinned_message.document:
                old_id = ch.pinned_message.message_id
        except Exception:
            pass
        with open(DB_PATH, "rb") as f:
            msg = await APP.bot.send_document(
                ADMIN_BACKUP_CHAT, InputFile(f, filename="student.db"),
                caption="💾 نسخة احتياطية من بيانات البوت", disable_notification=True)
        _last_backup = time.time()
        try:
            await APP.bot.pin_chat_message(ADMIN_BACKUP_CHAT, msg.message_id, disable_notification=True)
        except Exception:
            log.exception("pin failed")
        if old_id and old_id != msg.message_id:
            try:
                await APP.bot.delete_message(ADMIN_BACKUP_CHAT, old_id)
            except Exception:
                pass
    except Exception:
        _backup_dirty = True
        log.exception("backup failed")

async def drop_pin_notice(update, ctx):
    # يحذف رسالة "تم تثبيت رسالة" الخدمية
    try:
        await update.effective_message.delete()
    except Exception:
        pass

async def restore_backup(app):
    global _last_backup
    if not ADMIN_BACKUP_CHAT:
        return False
    try:
        chat = await app.bot.get_chat(ADMIN_BACKUP_CHAT)
        pin = chat.pinned_message
        if pin and pin.document and pin.document.file_name == "student.db":
            f = await app.bot.get_file(pin.document.file_id)
            out = io.BytesIO()
            await f.download_to_memory(out=out)
            with open(DB_PATH, "wb") as fh:
                fh.write(out.getvalue())
            try:
                _last_backup = pin.date.timestamp()
            except Exception:
                _last_backup = time.time()
            log.info("backup restored, %d bytes", len(out.getvalue()))
            return True
    except Exception:
        log.exception("restore failed")
    return False

SOLVE_SYS = (
    "أنت مدرّس خبير في المنهج الدراسي العراقي (المتوسط والإعدادي) لمواد الرياضيات والفيزياء والكيمياء والأحياء وغيرها. "
    "حلّ المسائل باللغة العربية بأسلوب كتب ومدرسي العراق: اكتب المعطيات، ثم المطلوب، ثم القانون المستخدم، ثم التعويض، ثم الحل خطوة بخطوة مع سبب كل خطوة، ثم الجواب النهائي. "
    "استخدم المصطلحات والرموز المعتمدة في الكتب العراقية. تحقق من الجواب قبل كتابته وانتبه للوحدات. "
    "إذا كانت الصورة غير واضحة أو ناقصة فاذكر ذلك واطلب إعادة تصويرها ولا تخمّن. "
    "التنسيق: الرسالة تظهر في تيليجرام كنص عادي، لذلك لا تستخدم LaTeX أبداً ولا الرموز $ ولا \\frac ولا markdown مثل ** أو ##. "
    "اكتب المعادلات بسطر مستقل وبرموز يونيكود مقروءة: x² ، √ ، ÷ ، × ، ½ ، π ، Δ ، θ ، ≈ ، → ، ⇌ ، وأرقام سفلية للكيمياء مثل H₂O و CO₂ و x₁. "
    "اجعل كل خطوة في سطر أو سطرين قصيرين وضع قبلها رقمها مثل: ١) ٢) ٣) أو 1) 2) 3). "
    "اختم دائماً بسطر: ✅ الجواب النهائي: ..."
)

# ---------------- helpers ----------------

def is_admin(update: Update):
    u = update.effective_user
    if not u:
        return False
    return u.id in ADMIN_IDS or (u.username or "").lower() in ADMIN_USERNAMES

def tick(on):
    return "✅" if on else "❌"

FALLBACKS = [m for m in os.environ.get("GEMINI_FALLBACKS", "gemini-3.5-flash-lite,gemini-flash-lite-latest,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-flash-latest").split(",") if m.strip()]

async def gemini(parts, system=BASE_SYS, temperature=0.5, max_tokens=8192, fast=False):
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}
    models = [MODEL] + [m.strip() for m in FALLBACKS if m.strip() != MODEL]
    if fast:
        models = models[:3]
    r = None
    async with httpx.AsyncClient(timeout=20 if fast else 180) as c:
        for attempt, m in enumerate(models if fast else models + models):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
            try:
                r = await c.post(url, params={"key": GKEY}, json=body)
            except httpx.HTTPError as e:
                log.warning("gemini network %s: %s", m, e)
                r = None
                await asyncio.sleep(2)
                continue
            if r.status_code == 200:
                break
            log.warning("gemini %s (%s): %s", r.status_code, m, r.text[:200])
            if r.status_code in (429, 500, 503, 404):
                await asyncio.sleep(1.5)
                continue
            break
    if r is None or r.status_code != 200:
        if r is not None and r.status_code in (429, 503):
            raise RuntimeError("الخدمة مزدحمة حالياً، حاول بعد دقيقة.")
        raise RuntimeError("تعذر الاتصال بالذكاء الاصطناعي، حاول لاحقاً.")
    data = r.json()
    try:
        parts_out = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts_out if not p.get("thought"))
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("لم يصلني رد صالح، حاول صياغة الطلب بشكل أوضح.")
    text = text.strip()
    if not text:
        raise RuntimeError("لم أستطع معالجة هذا المحتوى، جرّب محتوى آخر.")
    return text

def img_part(data, mime="image/jpeg"):
    return {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}

async def send_long(msg, text):
    limit = 4000
    while text:
        chunk = text[:limit]
        if len(text) > limit:
            cut = chunk.rfind("\n")
            if cut > 1500:
                chunk = chunk[:cut]
        await msg.reply_text(chunk)
        text = text[len(chunk):].lstrip("\n")

def ago(ts):
    d = int(time.time() - ts)
    if d < 60:
        return "الآن"
    if d < 3600:
        return f"قبل {d//60} د"
    if d < 86400:
        return f"قبل {d//3600} س"
    return f"قبل {d//86400} يوم"

# ---------------- user menu ----------------

# ---- one shared registry: add a tool here and it appears in the bot menu AND the Mini App ----
# input: text = text only, both = text or file (image/PDF/txt), audio = voice file
TOOLS = [
    dict(id="explain",   icon="📷", name="اشرح صورة",           input="both",  group="main"),
    dict(id="summary",   icon="📝", name="أنشئ ملخص",           input="both",  group="main"),
    dict(id="questions", icon="📚", name="أنشئ أسئلة",          input="both",  group="main"),
    dict(id="quiz",      icon="🧠", name="اختبرني",             input="text",  group="main", kind="quiz"),
    dict(id="schedule",  icon="📅", name="جدول دراستي",         input="text",  group="main"),
    dict(id="voice",     icon="🎤", name="حوّل صوتاً إلى نص",   input="audio", group="main"),
    dict(id="pdf",       icon="📄", name="أنشئ PDF",            input="both",  group="main"),
    dict(id="solve",     icon="🧮", name="حل المسائل",          input="both",  group="main"),
    dict(id="simple",    icon="🧒", name="اشرحها ببساطة",       input="both",  group="more"),
    dict(id="cards",     icon="🃏", name="بطاقات حفظ",          input="both",  group="more"),
    dict(id="check",     icon="✅", name="صحّح إجابتي",         input="both",  group="more"),
    dict(id="translate", icon="🌐", name="ترجمة",               input="both",  group="more"),
    dict(id="formulas",  icon="📐", name="ورقة قوانين",         input="text",  group="more"),
]

def tool_buttons(group):
    bs = [B(f"{t['icon']} {t['name']}", callback_data=f"m:{t['id']}") for t in TOOLS if t["group"] == group]
    return [bs[i:i + 2] for i in range(0, len(bs), 2)]

def menu_markup(adm=False):
    rows = ([[B("🎛 لوحة الأدمن", callback_data="a:main")]] if adm else [])
    base = os.environ.get("RENDER_EXTERNAL_URL")
    if base:
        rows = [[B("🌐 افتح التطبيق", web_app=WebAppInfo(url=base.rstrip("/") + "/app"))]] + rows
    return M(tool_buttons("main") + [
        [B("📚 الملازم", callback_data="h:s")],
        [B("✨ أدوات إضافية", callback_data="m:more")],
    ] + rows)

def more_markup():
    return M(tool_buttons("more") + [
        [B("⏱ مؤقت مذاكرة", callback_data="m:timer")],
        [B("🔙 القائمة الرئيسية", callback_data="m:menu")],
    ])

def timer_markup():
    return M([
        [B("🍅 25 دقيقة تركيز", callback_data="m:t25"), B("📚 45 دقيقة", callback_data="m:t45")],
        [B("🔙 رجوع", callback_data="m:more")],
    ])

MODE_PROMPTS = {
    "explain":   "📷 أرسل الصورة الآن (صفحة من كتاب، لوحة، تمرين...) وسأشرحها لك.",
    "summary":   "📝 أرسل النص أو صورة الصفحات أو ملف PDF، وسأصنع لك ملخصاً منظماً.",
    "questions": "📚 أرسل المادة (نصاً أو صورة أو PDF)، وسأنشئ لك أسئلة عليها مع الأجوبة النموذجية.",
    "quiz":      "🧠 عن ماذا تريد أن أختبرك؟ أرسل اسم المادة أو الموضوع (مثال: الفيزياء - الحرارة).",
    "schedule":  "📅 أرسل في رسالة واحدة: المواد التي تدرسها، وتواريخ الامتحانات إن وجدت، وعدد الساعات المتاحة يومياً. وسأرتب لك جدولاً.",
    "voice":     "🎤 أرسل الرسالة الصوتية أو المقطع الصوتي الآن، وسأحوّله إلى نص.",
    "pdf":       "📄 أرسل المحتوى (نصاً أو صورة)، وسأحوّله إلى ورقة مراجعة منظمة بصيغة PDF.",
    "simple":    "🧒 أرسل الدرس أو الفقرة أو السؤال (نصاً أو صورة)، وسأشرحه لك كأنك بعمر 10 سنوات مع أمثلة من الحياة.",
    "cards":     "🃏 أرسل المادة (نصاً أو صورة أو PDF) أو اسم الدرس، وسأصنع لك بطاقات حفظ (سؤال وجواب) مع حيل تساعدك على التذكر.",
    "check":     "✅ أرسل السؤال ثم إجابتك في رسالة واحدة (نصاً أو صورة)، وسأصححها وأعطيك درجة وملاحظات لتحسينها.",
    "translate": "🌐 أرسل النص (أو صورة فيها نص) وسأترجمه: من العربية إلى الإنجليزية، ومن أي لغة أخرى إلى العربية، مع شرح المصطلحات الصعبة.",
    "formulas":  "📐 اكتب المادة والموضوع أو الفصل (مثال: الفيزياء - الحركة)، وسأجهز لك ورقة بكل القوانين المهمة مع معنى كل رمز.",
    "solve":     "🧮 أرسل المسألة نصاً أو صورة (رياضيات، فيزياء، كيمياء، أحياء، أو أي مادة)، وسأحلها لك خطوة بخطوة بأسلوب المنهج العراقي.\n\nيمكنك إضافة تعليق مع الصورة مثل: حل السؤال 3 فقط.",
}

def back_markup():
    return M([[B("🔙 القائمة الرئيسية", callback_data="m:menu")]])

# ---------------- gate ----------------

async def gate_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if is_admin(update):
        return True
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if store.is_banned(u.id):
        if msg:
            await msg.reply_text("🚫 تم حظرك من استخدام البوت.")
        return False
    if store.on("maintenance"):
        if msg:
            await msg.reply_text(store.get("maintenance_msg", DEFAULT_MAINT_MSG) or DEFAULT_MAINT_MSG)
        return False
    if store.on("gate_on"):
        ch = store.get("gate_channel", "")
        if ch:
            try:
                cm = await ctx.bot.get_chat_member(ch, u.id)
                if cm.status in ("member", "administrator", "creator"):
                    return True
            except Exception as e:
                log.warning("gate check failed: %s", e)
                return True
            kb = M([[B("📢 اشترك في القناة", url=f"https://t.me/{ch.lstrip('@')}")],
                    [B("✅ تحقق", callback_data="g:verify")]])
            if msg:
                await msg.reply_text(store.get("gate_msg", DEFAULT_GATE_MSG) or DEFAULT_GATE_MSG, reply_markup=kb)
            return False
    return True

async def verify_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    ch = store.get("gate_channel", "")
    try:
        cm = await ctx.bot.get_chat_member(ch, q.from_user.id)
        ok = cm.status in ("member", "administrator", "creator")
    except Exception:
        ok = False
    if ok:
        await q.answer("تم التحقق، أهلاً بك!")
        await q.message.reply_text(store.get("welcome", DEFAULT_WELCOME), reply_markup=menu_markup(is_admin(update)))
    else:
        await q.answer("لم تشترك بعد. اشترك ثم اضغط تحقق.", show_alert=True)

# ---------------- user flows ----------------

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    is_new = store.upsert_user(u)
    if is_new:
        store.event(u.id, "join")
        if store.on("join_notice"):
            for aid in ADMIN_IDS:
                try:
                    await ctx.bot.send_message(aid, f"🔔 مستخدم جديد: {u.full_name} (@{u.username or '—'}) id={u.id}")
                except Exception:
                    pass
    if not await gate_ok(update, ctx):
        return
    ctx.user_data.clear()
    await update.message.reply_text(store.get("welcome", DEFAULT_WELCOME), reply_markup=menu_markup(is_admin(update)))

async def run_timer(bot, chat_id, mins, rest):
    try:
        await asyncio.sleep(mins * 60)
        await bot.send_message(chat_id, f"⏰ انتهت جلسة التركيز ({mins} دقيقة)! أحسنت 👏\nخذ استراحة {rest} دقائق: اشرب ماء وتحرّك قليلاً.")
        await asyncio.sleep(rest * 60)
        await bot.send_message(chat_id, "🔔 انتهت الاستراحة. هل نبدأ جلسة جديدة؟", reply_markup=timer_markup())
    except Exception:
        log.warning("timer failed")

async def menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await gate_ok(update, ctx):
        return
    data = q.data
    if data == "m:menu":
        ctx.user_data.clear()
        await q.message.reply_text(store.get("welcome", DEFAULT_WELCOME), reply_markup=menu_markup(is_admin(update)))
        return
    if data == "m:more":
        ctx.user_data.clear()
        await q.message.reply_text("✨ أدوات إضافية للطالب - اختر:", reply_markup=more_markup())
        return
    if data == "m:timer":
        ctx.user_data.clear()
        await q.message.reply_text("⏱ مؤقت المذاكرة: اختر مدة التركيز وسأنبهك عند انتهائها وعند انتهاء الاستراحة.", reply_markup=timer_markup())
        return
    if data in ("m:t25", "m:t45"):
        mins = 25 if data == "m:t25" else 45
        rest = 5 if mins == 25 else 10
        store.event(q.from_user.id, "timer")
        await q.message.reply_text(f"⏱ بدأ التركيز لمدة {mins} دقيقة. ضع الهاتف جانباً وركّز، وسأنبهك عند الانتهاء 💪", reply_markup=back_markup())
        asyncio.create_task(run_timer(ctx.bot, q.message.chat_id, mins, rest))
        return
    mode = data.split(":", 1)[1]
    ctx.user_data.clear()
    ctx.user_data["mode"] = mode
    await q.message.reply_text(MODE_PROMPTS[mode], reply_markup=back_markup())

# ---------------- quiz ----------------

async def make_quiz(topic):
    """Shared by bot and Mini App. Returns a list of questions or None."""
    raw = await gemini(
        [{"text": (
            f"أنشئ 5 أسئلة اختيار من متعدد لاختبار طالب في: «{topic}». "
            "اجعل الأسئلة متدرجة الصعوبة. أعد JSON فقط دون أي نص آخر بهذا الشكل بالضبط:\n"
            '[{"q":"نص السؤال","options":["خيار1","خيار2","خيار3","خيار4"],"answer":0,"why":"شرح قصير للإجابة"}]'
            "\nanswer هو رقم الخيار الصحيح من 0 إلى 3."
        )}],
        temperature=0.6)
    m = re.search(r"\[.*\]", raw, re.S)
    qs = None
    if m:
        try:
            qs = json.loads(m.group(0))
        except Exception:
            qs = None
    if not qs or not isinstance(qs, list) or not all(isinstance(x, dict) and "q" in x and "options" in x for x in qs):
        return None
    qs = [x for x in qs if isinstance(x.get("options"), list) and len(x["options"]) >= 2 and isinstance(x.get("answer"), int)][:8]
    return qs or None

async def start_quiz(msg, ctx, topic, uid):
    await msg.chat.send_action(ChatAction.TYPING)
    qs = await make_quiz(topic)
    store.event(uid, "quiz")
    if not qs:
        ctx.user_data.clear()
        await msg.reply_text("تعذر إنشاء الاختبار لهذا الموضوع، جرّب موضوعاً آخر.", reply_markup=back_markup())
        return
    ctx.user_data["quiz"] = {"qs": qs, "i": 0, "score": 0, "topic": topic}
    await msg.reply_text(f"🧠 اختبار: {topic}\nعدد الأسئلة: {len(qs)}. بالتوفيق!")
    await ask_quiz(msg, ctx)

async def ask_quiz(msg, ctx):
    quiz = ctx.user_data.get("quiz")
    if not quiz:
        return
    i = quiz["i"]
    q = quiz["qs"][i]
    letters = ["أ", "ب", "ج", "د", "هـ", "و"]
    buttons = [[B(f"{letters[n]}) {str(opt)[:40]}", callback_data=f"qz:{n}")] for n, opt in enumerate(q["options"][:6])]
    await msg.reply_text(f"س{i+1}) {q['q']}", reply_markup=M(buttons))

async def quiz_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    quiz = ctx.user_data.get("quiz")
    if not quiz:
        await q.answer("انتهى هذا الاختبار. ابدأ اختباراً جديداً من القائمة.")
        return
    await q.answer()
    i = quiz["i"]
    cur = quiz["qs"][i]
    pick = int(q.data.split(":")[1])
    right = int(cur.get("answer", 0))
    ok = pick == right
    if ok:
        quiz["score"] += 1
    letters = ["أ", "ب", "ج", "د", "هـ", "و"]
    why = cur.get("why", "")
    verdict = "✅ صحيح!" if ok else f"❌ خطأ. الصحيح: {letters[right] if right < len(letters) else right}) {cur['options'][right] if right < len(cur['options']) else ''}"
    try:
        await q.message.edit_text(f"س{i+1}) {cur['q']}\n\n{verdict}" + (f"\n💡 {why}" if why else ""))
    except Exception:
        pass
    quiz["i"] += 1
    if quiz["i"] >= len(quiz["qs"]):
        score, total = quiz["score"], len(quiz["qs"])
        pct = round(100 * score / total)
        note = "ممتاز! 🌟" if pct >= 80 else ("جيد، واصل 💪" if pct >= 50 else "راجع الدرس وحاول مجدداً 📖")
        ctx.user_data.clear()
        await q.message.reply_text(f"🏁 انتهى الاختبار!\nنتيجتك: {score} من {total} ({pct}%)\n{note}", reply_markup=back_markup())
    else:
        await ask_quiz(q.message, ctx)

# ---------------- pdf ----------------

def build_pdf(title, body_text):
    import arabic_reshaper
    from bidi.algorithm import get_display
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    pdfmetrics.registerFont(TTFont("Amiri", FONT_PATH))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    margin = 55

    def shape(t):
        return get_display(arabic_reshaper.reshape(t))

    def wrap(text, size, maxw):
        words, lines, cur = text.split(), [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if pdfmetrics.stringWidth(shape(trial), "Amiri", size) <= maxw:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines

    y = H - margin

    def new_page():
        nonlocal y
        c.showPage()
        c.setFont("Amiri", 12)
        y = H - margin

    c.setFont("Amiri", 20)
    for ln in wrap(title, 20, W - 2 * margin):
        c.drawRightString(W - margin, y, shape(ln))
        y -= 30
        if y < margin:
            new_page()
    y -= 10
    c.setFont("Amiri", 13)

    for raw in body_text.split("\n"):
        line = raw.strip()
        if not line:
            y -= 8
            if y < margin:
                new_page()
            continue
        heading = line.startswith("#")
        line = re.sub(r"^#+\s*", "", line)
        line = line.replace("**", "").replace("__", "")
        size = 15 if heading else 13
        c.setFont("Amiri", size)
        for ln in wrap(line, size, W - 2 * margin):
            if y < margin:
                new_page()
                c.setFont("Amiri", size)
            c.drawRightString(W - margin, y, shape(ln))
            y -= 22 if heading else 19
        if heading:
            y -= 4
    c.save()
    buf.seek(0)
    return buf

# ---------------- input helpers ----------------

async def fetch_file(ctx, file_id):
    f = await ctx.bot.get_file(file_id)
    out = io.BytesIO()
    await f.download_to_memory(out=out)
    return out.getvalue()

def blob_parts(data, mime, fname=""):
    """Shared by bot and Mini App: turn an uploaded file into Gemini parts. Returns (parts, error)."""
    mime = (mime or "").lower()
    fname = (fname or "").lower()
    if mime.startswith("image/"):
        return [img_part(data, mime)], None
    if mime.startswith("audio/") or mime.startswith("video/") or fname.endswith((".ogg", ".mp3", ".m4a", ".wav", ".oga", ".opus")):
        mm = mime if mime.startswith(("audio/", "video/")) else "audio/ogg"
        return [{"inline_data": {"mime_type": mm, "data": base64.b64encode(data).decode()}}], None
    if mime == "application/pdf" or fname.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:40])
        except Exception:
            text = ""
        text = text.strip()[:30000]
        if not text:
            return None, "لم أستطع استخراج نص من هذا الملف (قد يكون صوراً ممسوحة). أرسل صور الصفحات بدلاً منه."
        return [{"text": "محتوى ملف PDF التالي:\n" + text}], None
    if mime.startswith("text/") or fname.endswith(".txt"):
        return [{"text": data.decode("utf-8", "ignore")[:30000]}], None
    return None, "هذا النوع من الملفات غير مدعوم. أرسل نصاً أو صورة أو PDF."

async def content_parts(update, ctx):
    msg = update.message
    parts = []
    if msg.photo:
        data = await fetch_file(ctx, msg.photo[-1].file_id)
        parts.append(img_part(data, "image/jpeg"))
    if msg.voice or msg.audio:
        data = await fetch_file(ctx, (msg.voice or msg.audio).file_id)
        parts.append({"inline_data": {"mime_type": "audio/ogg", "data": base64.b64encode(data).decode()}})
    if msg.document:
        doc = msg.document
        data = await fetch_file(ctx, doc.file_id)
        p, err = blob_parts(data, doc.mime_type, doc.file_name)
        if err:
            return None, err
        parts += p
    cap = (msg.text or msg.caption or "").strip()
    if cap:
        parts.append({"text": cap})
    if not parts:
        return None, None
    return parts, None

MODE_ASKS = {
    "explain":   "اشرح محتوى هذه الصورة شرحاً تعليمياً واضحاً ومفصلاً، وحدد الأفكار الرئيسية واشرح المصطلحات الصعبة.",
    "summary":   "لخّص المحتوى التالي ملخصاً منظماً: الفكرة الرئيسية ثم أهم النقاط على شكل نقاط، ثم خلاصة قصيرة.",
    "questions": "أنشئ 10 أسئلة متنوعة (اختيار من متعدد وصح/خطأ وأسئلة مقالية قصيرة) على المحتوى التالي، ثم ضع الأجوبة النموذجية في قسم منفصل في الأسفل.",
    "simple":    "اشرح المحتوى التالي بأبسط طريقة ممكنة كأنك تشرح لطفل عمره 10 سنوات: جمل قصيرة، تشبيهات وأمثلة من الحياة اليومية، ثم لخّص الفكرة في سطرين. حافظ على صحة المعلومات والمصطلحات الأساسية.",
    "cards":     "أنشئ من المحتوى أو الموضوع التالي 12 بطاقة حفظ. شكل كل بطاقة: 🃏 رقم) سؤال قصير  ثم  ✅ الجواب. بعدها أضف قسماً بعنوان «حيل للحفظ» فيه 3 طرق ذكية للتذكر (اختصار أحرف أو قصة قصيرة أو ربط بصورة ذهنية) خاصة بهذا الدرس.",
    "check":     "الطالب أرسل سؤالاً وإجابته. صحّح الإجابة كمدرّس عراقي: أعطِ درجة من 10، ثم ✅ ما أصاب فيه، ثم ❌ الأخطاء أو النقص مع التصحيح، ثم اكتب الإجابة النموذجية المختصرة، ثم نصيحة واحدة للتحسين. إن لم تجد السؤال أو الإجابة واضحين فاطلب توضيحاً.",
    "translate": "ترجم النص التالي: إذا كان بالعربية فترجمه إلى الإنجليزية، وإن كان بأي لغة أخرى فترجمه إلى العربية الفصحى الواضحة. ضع الترجمة أولاً، ثم أضف قسماً قصيراً «مفردات مهمة» يشرح أصعب 3 إلى 5 كلمات أو مصطلحات. لا تضف شيئاً آخر.",
    "formulas":  "أنشئ ورقة قوانين منظمة للموضوع التالي وفق المنهج الدراسي العراقي: لكل قانون اكتب اسمه، ثم القانون بسطر مستقل بالرموز العادية (بدون LaTeX)، ثم معنى كل رمز ووحدته، ثم مثال رقمي صغير جداً. رتّبها من الأهم إلى الأقل أهمية.",
    "solve":     "حل المسألة أو المسائل الموجودة في المحتوى التالي (نص أو صورة) وفق تعليمات الحل. إذا وُجدت عدة أسئلة فاحلها كلها بالترتيب، وإذا طلب الطالب سؤالاً محدداً فاحله فقط.",
    "pdf":       "حوّل المحتوى التالي إلى ورقة مراجعة دراسية منظمة بعناوين ونقاط وتعريفات، مناسبة للطباعة والمراجعة قبل الامتحان. استخدم # للعناوين الرئيسية.",
    "schedule":  "بناءً على معلومات الطالب التالية، أنشئ جدول مذاكرة أسبوعياً واقعياً: وزّع المواد على أيام الأسبوع مع أوقات تقريبية وفترات راحة، ورتّب الأولويات حسب قرب الامتحان وصعوبة المادة، وأضف نصائح قصيرة للالتزام بالجدول.",
    "voice":     "انسخ هذا التسجيل الصوتي إلى نص مكتوب بدقة، باللغة التي يتحدث بها. اكتب النص فقط دون تعليق.",
}

async def run_ai(mode, parts):
    """Shared AI call for a tool mode (bot and Mini App)."""
    ask = MODE_ASKS.get(mode)
    if mode == "solve":
        return await gemini(parts + [{"text": ask}], system=SOLVE_SYS, temperature=0.2, max_tokens=16384)
    return await gemini(parts + ([{"text": ask}] if ask else []))

FEAT_NAMES = {
    "join": "انضمام", "chat": "سؤال مباشر", "explain": "شرح صورة", "summary": "ملخص",
    "questions": "أسئلة", "quiz": "اختبار", "schedule": "جدول", "voice": "صوت إلى نص",
    "pdf": "PDF", "solve": "حل المسائل", "simple": "شرح مبسّط", "cards": "بطاقات حفظ",
    "check": "تصحيح إجابة", "translate": "ترجمة", "formulas": "ورقة قوانين", "timer": "مؤقت", "handout": "تنزيل ملزمة", "webapp": "فتح التطبيق",
}

# ---------------- message flow ----------------

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    u = update.effective_user
    if W["uid"] and u and u.id == W["uid"]:
        d = msg.document
        fwd = getattr(msg, "forward_origin", None)
        if d and fwd and getattr(fwd, "type", "") == "channel" and ((d.mime_type or "") == "application/pdf" or (d.file_name or "").lower().endswith(".pdf")):
            await bulk_add(update, ctx)
        return   # the watcher account is only used to forward channel PDFs
    store.upsert_user(u)
    if await admin_input(update, ctx):
        return
    if is_admin(update):
        if msg.reply_to_message and await ing_reply(update, ctx):
            return
        d = msg.document
        is_pdf = bool(d and ((d.mime_type or "") == "application/pdf" or (d.file_name or "").lower().endswith(".pdf")))
        fwd = getattr(msg, "forward_origin", None)
        from_chan = bool(fwd and getattr(fwd, "type", "") == "channel")
        if from_chan and not ctx.user_data.get("mode"):
            if is_pdf:
                await bulk_add(update, ctx)
            return   # forwarded channel posts that are not PDFs are skipped silently
        if is_pdf and not ctx.user_data.get("mode"):
            await ingest_pdf(update, ctx)
            return
    if not await gate_ok(update, ctx):
        return
    mode = ctx.user_data.get("mode")

    if mode == "hsearch":
        text = (msg.text or "").strip()
        if not text:
            await msg.reply_text("اكتب كلمات البحث نصاً من فضلك.")
            return
        ctx.user_data.pop("mode", None)
        files, total = search_files(text)
        log_search(u.id, text)
        if files:
            await msg.reply_text(f"🔍 نتائج «{trim(text, 40)}» ({total}):", reply_markup=results_markup(files, total))
        else:
            await msg.reply_text("😕 لم أجد ملزمة مطابقة. جرّب كلمات أقل (مثلاً المادة فقط) أو استعمل البحث الموجّه.",
                                 reply_markup=M([[B("🧭 بحث موجّه", callback_data="h:g:0:-:-:-"), B("🔎 بحث جديد", callback_data="h:qm")],
                                                 [B("🔙 المكتبة", callback_data="h:s")]]))
        return
    if mode == "quiz":
        topic = (msg.text or "").strip()
        if not topic:
            await msg.reply_text("أرسل اسم المادة أو الموضوع نصاً لأبني لك اختباراً.", reply_markup=back_markup())
            return
        ctx.user_data.pop("mode", None)
        try:
            await start_quiz(msg, ctx, topic, u.id)
        except RuntimeError as e:
            await msg.reply_text(str(e), reply_markup=back_markup())
        return

    ask = MODE_ASKS.get(mode)
    try:
        parts, err = await content_parts(update, ctx)
    except Exception:
        log.exception("download failed")
        await msg.reply_text("تعذر تنزيل الملف، حاول إرساله مرة أخرى.", reply_markup=back_markup())
        return
    if err:
        await msg.reply_text(err, reply_markup=back_markup())
        return
    if not parts:
        return
    await msg.chat.send_action(ChatAction.TYPING)
    try:
        result = await run_ai(mode, parts) if ask else await gemini(parts)
    except RuntimeError as e:
        await msg.reply_text(str(e), reply_markup=back_markup() if ask else None)
        return
    store.event(u.id, mode if ask else "chat")
    if mode == "pdf":
        try:
            pdf = build_pdf("ورقة مراجعة", result)
        except Exception:
            log.exception("pdf build failed")
            await send_long(msg, result)
            return
        await msg.reply_document(document=pdf, filename="مراجعة.pdf", caption="📄 ورقة المراجعة جاهزة!")
    else:
        await send_long(msg, result)
    if not ask:
        return
    await msg.reply_text("هل تريد شيئاً آخر؟", reply_markup=back_markup())

# ---------------- admin panel ----------------

def screen(name):
    s = store
    if name == "main":
        rows = [
            [B("📊 الإحصائيات", callback_data="a:stats"), B("👥 المستخدمون", callback_data="a:users")],
            [B("📢 إذاعة", callback_data="a:bc"), B("👋 رسالة الترحيب", callback_data="a:welcome")],
            [B("🔐 الاشتراك الإجباري", callback_data="a:sub"), B("⚙️ الإعدادات", callback_data="a:settings")],
            [B("📚 إدارة الملازم", callback_data="a:hm"), B("📝 كليشة الملازم", callback_data="a:cfoot")],
            [B("🖼 ختم الملازم", callback_data="k:m"), B("➕ قناة مصدر", callback_data="w:m")],
            [B("💾 نسخة احتياطية", callback_data="a:backup")],
            [B("🏠 قائمة الطالب", callback_data="m:menu")],
        ]
        return "🎛 لوحة الأدمن", M(rows)
    if name == "settings":
        rows = [
            [B(f"🛠 الصيانة {tick(s.on('maintenance'))}", callback_data="a:tmaint"),
             B(f"🔔 إشعار الدخول {tick(s.on('join_notice'))}", callback_data="a:tjoin")],
            [B("✏️ رسالة الصيانة", callback_data="a:maintmsg")],
            [B("🔙 رجوع", callback_data="a:main")],
        ]
        return "⚙️ الإعدادات", M(rows)
    if name == "sub":
        ch = s.get("gate_channel", "") or "غير محددة"
        rows = [
            [B(f"🔐 الاشتراك الإجباري {tick(s.on('gate_on'))}", callback_data="a:tgate")],
            [B("📺 تحديد القناة", callback_data="a:gch"), B("✏️ رسالة الاشتراك", callback_data="a:gmsg")],
            [B("🔙 رجوع", callback_data="a:main")],
        ]
        return f"🔐 الاشتراك الإجباري\nالقناة: {ch}\n(يجب أن يكون البوت مشرفاً في القناة)", M(rows)
    if name == "users":
        rows = [
            [B("🚫 حظر مستخدم", callback_data="a:ban"), B("✅ فك حظر", callback_data="a:unban")],
            [B("📋 المحظورون", callback_data="a:banned"), B("🕒 آخر النشطين", callback_data="a:recent")],
            [B("🔙 رجوع", callback_data="a:main")],
        ]
        return "👥 المستخدمون", M(rows)
    return "🎛 لوحة الأدمن", M([[B("🔙 رجوع", callback_data="a:main")]])

def back_admin(to="main"):
    return M([[B("🔙 رجوع", callback_data=f"a:{to}")]])

def stats_text():
    total, new_today, active, banned, feats, feats_day = store.stats()
    t = (f"📊 الإحصائيات\n\n👥 المستخدمون: {total}\n🆕 جدد (24 س): {new_today}\n"
         f"🟢 نشطون (24 س): {active}\n🚫 محظورون: {banned}\n\n📈 استخدام الميزات (الكل / 24 س):\n")
    day = dict(feats_day)
    for k, n in feats:
        if k == "join":
            continue
        t += f"• {FEAT_NAMES.get(k, k)}: {n} / {day.get(k, 0)}\n"
    if not any(k != "join" for k, _ in feats):
        t += "لا استخدام بعد.\n"
    n_h, n_d = store.q("SELECT COUNT(*), COALESCE(SUM(downloads),0) FROM h_files", fetch=True)[0]
    t += f"\n📚 الملازم: {n_h} ملزمة | {n_d} تنزيل\n"
    for title, d in store.q("SELECT title, downloads FROM h_files WHERE downloads>0 ORDER BY downloads DESC LIMIT 5", fetch=True):
        t += f"  ◦ {title}: {d}\n"
    n_s = store.q("SELECT COUNT(*) FROM searches", fetch=True)[0][0]
    t += f"\n🔍 عمليات البحث: {n_s}\n"
    for w, n in store.q("SELECT q, COUNT(*) c FROM searches GROUP BY q ORDER BY c DESC, MAX(ts) DESC LIMIT 5", fetch=True):
        t += f"  ◦ {w}: {n}\n"
    return t

def uline(r):
    return f"• {r[1]} (@{r[2] or '—'}) id={r[0]}"

WAIT_PROMPTS = {
    "bc": "📢 أرسل الآن الرسالة التي تريد إذاعتها لكل المستخدمين (نص أو صورة مع تعليق).",
    "welcome": "👋 أرسل نص رسالة الترحيب الجديدة. أرسل - للرجوع للافتراضية.",
    "gch": "📺 أرسل يوزر القناة مثل @mychannel (البوت مشرف فيها).",
    "gmsg": "✏️ أرسل نص رسالة الاشتراك. أرسل - للافتراضية.",
    "maintmsg": "✏️ أرسل نص رسالة الصيانة. أرسل - للافتراضية.",
    "cfoot": "📝 أرسل كليشة الملازم: النص الذي يظهر أسفل وصف كل ملزمة (يمكن عدة أسطر، ويوزر @ وروابط تعمل بالضغط). أرسل - لحذفها.",
    "ban": "🚫 أرسل آيدي المستخدم (رقم) لحظره.",
    "unban": "✅ أرسل آيدي المستخدم (رقم) لفك حظره.",
}

async def myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(f"🆔 آيديك: {u.id}\nاليوزر: @{u.username or '—'}")

async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    log.info("ADMIN_CMD from id=%s username=%s admin=%s", u.id, u.username, is_admin(update))
    if not is_admin(update):
        await update.message.reply_text(f"🚫 هذا الأمر للأدمن فقط.\nآيديك: {u.id}\n(إن كنت صاحب البوت فأرسل هذا الآيدي لإعداد الصلاحية.)")
        return
    ctx.user_data.pop("admin_wait", None)
    ctx.user_data.pop("mode", None)
    t, kb = screen("main")
    await update.message.reply_text(t, reply_markup=kb)

async def admin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("للأدمن فقط", show_alert=True)
        return
    await q.answer()
    act = q.data.split(":", 1)[1]
    ctx.user_data.pop("admin_wait", None)
    s = store
    if act.startswith("h"):
        await hadmin_cb(q, ctx, act)
        return
    if act in ("main", "settings", "sub", "users"):
        t, kb = screen(act)
    elif act == "stats":
        t, kb = stats_text(), back_admin()
    elif act in WAIT_PROMPTS:
        ctx.user_data["admin_wait"] = act
        back = {"bc": "main", "welcome": "main", "gch": "sub", "gmsg": "sub", "maintmsg": "settings"}.get(act, "users")
        t, kb = WAIT_PROMPTS[act], back_admin(back)
        if act == "cfoot":
            cur = (s.get("h_footer", "") or "").strip()
            t += "\n\nالكليشة الحالية:\n" + (cur or "(لا توجد)")
    elif act == "tmaint":
        s.set("maintenance", "0" if s.on("maintenance") else "1")
        t, kb = screen("settings")
    elif act == "tjoin":
        s.set("join_notice", "0" if s.on("join_notice") else "1")
        t, kb = screen("settings")
    elif act == "tgate":
        s.set("gate_on", "0" if s.on("gate_on") else "1")
        t, kb = screen("sub")
    elif act == "banned":
        rows = s.banned_users()
        t = "📋 المحظورون\n" + ("\n".join(uline(r) for r in rows) if rows else "لا يوجد.")
        kb = back_admin("users")
    elif act == "recent":
        rows = s.recent_users(10)
        t = "🕒 آخر النشطين\n" + "\n".join(uline(r) + f" - {ago(r[3])}" for r in rows)
        kb = back_admin("users")
    elif act == "backup":
        await do_backup()
        t, kb = "💾 تم تحديث النسخة الاحتياطية (رسالة واحدة مثبتة بصمت). النسخ التلقائي كل 48 ساعة، وتُحمَّل عند إعادة تشغيل الخدمة.", back_admin()
    else:
        t, kb = screen("main")
    try:
        await q.message.edit_text(t, reply_markup=kb)
    except Exception:
        await q.message.reply_text(t, reply_markup=kb)


# ---------------- handouts (ملازم) ----------------

def hq(sql, args=(), fetch=True):
    return store.q(sql, args, fetch=fetch)

def trim(t, n=40):
    t = (t or "").strip()
    return t if len(t) <= n else t[:n - 1] + "…"

async def hshow(q, text, kb):
    try:
        await q.message.edit_text(text, reply_markup=kb)
    except Exception:
        await q.message.reply_text(text, reply_markup=kb)

HOME = [B("🔙 القائمة الرئيسية", callback_data="m:menu")]

async def handout_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    store.upsert_user(q.from_user)
    if not await gate_ok(update, ctx):
        return
    parts = q.data.split(":")
    act = parts[1]
    if act == "s":
        rows = hq("SELECT s.id, s.name, (SELECT COUNT(*) FROM h_files f JOIN h_subjects j ON f.subject_id=j.id WHERE j.stage_id=s.id) FROM h_stages s ORDER BY s.id")
        kb = [[B("🔍 بحث", callback_data="h:q")]]
        kb += [[B(f"🎓 {trim(r[1])} ({r[2]})", callback_data=f"h:t:{r[0]}")] for r in rows[:50]]
        kb.append(HOME)
        await hshow(q, "📚 الملازم\nاختر المرحلة الدراسية:" if rows else "📚 الملازم\nلا توجد ملازم بعد، سنضيفها قريباً.", M(kb))
    elif act == "q":
        ctx.user_data.pop("mode", None)
        await hshow(q, "🔍 كيف تريد البحث عن الملزمة؟",
                    M([[B("⌨️ بحث بالكتابة", callback_data="h:qm")], [B("🧭 بحث موجّه بالأزرار", callback_data="h:g:0:-:-:-")],
                       [B("🔙 المكتبة", callback_data="h:s")]]))
    elif act == "qm":
        ctx.user_data["mode"] = "hsearch"
        await hshow(q, "⌨️ اكتب كلمات البحث: المادة أو الأستاذ أو العنوان أو السنة أو النوع.\nمثال: كيمياء سعد 2027 وزاريات",
                    M([[B("🔙 رجوع", callback_data="h:q")]]))
    elif act == "g":
        await guided_view(q, int(parts[2]), parts[3], parts[4], parts[5])
    elif act == "t":
        sid = int(parts[2])
        st = hq("SELECT name FROM h_stages WHERE id=?", (sid,))
        rows = hq("SELECT j.id, j.name, (SELECT COUNT(*) FROM h_files f WHERE f.subject_id=j.id) FROM h_subjects j WHERE j.stage_id=? ORDER BY j.id", (sid,))
        kb = [[B(f"📖 {trim(r[1])} ({r[2]})", callback_data=f"h:j:{r[0]}")] for r in rows[:50]]
        kb.append([B("🔙 رجوع", callback_data="h:s")])
        await hshow(q, f"🎓 {st[0][0] if st else ''}\nاختر المادة:" if rows else "لا توجد مواد في هذه المرحلة بعد.", M(kb))
    elif act in ("j", "r", "y"):
        jid = int(parts[2])
        j = hq("SELECT name, stage_id FROM h_subjects WHERE id=?", (jid,))
        sname = j[0][0] if j else ""
        tree = lib_tree(jid)
        back_subj = f"h:t:{j[0][1]}" if j else "h:s"
        if act == "j":
            kb = [[B(f"👨‍🏫 {trim(t['name'], 40)} ({t['n']})", callback_data=f"h:r:{jid}:{t['id']}")] for t in tree[:50]]
            kb.append([B("🔙 رجوع", callback_data=back_subj)])
            await hshow(q, f"📖 {sname}\nاختر الأستاذ:" if tree else "لا توجد ملازم في هذه المادة بعد.", M(kb))
            return
        tch = next((t for t in tree if t["id"] == parts[3]), None)
        if not tch:
            await hshow(q, "لم يعد هذا القسم متوفراً.", M([[B("🔙 رجوع", callback_data=f"h:j:{jid}")]]))
            return
        if act == "r":
            kb = [[B(f"🗂 {trim(y['name'], 40)} ({y['n']})", callback_data=f"h:y:{jid}:{tch['id']}:{y['id']}")] for y in tch["types"][:50]]
            kb.append([B("🔙 رجوع", callback_data=f"h:j:{jid}")])
            await hshow(q, f"👨‍🏫 {tch['name']} · {sname}\nاختر النوع:", M(kb))
            return
        ty = next((y for y in tch["types"] if y["id"] == parts[4]), None)
        if not ty:
            await hshow(q, "لم يعد هذا القسم متوفراً.", M([[B("🔙 رجوع", callback_data=f"h:r:{jid}:{tch['id']}")]]))
            return
        kb = [[B(f"📄 {trim(f['title'], 50)}", callback_data=f"h:d:{f['id']}")] for f in ty["files"][:60]]
        kb.append([B("🔙 رجوع", callback_data=f"h:r:{jid}:{tch['id']}")])
        await hshow(q, f"🗂 {ty['name']} · {tch['name']} · {sname}\nاختر الملف لتصلك:", M(kb))
    elif act == "d":
        hid = int(parts[2])
        r = hq("SELECT title, file_id, kind, subject_id FROM h_files WHERE id=?", (hid,))
        if not r:
            await q.message.reply_text("هذه الملزمة لم تعد متوفرة.")
            return
        title, fid, kind, jid = r[0]
        try:
            if kind == "photo":
                await q.message.reply_photo(fid, caption=hcaption(hid) or f"📄 {title}")
            else:
                await q.message.reply_document(fid, caption=hcaption(hid) or f"📄 {title}")
        except Exception:
            log.exception("send handout failed")
            await q.message.reply_text("تعذر إرسال الملف حالياً، حاول لاحقاً.")
            return
        store.q("UPDATE h_files SET downloads=downloads+1 WHERE id=?", (hid,))
        store.event(q.from_user.id, "handout")

# --- admin side ---

def hb(text, cb):
    return B(text, callback_data=cb)

async def hadmin_cb(q, ctx, act):
    parts = act.split(":")
    a = parts[0]
    back = [hb("🔙 رجوع", "a:main")]
    if a == "hm":
        n_s, n_j, n_h = [hq(f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("h_stages", "h_subjects", "h_files")]
        kb = [[hb("➕ إضافة ملزمة", "a:hb"), hb("📋 استعراض وتعديل", "a:hb")],
              [hb("🗂 مراحل جديدة", "a:hns")], back]
        await hshow(q, f"📚 إدارة الملازم\nالمراحل: {n_s} | المواد: {n_j} | الملازم: {n_h}\n\nلإضافة ملزمة: افتح المرحلة ثم المادة ثم اضغط ➕ إضافة ملزمة هنا.", M(kb))
    elif a == "hns":
        ctx.user_data["admin_wait"] = "hstage"
        await hshow(q, "🗂 أرسل اسم المرحلة الجديدة (مثال: السادس الإعدادي العلمي).", M([[hb("🔙 رجوع", "a:hm")]]))
    elif a == "hb":
        rows = hq("SELECT id, name FROM h_stages ORDER BY id")
        kb = [[hb(f"🎓 {trim(r[1])}", f"a:hbs:{r[0]}")] for r in rows[:50]]
        kb.append([hb("➕ مرحلة جديدة", "a:hns")])
        kb.append([hb("🔙 رجوع", "a:hm")])
        await hshow(q, "اختر المرحلة:" if rows else "لا توجد مراحل. أضف مرحلة أولاً.", M(kb))
    elif a == "hbs":
        sid = int(parts[1])
        st = hq("SELECT name FROM h_stages WHERE id=?", (sid,))
        rows = hq("SELECT id, name FROM h_subjects WHERE stage_id=? ORDER BY id", (sid,))
        kb = [[hb(f"📖 {trim(r[1])}", f"a:hbj:{r[0]}")] for r in rows[:50]]
        kb.append([hb("➕ مادة جديدة", f"a:hnj:{sid}")])
        kb.append([hb("✏️ تغيير اسم المرحلة", f"a:hrs:{sid}"), hb("🗑 حذف المرحلة", f"a:hds:{sid}")])
        kb.append([hb("🔙 رجوع", "a:hb")])
        await hshow(q, f"🎓 {st[0][0] if st else ''}\nالمواد:", M(kb))
    elif a == "hnj":
        ctx.user_data["admin_wait"] = f"hsub:{parts[1]}"
        await hshow(q, "📖 أرسل اسم المادة الجديدة (مثال: الفيزياء).", M([[hb("🔙 رجوع", f"a:hbs:{parts[1]}")]]))
    elif a == "hrs":
        ctx.user_data["admin_wait"] = f"hrst:{parts[1]}"
        await hshow(q, "✏️ أرسل الاسم الجديد للمرحلة.", M([[hb("🔙 رجوع", f"a:hbs:{parts[1]}")]]))
    elif a == "hds":
        sid = parts[1]
        await hshow(q, "⚠️ سيتم حذف المرحلة مع كل موادها وملازمها. متأكد؟",
                    M([[hb("✅ نعم احذف", f"a:hdsy:{sid}"), hb("❌ إلغاء", f"a:hbs:{sid}")]]))
    elif a == "hdsy":
        sid = int(parts[1])
        hq("DELETE FROM h_files WHERE subject_id IN (SELECT id FROM h_subjects WHERE stage_id=?)", (sid,), False)
        hq("DELETE FROM h_subjects WHERE stage_id=?", (sid,), False)
        hq("DELETE FROM h_stages WHERE id=?", (sid,), False)
        backup_soon()
        await hshow(q, "🗑 تم حذف المرحلة.", M([[hb("🔙 رجوع", "a:hb")]]))
    elif a == "hbj":
        jid = int(parts[1])
        j = hq("SELECT name, stage_id FROM h_subjects WHERE id=?", (jid,))
        if not j:
            await hshow(q, "المادة غير موجودة.", M([[hb("🔙 رجوع", "a:hb")]]))
            return
        rows = hq("SELECT id, title, downloads FROM h_files WHERE subject_id=? ORDER BY id", (jid,))
        kb = [[hb(f"📄 {trim(r[1], 30)} ({r[2]}⬇)", f"a:hh:{r[0]}")] for r in rows[:60]]
        kb.append([hb("➕ إضافة ملزمة هنا", f"a:hadd:{jid}")])
        kb.append([hb("✏️ تغيير اسم المادة", f"a:hrj:{jid}"), hb("🗑 حذف المادة", f"a:hdj:{jid}")])
        kb.append([hb("🔙 رجوع", f"a:hbs:{j[0][1]}")])
        await hshow(q, f"📖 {j[0][0]}\nالملازم ({len(rows)}):", M(kb))
    elif a == "hrj":
        ctx.user_data["admin_wait"] = f"hrnj:{parts[1]}"
        await hshow(q, "✏️ أرسل الاسم الجديد للمادة.", M([[hb("🔙 رجوع", f"a:hbj:{parts[1]}")]]))
    elif a == "hdj":
        jid = parts[1]
        await hshow(q, "⚠️ سيتم حذف المادة مع كل ملازمها. متأكد؟",
                    M([[hb("✅ نعم احذف", f"a:hdjy:{jid}"), hb("❌ إلغاء", f"a:hbj:{jid}")]]))
    elif a == "hdjy":
        jid = int(parts[1])
        j = hq("SELECT stage_id FROM h_subjects WHERE id=?", (jid,))
        hq("DELETE FROM h_files WHERE subject_id=?", (jid,), False)
        hq("DELETE FROM h_subjects WHERE id=?", (jid,), False)
        backup_soon()
        await hshow(q, "🗑 تم حذف المادة.", M([[hb("🔙 رجوع", f"a:hbs:{j[0][0]}" if j else "a:hb")]]))
    elif a == "hadd":
        ctx.user_data["admin_wait"] = f"htitle:{parts[1]}"
        await hshow(q, "📄 أرسل عنوان الملزمة (مثال: ملزمة الفصل الأول - أ. أحمد).", M([[hb("🔙 رجوع", f"a:hbj:{parts[1]}")]]))
    elif a == "hh":
        hid = int(parts[1])
        r = hq("SELECT title, downloads, subject_id FROM h_files WHERE id=?", (hid,))
        if not r:
            await hshow(q, "غير موجودة.", M([[hb("🔙 رجوع", "a:hb")]]))
            return
        kb = [[hb("✏️ تعديل العنوان", f"a:hrt:{hid}"), hb("🔄 استبدال الملف", f"a:hrf:{hid}")],
              [hb("📥 استلام الملف", f"a:hget:{hid}"), hb("🗑 حذف", f"a:hdh:{hid}")],
              [hb("🔙 رجوع", f"a:hbj:{r[0][2]}")]]
        await hshow(q, f"📄 {r[0][0]}\nالتنزيلات: {r[0][1]}", M(kb))
    elif a == "hget":
        r = hq("SELECT title, file_id, kind FROM h_files WHERE id=?", (int(parts[1]),))
        if r:
            if r[0][2] == "photo":
                await q.message.reply_photo(r[0][1], caption=hcaption(int(parts[1])) or r[0][0])
            else:
                await q.message.reply_document(r[0][1], caption=hcaption(int(parts[1])) or r[0][0])
    elif a == "hrt":
        ctx.user_data["admin_wait"] = f"hrnt:{parts[1]}"
        await hshow(q, "✏️ أرسل العنوان الجديد.", M([[hb("🔙 رجوع", f"a:hh:{parts[1]}")]]))
    elif a == "hrf":
        ctx.user_data["admin_wait"] = f"hrep:{parts[1]}"
        await hshow(q, "🔄 أرسل الملف الجديد (PDF أو صورة).", M([[hb("🔙 رجوع", f"a:hh:{parts[1]}")]]))
    elif a == "hdh":
        hid = parts[1]
        await hshow(q, "⚠️ حذف هذه الملزمة؟", M([[hb("✅ نعم احذف", f"a:hdhy:{hid}"), hb("❌ إلغاء", f"a:hh:{hid}")]]))
    elif a == "hdhy":
        hid = int(parts[1])
        r = hq("SELECT subject_id FROM h_files WHERE id=?", (hid,))
        hq("DELETE FROM h_files WHERE id=?", (hid,), False)
        backup_soon()
        await hshow(q, "🗑 تم حذف الملزمة.", M([[hb("🔙 رجوع", f"a:hbj:{r[0][0]}" if r else "a:hb")]]))
    else:
        await hshow(q, "📚 إدارة الملازم", M([[hb("🔙 رجوع", "a:hm")]]))

def file_of(msg):
    if msg.document:
        return msg.document.file_id, "document"
    if msg.photo:
        return msg.photo[-1].file_id, "photo"
    return None, None

async def hadmin_input(update, ctx, act, msg, text):
    parts = act.split(":")
    a = parts[0]
    def kbk(cb):
        return M([[hb("🔙 رجوع", cb)]])
    if a in ("hstage", "hsub", "hrst", "hrnj", "hrnt", "htitle") and not text:
        ctx.user_data["admin_wait"] = act
        await msg.reply_text("أرسل نصاً من فضلك.")
        return True
    if a == "hstage":
        hq("INSERT INTO h_stages(name) VALUES(?)", (text,), False)
        backup_soon()
        await msg.reply_text(f"✅ تمت إضافة المرحلة: {text}", reply_markup=kbk("a:hb"))
    elif a == "hsub":
        hq("INSERT INTO h_subjects(stage_id,name) VALUES(?,?)", (int(parts[1]), text), False)
        backup_soon()
        await msg.reply_text(f"✅ تمت إضافة المادة: {text}", reply_markup=kbk(f"a:hbs:{parts[1]}"))
    elif a == "hrst":
        hq("UPDATE h_stages SET name=? WHERE id=?", (text, int(parts[1])), False)
        backup_soon()
        await msg.reply_text("✅ تم تغيير الاسم.", reply_markup=kbk(f"a:hbs:{parts[1]}"))
    elif a == "hrnj":
        hq("UPDATE h_subjects SET name=? WHERE id=?", (text, int(parts[1])), False)
        backup_soon()
        await msg.reply_text("✅ تم تغيير الاسم.", reply_markup=kbk(f"a:hbj:{parts[1]}"))
    elif a == "hrnt":
        hq("UPDATE h_files SET title=? WHERE id=?", (text, int(parts[1])), False)
        backup_soon()
        await msg.reply_text("✅ تم تعديل العنوان.", reply_markup=kbk(f"a:hh:{parts[1]}"))
    elif a == "htitle":
        ctx.user_data["hnew_title"] = text
        ctx.user_data["admin_wait"] = f"hfile:{parts[1]}"
        await msg.reply_text(f"📎 العنوان: {text}\nالآن أرسل الملف (PDF أو صورة).", reply_markup=kbk(f"a:hbj:{parts[1]}"))
    elif a == "hfile":
        fid, kind = file_of(msg)
        if not fid:
            ctx.user_data["admin_wait"] = act
            await msg.reply_text("أرسل الملف نفسه (PDF أو صورة) وليس نصاً.")
            return True
        title = ctx.user_data.pop("hnew_title", None) or (msg.document.file_name if msg.document else "ملزمة")
        hq("INSERT INTO h_files(subject_id,title,file_id,kind,added) VALUES(?,?,?,?,?)",
           (int(parts[1]), title, fid, kind, time.time()), False)
        backup_soon()
        await msg.reply_text(f"✅ تمت إضافة الملزمة: {title}\nلإضافة أخرى اضغط ➕ مجدداً.",
                             reply_markup=M([[hb("➕ إضافة ملزمة أخرى", f"a:hadd:{parts[1]}")], [hb("📖 المادة", f"a:hbj:{parts[1]}")]]))
    elif a == "hrep":
        fid, kind = file_of(msg)
        if not fid:
            ctx.user_data["admin_wait"] = act
            await msg.reply_text("أرسل الملف نفسه.")
            return True
        hq("UPDATE h_files SET file_id=?, kind=? WHERE id=?", (fid, kind, int(parts[1])), False)
        backup_soon()
        await msg.reply_text("✅ تم استبدال الملف.", reply_markup=kbk(f"a:hh:{parts[1]}"))
    else:
        return False
    return True

async def admin_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle admin free-text input for pending panel actions. True if consumed."""
    if not is_admin(update):
        return False
    act = ctx.user_data.get("admin_wait")
    if not act:
        return False
    msg = update.message
    ctx.user_data.pop("admin_wait", None)
    text = (msg.text or msg.caption or "").strip()
    s = store
    if act.startswith("ie:"):
        return await ing_edit_input(update, ctx, act)
    if act.startswith("ks"):
        return await stamp_input(update, ctx, act)
    if act.startswith("wa"):
        return await watch_input(update, ctx, act)
    if act.startswith("h"):
        return await hadmin_input(update, ctx, act, msg, text)
    back = {"welcome": "main", "gch": "sub", "gmsg": "sub", "maintmsg": "settings"}
    if act == "bc":
        ids = s.all_user_ids()
        await msg.reply_text(f"⏳ جاري الإذاعة إلى {len(ids)} مستخدم...")
        ok = fail = 0
        for uid in ids:
            try:
                await msg.copy(uid)
                ok += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.05)
        await msg.reply_text(f"✅ انتهت الإذاعة\nنجح: {ok}\nفشل: {fail}", reply_markup=back_admin())
        return True
    if act == "cfoot":
        s.set("h_footer", "" if text == "-" else (msg.text or "").strip()[:700])
        await msg.reply_text("✅ تم حفظ كليشة الملازم." if text != "-" else "🗑 تم حذف الكليشة.", reply_markup=back_admin("main"))
        return True
    if act in ("welcome", "gmsg", "maintmsg"):
        key = {"welcome": "welcome", "gmsg": "gate_msg", "maintmsg": "maintenance_msg"}[act]
        s.set(key, "" if text == "-" else text)
        await msg.reply_text("✅ تم الحفظ.", reply_markup=back_admin(back[act]))
        return True
    if act == "gch":
        ch = text if text.startswith("@") else "@" + text
        try:
            c = await ctx.bot.get_chat(ch)
            me = await ctx.bot.get_chat_member(c.id, ctx.bot.id)
            if me.status not in ("administrator", "creator"):
                raise ValueError("not admin")
        except Exception:
            await msg.reply_text("❌ تعذر التحقق: تأكد أن اليوزر صحيح وأن البوت مشرف في القناة.", reply_markup=back_admin("sub"))
            return True
        s.set("gate_channel", ch)
        await msg.reply_text(f"✅ تم تحديد القناة {ch}", reply_markup=back_admin("sub"))
        return True
    if act in ("ban", "unban"):
        if not text.lstrip("-").isdigit():
            await msg.reply_text("❌ أرسل آيدي رقمي صحيح.", reply_markup=back_admin("users"))
            return True
        uid = int(text)
        if act == "ban" and uid in ADMIN_IDS:
            await msg.reply_text("❌ لا يمكن حظر أدمن.", reply_markup=back_admin("users"))
            return True
        s.set_ban(uid, act == "ban")
        await msg.reply_text("🚫 تم الحظر." if act == "ban" else "✅ تم فك الحظر.", reply_markup=back_admin("users"))
        return True
    return False

# ---------------- handout search ----------------
def all_files():
    rows = store.q("SELECT f.id, f.title, f.teacher, f.ftype, f.year, f.quality, j.name, s.name FROM h_files f "
                   "JOIN h_subjects j ON f.subject_id=j.id JOIN h_stages s ON j.stage_id=s.id ORDER BY f.id DESC", fetch=True)
    out = []
    for fid, title, teacher, ftype, year, quality, jn, sn in rows:
        t = eff_teacher(teacher, title)
        y = eff_type(ftype, title)
        fields = (title, jn, sn, "" if t == GENERAL else t, y, year or "", quality or "")
        out.append({"id": fid, "title": title, "teacher": t, "type": y, "year": year or "", "subject": jn, "stage": sn,
                    "doc": " ".join(nrm(x) for x in fields), "ntitle": nrm(title),
                    "sk": sid_of(nrm(jn)), "gk": sid_of(nrm(sn)), "tk": sid_of(t)})
    return out

def tok_variants(tok):
    v = {tok}
    if tok == "ملازم":
        v.add("ملزم")
    if len(tok) >= 4:
        v.add(tok[:-1])
    if len(tok) >= 5 and tok.endswith(("ات", "ون", "ين")):
        v.add(tok[:-2])
    return v

def search_files(query, stage=None, subject=None, teacher=None, limit=25):
    """query: free text (may be empty); stage/subject/teacher: normalized-name keys (sid_of) or None."""
    toks = [t for t in (nrm(w) for w in (query or "").split()) if t]
    res = []
    for f in all_files():
        if stage and f["gk"] != stage or subject and f["sk"] != subject or teacher and f["tk"] != teacher:
            continue
        score = 0
        ok = True
        for t in toks:
            vs = tok_variants(t)
            if not any(v in f["doc"] for v in vs):
                ok = False
                break
            score += 2 if any(v in f["ntitle"] for v in vs) else 1
        if ok:
            res.append((score, f))
    res.sort(key=lambda x: -x[0])
    return [f for _, f in res[:limit]], len(res)

def log_search(uid, q):
    q = " ".join(nrm(w) for w in (q or "").split() if nrm(w))[:60]
    if len(q) >= 2:
        store.q("INSERT INTO searches(ts,user_id,q) VALUES(?,?,?)", (time.time(), uid, q))

def results_markup(files, total, back="h:q"):
    kb = [[B(f"📄 {trim(f['title'], 40)} · {trim(f['stage'], 18)}", callback_data=f"h:d:{f['id']}")] for f in files]
    kb.append([B("🔎 بحث جديد", callback_data="h:q"), B("🔙 المكتبة", callback_data="h:s")])
    return M(kb)

def uniq_options(files, key, label):
    seen = {}
    for f in files:
        k = f[key]
        e = seen.setdefault(k, [f[label], 0])
        e[1] += 1
    return [(k, v[0], v[1]) for k, v in sorted(seen.items(), key=lambda kv: (kv[1][0] == GENERAL, kv[1][0]))]

async def guided_view(q, step, S, T, C):
    """step 0 subject, 1 stage, 2 teacher, 3 results. S/T/C: chosen key or '-'."""
    key = lambda x: None if x == "-" else x
    files, total = search_files("", stage=key(T), subject=key(S), teacher=key(C), limit=60)
    chosen = any(x != "-" for x in (S, T, C))
    if step >= 3 or (chosen and total <= 6):
        txt = f"🔍 النتائج ({total})" if files else "😕 لا توجد نتائج بهذه الاختيارات."
        await hshow(q, txt, results_markup(files[:25], total))
        return
    spec = {0: ("sk", "subject", "📖", "اختر المادة"), 1: ("gk", "stage", "🎓", "اختر المرحلة"), 2: ("tk", "teacher", "👨‍🏫", "اختر الأستاذ")}[step]
    opts = uniq_options(files, spec[0], spec[1])
    def nxt(val):
        ch = [S, T, C]
        ch[step] = val
        return f"h:g:{step + 1}:{ch[0]}:{ch[1]}:{ch[2]}"
    kb = [[B(f"{spec[2]} {trim(n, 35)} ({c})", callback_data=nxt(k))] for k, n, c in opts[:40]]
    row = [B("⏭ تخطي", callback_data=nxt("-"))]
    if chosen:
        row.append(B(f"📄 عرض النتائج ({total})", callback_data=f"h:g:3:{S}:{T}:{C}"))
    kb.append(row)
    kb.append([B("🔙 رجوع", callback_data="h:q")])
    await hshow(q, f"🧭 بحث موجّه ({step + 1}/3)\n{spec[3]}:" if opts else "لا توجد ملازم بعد.", M(kb))

# ---------------- handout library tree (stage > subject > teacher > type > files) ----------------
TYPES = ["ملازم", "وزاريات", "أسئلة", "ملخصات", "اختبارات"]
GENERAL = "عام"

def classify_type(text):
    n = nrm(text)
    for kw, t in (("وزاري", "وزاريات"), ("اختبار", "اختبارات"), ("امتحان", "اختبارات"), ("كويز", "اختبارات"),
                  ("سؤال", "أسئلة"), ("اسئل", "أسئلة"), ("ملخص", "ملخصات"), ("مختصر", "ملخصات")):
        if kw in n:
            return t
    return "ملازم"

def eff_teacher(teacher, title):
    t = (teacher or "").strip()
    if not t:
        m = re.search(r" - أ\. (.+)$", title or "")
        t = m.group(1).strip() if m else ""
    return t or GENERAL

def eff_type(ftype, title):
    return (ftype or "").strip() or classify_type(title)

def sid_of(name):
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:6]

def lib_tree(jid):
    """[{name, id, n, types:[{name, id, n, files:[{id,title}]}]}] for one subject."""
    teachers = {}
    for fid, title, teacher, ftype in store.q("SELECT id, title, teacher, ftype FROM h_files WHERE subject_id=? ORDER BY id", (jid,), fetch=True):
        t = eff_teacher(teacher, title)
        y = eff_type(ftype, title)
        teachers.setdefault(t, {}).setdefault(y, []).append({"id": fid, "title": title})
    out = []
    for t in sorted(teachers, key=lambda x: (x == GENERAL, x)):
        tys = teachers[t]
        order = sorted(tys, key=lambda y: (TYPES.index(y) if y in TYPES else len(TYPES), y))
        types = [{"name": y, "id": sid_of(y), "n": len(tys[y]), "files": tys[y]} for y in order]
        out.append({"name": t, "id": sid_of(t), "n": sum(x["n"] for x in types), "types": types})
    return out

# ---------------- handout caption ----------------
def stage_for(stage):
    st = (stage or "").strip()
    if re.search(r"(الإعدادي|الاعدادي)\s+(العلمي|الأدبي|الادبي)", st):
        st = re.sub(r"\s*(الإعدادي|الاعدادي)", "", st)
    st = re.sub(r"^ال", "لل", st) if st.startswith("ال") else "ل" + st
    return st

def hcaption(hid):
    r = hq("SELECT f.title, f.teacher, f.year, f.quality, j.name, s.name FROM h_files f "
           "JOIN h_subjects j ON f.subject_id=j.id JOIN h_stages s ON j.stage_id=s.id WHERE f.id=?", (hid,))
    if not r:
        return ""
    title, teacher, year, quality, subj, stage = [(x or "").strip() for x in r[0]]
    base = title
    if teacher and base.endswith(f" - أ. {teacher}"):
        base = base[: -len(f" - أ. {teacher}")].strip()
    if subj and nrm(subj) not in nrm(base):
        base = f"{base} - {subj}"
    lines = [f"💡➖ {base}", f"⭕️➖ {stage_for(stage)}"]
    if teacher:
        lines.append(f"🛎➖ للاستاذ {teacher}")
    if year:
        lines.append(f"✏️➖ لسنــــة : {year}")
    if quality:
        lines.append(f"✅➖ دقة : {quality}")
    head = "\n".join(lines)
    foot = (store.get("h_footer", "") or "").strip()
    if not foot:
        return head[:1024]
    room = 1024 - len(head) - 2
    if room < 10:
        return head[:1024]
    if len(foot) > room:
        foot = foot[: room - 1] + "…"
    return head + "\n\n" + foot

# ---------------- auto handout ingest (admin sends a PDF, AI files it) ----------------
ING_LOCK = asyncio.Lock()
PENDING = {}      # bot message id -> record waiting for an answer / confirmation
AUTO = set()      # ('s'|'j', id) created automatically, removed again if left empty
MAX_DL = 20 * 1024 * 1024   # Telegram Bot API download limit

ING_SYS = (
    "أنت أمين مكتبة مدرسية عراقية. تقرأ بداية ملزمة (PDF) واسم الملف ووصفها، وتستخرج بياناتها للأرشفة. "
    "أجب بـ JSON فقط بدون أي شرح أو علامات."
)

def nrm(s):
    s = re.sub(r"[\u064B-\u065F\u0640]", "", s or "")
    s = s.translate(str.maketrans("أإآىة", "ااايه")).lower()
    s = re.sub(r"(^|\s)ال", r"\1", s)
    return re.sub(r"[^\w]", "", s)

def stem(fname):
    s = re.sub(r"\.pdf$", "", fname or "", flags=re.I)
    return re.sub(r"[_]+", " ", s).strip() or "ملزمة"

def compose_title(title, teacher):
    title = (title or "").strip() or "ملزمة"
    teacher = (teacher or "").strip()
    if teacher and nrm(teacher) not in nrm(title):
        title = f"{title} - أ. {teacher}"
    return title

def get_stage(name, create=True):
    n = nrm(name)
    for sid, sname in hq("SELECT id, name FROM h_stages ORDER BY id"):
        if nrm(sname) == n:
            return sid
    if not create:
        return None
    hq("INSERT INTO h_stages(name) VALUES(?)", (name.strip(),), False)
    sid = hq("SELECT id FROM h_stages ORDER BY id DESC LIMIT 1")[0][0]
    AUTO.add(("s", sid))
    return sid

def get_subject(sid, name):
    n = nrm(name)
    for jid, jname in hq("SELECT id, name FROM h_subjects WHERE stage_id=? ORDER BY id", (sid,)):
        if nrm(jname) == n:
            return jid
    hq("INSERT INTO h_subjects(stage_id,name) VALUES(?,?)", (sid, name.strip()), False)
    jid = hq("SELECT id FROM h_subjects ORDER BY id DESC LIMIT 1")[0][0]
    AUTO.add(("j", jid))
    return jid

def cleanup_empty(jid):
    r = hq("SELECT stage_id FROM h_subjects WHERE id=?", (jid,))
    if not r:
        return
    sid = r[0][0]
    if ("j", jid) in AUTO and not hq("SELECT 1 FROM h_files WHERE subject_id=? LIMIT 1", (jid,)):
        hq("DELETE FROM h_subjects WHERE id=?", (jid,), False)
        AUTO.discard(("j", jid))
    if ("s", sid) in AUTO and not hq("SELECT 1 FROM h_subjects WHERE stage_id=? LIMIT 1", (sid,)):
        hq("DELETE FROM h_stages WHERE id=?", (sid,), False)
        AUTO.discard(("s", sid))

def pdf_head(data, pages=3):
    """First pages of a PDF: (text, small_pdf_bytes_or_None)."""
    from pypdf import PdfReader, PdfWriter
    r = PdfReader(io.BytesIO(data))
    if r.is_encrypted:
        try:
            r.decrypt("")
        except Exception:
            return "", None
    sel = r.pages[:pages]
    text = "\n".join((p.extract_text() or "") for p in sel).strip()
    small = None
    if len(text) < 80:   # scanned: let Gemini look at the pages themselves
        w = PdfWriter()
        for p in sel:
            w.add_page(p)
        buf = io.BytesIO()
        w.write(buf)
        if buf.tell() <= 7 * 1024 * 1024:
            small = buf.getvalue()
    return text[:3500], small

async def analyze_pdf(ctx, doc, caption, bulk=False, no_file=False):
    existing = []
    for sid, sname in hq("SELECT id, name FROM h_stages ORDER BY id"):
        subs = [r[0] for r in hq("SELECT name FROM h_subjects WHERE stage_id=? ORDER BY id", (sid,))]
        existing.append(f"- {sname}: " + (("، ".join(subs)) or "(لا مواد بعد)"))
    prompt = (
        "استخرج من هذه الملزمة: المرحلة/الصف، المادة، اسم المدرّس، وعنواناً مناسباً.\n"
        f"اسم الملف: {doc.file_name or ''}\nوصف الأدمن: {caption or '(لا يوجد)'}\n"
        "المراحل والمواد الموجودة حالياً في المكتبة:\n" + ("\n".join(existing) or "(فارغة)") + "\n\n"
        "القواعد: إذا كانت المرحلة أو المادة تطابق موجوداً فاستعمل اسمه حرفياً. "
        "المرحلة بصيغة مثل: السادس الإعدادي العلمي، الثالث المتوسط، الرابع الإعدادي. "
        "المادة مثل: الفيزياء، الرياضيات. اسم المدرّس بدون لقب (بدون أ. أو الأستاذ). "
        "العنوان هو عنوان الملزمة فقط مثل «ملزمة الفصل الأول» أو «أسئلة وزارية» بدون اسم المدرّس وبدون المرحلة. "
        "النوع type: واحد من (ملازم، وزاريات، أسئلة، ملخصات، اختبارات) حسب محتوى الملف، أو كلمة قصيرة أخرى إن لم ينطبق أي منها. "
        "السنة year: سنة أو دورة الملزمة كما هي مكتوبة فعلاً (مثل 2027 أو 2026-2027). "
        "الدقة quality: فقط إذا كُتبت صراحةً في الوصف أو اسم الملف (مثل سكنر، نسخة أصلية). "
        "لا تخمّن ولا تخترع: إذا لم تجد قيمة مكتوبة بوضوح (خصوصاً المدرّس والسنة) اتركها نصاً فارغاً.\n"
        'الصيغة: {"stage":"","subject":"","teacher":"","title":"","type":"","year":"","quality":""}'
    )
    if bulk:
        prompt += ("\nملاحظة: الوصف أعلاه هو نص منشور قناة عامة، وهو المصدر الأساسي للمرحلة والمادة والمدرّس والسنة. "
                   "تجاهل الروابط والإعلانات وأسماء القنوات. اكتب المرحلة بصيغتها الكاملة مثل السادس الإعدادي العلمي حتى لو كُتبت مختصرة (مثل: سادس علمي).")
    parts = [{"text": prompt}]
    size = doc.file_size or 0
    text, small, scanned = "", None, False
    if size and size <= MAX_DL and not no_file:
        data = await asyncio.wait_for(fetch_file(ctx, doc.file_id), 30)
        try:
            text, small = await asyncio.wait_for(asyncio.to_thread(pdf_head, data), 20)
        except Exception:
            log.exception("pdf_head failed")
            text, small = "", None
        del data
        scanned = not text
        if text:
            parts.append({"text": "نص أول صفحات الملف:\n" + text})
        elif small:
            parts.append({"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(small).decode()}})
    out = await asyncio.wait_for(gemini(parts, system=ING_SYS, temperature=0.1, max_tokens=1024, fast=True), 45)
    m = re.search(r"\{.*\}", out, re.S)
    d = json.loads(m.group(0)) if m else {}
    res = {k: str(d.get(k) or "").strip() for k in ("stage", "subject", "teacher", "title", "type", "year", "quality")}
    if res["year"] and not small:   # no visual source: the year must really appear in the name, caption or text
        src = f"{doc.file_name or ''} {caption or ''} {text}"
        if not all(y in src for y in re.findall(r"\d{4}", res["year"])) or not re.search(r"\d{4}", res["year"]):
            res["year"] = ""
    if not res["type"]:
        res["type"] = classify_type(f"{res['title']} {doc.file_name or ''} {caption or ''}")
    if not res["quality"] and scanned:
        res["quality"] = "سكنر"
    return res

SUBJ_KW = [("انكليزي", "اللغة الإنكليزية"), ("انجليزي", "اللغة الإنكليزية"), ("english", "اللغة الإنكليزية"),
           ("عربي", "اللغة العربية"), ("فيزياء", "الفيزياء"), ("كيمياء", "الكيمياء"), ("رياضيات", "الرياضيات"),
           ("احياء", "الأحياء"), ("اسلامي", "التربية الإسلامية"), ("حاسوب", "الحاسوب"), ("جغرافي", "الجغرافية"),
           ("تاريخ", "التاريخ"), ("فرنسي", "اللغة الفرنسية"), ("علوم", "العلوم")]
ORD = ["الاول", "الثاني", "الثالث", "الرابع", "الخامس", "السادس"]

def guess_from_name(name, caption):
    raw = f"{stem(name)} {caption or ''}"
    n = nrm(raw.replace("_", " ").replace("-", " "))
    g = {"stage": "", "subject": "", "teacher": "", "title": "", "type": classify_type(raw), "year": "", "quality": ""}
    ym = re.search(r"(20\d\d)(?:\s*[-/]\s*(20\d\d))?", raw)
    if ym:
        g["year"] = ym.group(0).replace(" ", "")
    if re.search(r"سكنر|سكانر|scan", raw, re.I):
        g["quality"] = "سكنر"
    for sid, sname in hq("SELECT id, name FROM h_subjects ORDER BY id"):
        if nrm(sname) and nrm(sname) in n:
            g["subject"] = sname
            break
    if not g["subject"]:
        for kw, nm in SUBJ_KW:
            if kw in n:
                g["subject"] = nm
                break
    for o in ORD:
        if nrm(o) in n:
            if "متوسط" in n or o in ("الاول", "الثاني", "الثالث") and "اعدادي" not in n:
                g["stage"] = f"{o} المتوسط".replace("الاول", "الأول")
            else:
                g["stage"] = f"{o} الإعدادي" + (" العلمي" if "علمي" in n else " الأدبي" if "ادبي" in n else "")
            break
    # teacher: first Arabic chunk of the file name that is not a subject, stage word or number
    for part in re.split(r"[_\-|]+", re.sub(r"\.pdf$", "", name or "", flags=re.I)):
        pn = nrm(part)
        if not pn or pn.isdigit() or not re.search(r"[\u0600-\u06FF]", part):
            continue
        if any(kw in pn for kw, _ in SUBJ_KW) or any(nrm(o) in pn for o in ORD) or "ملزم" in pn or "متوسط" in pn or "اعدادي" in pn:
            continue
        if len(part.split()) <= 4:
            g["teacher"] = part.strip()
            break
    year = re.search(r"(20\d\d)", stem(name))
    if g["subject"]:
        g["title"] = "ملزمة " + g["subject"] + (f" {year.group(1)}" if year else "")
    return g

def ing_summary(hid):
    r = hq("SELECT f.title, f.teacher, j.name, s.name, f.year, f.quality, f.ftype FROM h_files f JOIN h_subjects j ON f.subject_id=j.id "
           "JOIN h_stages s ON j.stage_id=s.id WHERE f.id=?", (hid,))
    if not r:
        return None, None
    title, teacher, subj, stage, year, quality, ftype = r[0]
    text = f"✅ {title}\n🎓 {stage} › 📘 {subj} › 👨‍🏫 {teacher or 'غير محدد'}\n📅 السنة: {year or 'غير محددة'} › 🔎 الدقة: {quality or 'غير محددة'}\n🗂 النوع: {eff_type(ftype, title)}"
    kb = M([[hb("✏️ تعديل", f"i:e:{hid}"), hb("🗑 حذف", f"i:d:{hid}")]])
    return text, kb

# ---------------- handout stamp (image on first + last page) ----------------

STAMP_LOCK = asyncio.Lock()
STAMPJOB = {"run": False, "stop": False}
POS_LABEL = {"tl": "أعلى اليسار", "tc": "أعلى الوسط", "tr": "أعلى اليمين",
             "bl": "أسفل اليسار", "bc": "أسفل الوسط", "br": "أسفل اليمين"}
POS_ORDER = ["tc", "bc", "tr", "tl", "br", "bl"]
SIZE_LABEL = {"s": "صغير", "m": "متوسط", "l": "كبير"}
SIZE_ORDER = ["s", "m", "l"]
OP_ORDER = ["100", "80", "60", "40"]

def stamp_cfg():
    import base64 as b64
    s = store
    f = s.get("stamp_img_f", "")
    if not f:
        try:
            from stamp_default import DEFAULT_B64 as f   # shipped default image (optional file)
        except Exception:
            f = ""
    if not f:
        return None
    l = s.get("stamp_img_l", "") or f
    pf = s.get("stamp_pos_f", "") or "tc"
    return {"on": s.on("stamp_on"), "img_f": b64.b64decode(f), "img_l": b64.b64decode(l), "has_l": bool(s.get("stamp_img_l", "")),
            "pos_f": pf, "pos_l": s.get("stamp_pos_l", "") or pf,
            "size": s.get("stamp_size", "") or "s", "op": int(s.get("stamp_op", "") or 100) / 100.0}

def stamp_rect(pos, size, iw, ih, W, H):
    frac = {"s": 0.30, "m": 0.50, "l": 0.75}[size]
    cap = {"s": 0.07, "m": 0.11, "l": 0.16}[size]
    w = W * frac
    h = w * ih / iw
    if h > H * cap:
        h = H * cap
        w = h * iw / ih
    m = min(W, H) * 0.02
    x = {"l": m, "c": (W - w) / 2, "r": W - w - m}[pos[1]]
    y = {"t": H - h - m, "b": m}[pos[0]]
    return x, y, w, h

def prep_stamp_image(data):
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im = im.convert("RGBA")
    im.thumbnail((900, 900))
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()

def stamp_pdf(data, cfg):
    """Return (stamped_bytes, pages_stamped). First and last page; one page once."""
    from pypdf import PdfReader, PdfWriter, Transformation
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from PIL import Image
    r = PdfReader(io.BytesIO(data))
    if r.is_encrypted:
        r.decrypt("")
    n = len(r.pages)
    if n == 0:
        raise ValueError("empty pdf")
    w = PdfWriter()
    w.append(r)
    del r
    targets = [(0, "f")] if n == 1 else [(0, "f"), (n - 1, "l")]
    imgs = {"f": Image.open(io.BytesIO(cfg["img_f"])).convert("RGBA"), "l": Image.open(io.BytesIO(cfg["img_l"])).convert("RGBA")}
    done = 0
    for idx, slot in targets:
        page = w.pages[idx]
        if (page.get("/Rotate", 0) or 0) % 360:
            try:
                page.transfer_rotation_to_content()
            except Exception:
                log.warning("rotate transfer failed")
                continue
        mb = page.cropbox
        W, H = float(mb.width), float(mb.height)
        im = imgs[slot]
        x, y, bw, bh = stamp_rect(cfg["pos_" + slot], cfg["size"], im.width, im.height, W, H)
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(W, H))
        c.setFillAlpha(cfg["op"])
        c.drawImage(ImageReader(im), x, y, bw, bh, mask="auto")
        c.save()
        ov = PdfReader(io.BytesIO(buf.getvalue())).pages[0]
        page.merge_transformed_page(ov, Transformation().translate(float(mb.left), float(mb.bottom)))
        done += 1
    if not done:
        raise ValueError("no page could be stamped")
    out = io.BytesIO()
    w.write(out)
    return out.getvalue(), done

def stamp_preview_png(cfg):
    from PIL import Image, ImageDraw
    W, H = 420, 594
    pages = []
    for slot, label in (("f", "الصفحة الأولى"), ("l", "الصفحة الأخيرة")):
        pg = Image.new("RGBA", (W, H), "white")
        d = ImageDraw.Draw(pg)
        d.rectangle([0, 0, W - 1, H - 1], outline=(180, 180, 180, 255))
        for i in range(24):
            yy = 70 + i * 20
            d.rectangle([40, yy, W - 40 - (i % 5) * 25, yy + 7], fill=(205, 205, 205, 255))
        d.rectangle([40, 36, 220, 52], fill=(120, 120, 120, 255))
        im = Image.open(io.BytesIO(cfg["img_" + slot])).convert("RGBA")
        x, y, bw, bh = stamp_rect(cfg["pos_" + slot], cfg["size"], im.width, im.height, W, H)
        st = im.resize((max(1, int(bw)), max(1, int(bh))))
        a = st.getchannel("A").point(lambda v: int(v * cfg["op"]))
        st.putalpha(a)
        pg.alpha_composite(st, (int(x), int(H - y - bh)))
        pages.append(pg)
    sheet = Image.new("RGB", (W * 2 + 30, H + 20), (235, 235, 235))
    sheet.paste(pages[0].convert("RGB"), (10, 10))
    sheet.paste(pages[1].convert("RGB"), (W + 20, 10))
    out = io.BytesIO()
    sheet.save(out, "PNG")
    return out.getvalue()

STORE_OK = {"v": None}   # None = unverified, True = deleted-message file_ids work, False = keep messages

async def store_doc(bot, out, fname):
    """Upload bytes to get a bot file_id without leaving chat clutter: delete the message afterwards.
    Verified once per process that a file_id still works after its message is deleted; if not, messages are kept."""
    m = await bot.send_document(ADMIN_BACKUP_CHAT, InputFile(io.BytesIO(out), filename=fname), disable_notification=True)
    fid = m.document.file_id
    if STORE_OK["v"] is None:
        try:
            t = await bot.send_document(ADMIN_BACKUP_CHAT, fid, disable_notification=True)
            await bot.delete_message(ADMIN_BACKUP_CHAT, m.message_id)   # original gone; fid must still work (the test copy proves it below)
            t2 = await bot.send_document(ADMIN_BACKUP_CHAT, fid, disable_notification=True)
            for x in (t, t2):
                try:
                    await bot.delete_message(ADMIN_BACKUP_CHAT, x.message_id)
                except Exception:
                    pass
            STORE_OK["v"] = True
            log.info("store_doc: file_id works after message deletion, cleanup on")
        except Exception:
            STORE_OK["v"] = False
            log.exception("store_doc: deleted-message file_id failed; keeping messages")
            m = await bot.send_document(ADMIN_BACKUP_CHAT, InputFile(io.BytesIO(out), filename=fname), disable_notification=True)
            fid = m.document.file_id
        return fid
    if STORE_OK["v"]:
        try:
            await bot.delete_message(ADMIN_BACKUP_CHAT, m.message_id)
        except Exception:
            pass
    return fid

async def apply_stamp(ctx, rec):
    """Stamp rec['fid'] (first + last page) and swap in the stamped file_id. Never raises. Returns a short note."""
    cfg = stamp_cfg()
    if not cfg or not cfg["on"]:
        return ""
    rec["stamped"] = 0
    size = int(rec.get("fsize", 0) or 0)
    if size > MAX_DL and (rec.get("fname") or "") in W.get("pre", {}):
        rec["stamped"] = 1
        return "🖼 ختم الصفحة الأولى والأخيرة (ملف كبير، عبر الحساب الثاني).\n"
    if size > MAX_DL:
        rec["stamp_fail"] = "كبير"
        return "⚠️ أُضيفت بدون ختم (الملف أكبر من 20MB).\n"
    try:
        async with STAMP_LOCK:
            data = await asyncio.wait_for(fetch_file(ctx, rec["fid"]), 60)
            out, n = await asyncio.wait_for(asyncio.to_thread(stamp_pdf, data, cfg), 90)
            del data
            new_fid = await store_doc(ctx.bot, out, rec.get("fname") or "handout.pdf")
            del out
        rec["orig_fid"] = rec["fid"]
        rec["fid"] = new_fid
        rec["stamped"] = 1
        return "🖼 تم ختم الصفحة الأولى والأخيرة.\n"
    except Exception as e:
        log.warning("stamp failed: %r", e)
        rec["stamp_fail"] = "خطأ"
        return "⚠️ أُضيفت بدون ختم (تعذّر الختم).\n"

def stamp_menu():
    cfg = stamp_cfg()
    s = store
    if not cfg:
        t = "🖼 ختم الملازم\n\nلا توجد صورة بعد. أرسل صورة الختم (يُفضّل PNG شفاف كملف)."
        return t, M([[hb("🖼 رفع صورة الختم", "k:i:f")], [hb("🔙 رجوع", "a:main")]])
    n_st = hq("SELECT COUNT(*) FROM h_files WHERE stamped=1")[0][0]
    n_all = hq("SELECT COUNT(*) FROM h_files WHERE kind='document'")[0][0]
    t = (f"🖼 ختم الملازم: {'🟢 مفعّل' if cfg['on'] else '🔴 معطّل'}\n"
         f"يُختم أول صفحة وآخر صفحة من كل ملزمة PDF جديدة (صفحة واحدة تُختم مرة).\n"
         f"📊 المختومة: {n_st} من {n_all}")
    kb = M([
        [hb("🔴 إيقاف" if cfg["on"] else "🟢 تفعيل", "k:t")],
        [hb("🖼 صورة الأولى", "k:i:f"), hb("🖼 صورة الأخيرة" + ("" if cfg["has_l"] else " (نفسها)"), "k:i:l")],
        [hb("📍 الأولى: " + POS_LABEL[cfg["pos_f"]], "k:p:f"), hb("📍 الأخيرة: " + POS_LABEL[cfg["pos_l"]], "k:p:l")],
        [hb("📏 الحجم: " + SIZE_LABEL[cfg["size"]], "k:z"), hb(f"🌫 الشفافية: {int(cfg['op'] * 100)}%", "k:o")],
        [hb("👁 معاينة", "k:v"), hb("🧪 جرّب على ملف PDF", "k:x")],
        [hb("♻️ ختم الملازم الموجودة", "k:r"), hb("🧹 إزالة الختم من الموجودة", "k:u")],
        [hb("🧽 استبدال الأختام القديمة", "k:o:m")],
        [hb("🔙 رجوع", "a:main")]])
    return t, kb

async def stamp_job(ctx, chat, mid):
    if STAMPJOB["run"]:
        return
    STAMPJOB.update(run=True, stop=False)
    rows = hq("SELECT id, file_id, fsize FROM h_files WHERE kind='document' AND COALESCE(stamped,0)=0 ORDER BY id")
    total, done, ok, fail, last = len(rows), 0, 0, 0, 0.0
    async def show(final=False):
        nonlocal last
        if not final and time.time() - last < 3:
            return
        try:
            kb = None if final else M([[hb("⛔ إيقاف", "k:s")]])
            await ctx.bot.edit_message_text(f"{'✅ انتهى' if final else '⏳'} ختم الملازم {done}/{total} · 🖼 {ok} · ⚠️ {fail} تعذّر", chat_id=chat, message_id=mid, reply_markup=kb)
            last = time.time()
        except Exception:
            pass
    try:
        for hid, fid, fsize in rows:
            if STAMPJOB["stop"]:
                break
            rec = {"fid": fid, "fsize": fsize or 0, "fname": "handout.pdf"}
            fn = hq("SELECT fname FROM h_files WHERE id=?", (hid,))[0][0]
            rec["fname"] = fn or "handout.pdf"
            await apply_stamp(ctx, rec)
            if rec.get("stamped"):
                hq("UPDATE h_files SET file_id=?, orig_fid=?, stamped=1 WHERE id=?", (rec["fid"], rec["orig_fid"], hid), False)
                ok += 1
            else:
                fail += 1
            done += 1
            await show()
            await asyncio.sleep(0.5)
    finally:
        STAMPJOB["run"] = False
        backup_soon()
        await show(final=True)

async def stamp_cb(update, ctx):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("للأدمن فقط", show_alert=True)
        return
    parts = q.data.split(":")
    a = parts[1]
    await q.answer()
    s = store
    cfg = stamp_cfg()
    if a == "m":
        pass
    elif a == "o":
        await old_cb(q, ctx, parts)
        return
    elif a == "i":
        ctx.user_data["admin_wait"] = "ksi:" + parts[2]
        extra = "\\nأرسل - لإلغاء صورة الأخيرة والعودة لنفس صورة الأولى." if parts[2] == "l" else ""
        await q.message.reply_text("🖼 أرسل صورة الختم الآن (PNG شفاف كملف أفضل، أو صورة عادية)." + extra.replace("\\n", "\n"))
        return
    elif a == "t":
        if cfg:
            s.set("stamp_on", "0" if cfg["on"] else "1")
    elif a == "p" and cfg:
        k = "stamp_pos_" + parts[2]
        cur = cfg["pos_" + parts[2]]
        s.set(k, POS_ORDER[(POS_ORDER.index(cur) + 1) % len(POS_ORDER)])
    elif a == "z" and cfg:
        s.set("stamp_size", SIZE_ORDER[(SIZE_ORDER.index(cfg["size"]) + 1) % 3])
    elif a == "o" and cfg:
        cur = str(int(cfg["op"] * 100))
        s.set("stamp_op", OP_ORDER[(OP_ORDER.index(cur) + 1) % 4] if cur in OP_ORDER else "100")
    elif a == "v" and cfg:
        png = await asyncio.to_thread(stamp_preview_png, cfg)
        await q.message.reply_photo(png, caption="👁 معاينة (نموذج صفحة): الأولى يميناً... الأخيرة يساراً حسب العرض. الصفحة الحقيقية تأخذ نفس الموضع نسبياً.")
        return
    elif a == "x":
        ctx.user_data["admin_wait"] = "ksx"
        await q.message.reply_text("🧪 أرسل ملف PDF لأختمه لك كتجربة (لا يُحفظ في المكتبة).")
        return
    elif a == "r":
        if STAMPJOB["run"]:
            await q.message.reply_text("⏳ هناك عملية ختم جارية.")
            return
        if not cfg:
            return
        m = await q.message.reply_text("⏳ جاري بدء ختم الملازم الموجودة...", reply_markup=M([[hb("⛔ إيقاف", "k:s")]]))
        asyncio.create_task(stamp_job(ctx, m.chat_id, m.message_id))
        return
    elif a == "s":
        STAMPJOB["stop"] = True
        return
    elif a == "u":
        n = hq("SELECT COUNT(*) FROM h_files WHERE stamped=1 AND COALESCE(orig_fid,'')<>''")[0][0]
        hq("UPDATE h_files SET file_id=orig_fid, stamped=0 WHERE stamped=1 AND COALESCE(orig_fid,'')<>''", (), False)
        backup_soon()
        await q.message.reply_text(f"🧹 أُعيدت النسخ الأصلية لـ {n} ملزمة. (الملازم الجديدة تُختم ما دام الختم مفعّلاً، عطّله من هنا.)")
    t, kb = stamp_menu()
    try:
        await q.message.edit_text(t, reply_markup=kb)
    except Exception:
        await q.message.reply_text(t, reply_markup=kb)

# ---------------- old manual stamp pages: detect and replace ----------------
OLD_HAM = 24          # max differing bits (of 256) to count as the same old-stamp page
OLDJOB = {"run": False, "stop": False}
OLDC = {}             # candidate idx -> {"sig","count","fids"}
OLDST = {"tested": False}
AIST = {"on": True, "budget": 150}

def img_sig(pim):
    g = pim.convert("L").resize((17, 16))
    px = list(g.getdata())
    bits = 0
    for r in range(16):
        for c in range(16):
            bits = (bits << 1) | (1 if px[r * 17 + c] > px[r * 17 + c + 1] else 0)
    return "%064x:%.3f" % (bits, pim.width / max(1, pim.height))

def sig_dist(a, b):
    try:
        ha, aa = a.split(":")
        hb2, ab = b.split(":")
        if abs(float(aa) - float(ab)) / max(float(ab), 0.01) > 0.15:
            return 999
        return bin(int(ha, 16) ^ int(hb2, 16)).count("1")
    except Exception:
        return 999

def page_sig(page):
    """Signature of the page's main image if the page is image-only (no text layer), else ''."""
    try:
        txt = (page.extract_text() or "").strip()
    except Exception:
        txt = ""
    if len(txt) > 25:
        return ""
    best = None
    try:
        for im in list(page.images):
            try:
                p = im.image
                a = p.width * p.height
                if best is None or a > best[0]:
                    best = (a, p)
            except Exception:
                continue
    except Exception:
        return ""
    if not best or min(best[1].size) < 200:
        return ""
    return img_sig(best[1])

HANDLE_RE = re.compile(r"(?:(?<![\w.])@[A-Za-z][A-Za-z0-9_]{3,31}\b|(?:https?://)?(?:t|telegram)\.me/[\w+/\-]+|(?:tg://\S+))", re.I)

def text_verdict(page):
    """'p' = mostly a promo page (handle + little text), 'r' = handle but real content (review only), '' = none."""
    try:
        txt = page.extract_text() or ""
    except Exception:
        return ""
    if not HANDLE_RE.search(txt):
        return ""
    rest = HANDLE_RE.sub(" ", txt)
    words = len(re.findall(r"\w{2,}", rest))
    return "p" if words <= 60 else "r"

def page_jpeg(page):
    best = None
    try:
        for im in list(page.images):
            try:
                p = im.image
                a = p.width * p.height
                if best is None or a > best[0]:
                    best = (a, p)
            except Exception:
                continue
    except Exception:
        return None
    if not best:
        return None
    p = best[1].convert("RGB")
    p.thumbnail((700, 700))
    bio = io.BytesIO()
    p.save(bio, "JPEG", quality=60)
    return bio.getvalue()

def pdf_scan(data):
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    if r.is_encrypted:
        r.decrypt("")
    n = len(r.pages)
    out = {"n": n, "sf": "", "sl": "", "tf": "", "tl": "", "jf": None, "jl": None}
    for k, idx in (("f", 0), ("l", n - 1)):
        if idx < 0 or (k == "l" and n < 2):
            continue
        pg = r.pages[idx]
        out["s" + k] = page_sig(pg)
        out["t" + k] = text_verdict(pg)
        if out["s" + k]:
            out["j" + k] = page_jpeg(pg)
    return out

def pdf_sigs(data):
    o = pdf_scan(data)
    return o["n"], o["sf"], o["sl"]

AI_PROMPT = ("This is a first or last page of a study handout PDF. Is the page MAINLY an advertisement or promo page "
             "(channel/bot promotion, Telegram handles, QR code, logo banner) with NO study content (no lecture, exam or questions)? "
             "Answer with exactly one letter: P if it is promo only, N otherwise (including if unsure).")

async def ai_promo(jpeg):
    """'p' / 'n', or None when AI is unavailable (quota)."""
    if time.time() < AIST.get("off_until", 0):
        return None
    try:
        t = await gemini([img_part(jpeg), {"text": AI_PROMPT}], system="", temperature=0, max_tokens=200, fast=True)
    except Exception as e:
        log.warning("ai promo unavailable: %s", str(e)[:80])
        AIST["off_until"] = time.time() + 600
        return None
    return "p" if t.strip().upper().startswith("P") else "n"

def old_refs():
    try:
        return json.loads(store.get("old_refs", "") or "[]")
    except Exception:
        return []

def old_match(sig, refs=None):
    """(best distance, matched?)"""
    if not sig:
        return 999, False
    d = min([sig_dist(sig, r["s"]) for r in (refs if refs is not None else old_refs())] or [999])
    return d, d <= OLD_HAM

def clean_pdf(data, rm_first, rm_last):
    from pypdf import PdfReader, PdfWriter
    r = PdfReader(io.BytesIO(data))
    if r.is_encrypted:
        r.decrypt("")
    n = len(r.pages)
    drop = set()
    if rm_first:
        drop.add(0)
    if rm_last and n > 1:
        drop.add(n - 1)
    if len(drop) >= n:
        raise ValueError("would remove every page")
    w = PdfWriter()
    for i in range(n):
        if i not in drop:
            w.add_page(r.pages[i])
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()

def old_db_init():
    hq("CREATE TABLE IF NOT EXISTS h_sig(fid TEXT PRIMARY KEY, pages INTEGER, sf TEXT, sl TEXT)", (), False)
    for col in ("prev_fid TEXT", "prev_orig TEXT", "prev_st INTEGER"):
        try:
            hq("ALTER TABLE h_files ADD COLUMN " + col, (), False)
        except Exception:
            pass
    for col in ("tf TEXT", "tl TEXT", "af TEXT", "al TEXT"):
        try:
            hq("ALTER TABLE h_sig ADD COLUMN " + col, (), False)
        except Exception:
            pass

def src_fid(fid, stamped, orig):
    return orig if (stamped and orig) else fid

def old_menu():
    n = len(old_refs())
    t = (f"🧹 استبدال الأختام اليدوية القديمة\n"
         f"النماذج المحفوظة: {n}\n\n"
         "يُحذف تلقائياً فقط: (1) صفحة أولى/أخيرة نصية فيها @ أو t.me مع كلام قليل (إعلان)، (2) صفحة صورة تشبه نموذجاً أكّدته، (3) صفحة صورة يحكم الذكاء أنها إعلان بدون محتوى دراسي. "
         "الصفحة التي فيها @ لكن لها محتوى حقيقي تُترك. لا تُحذف إلا الصفحة الأولى/الأخيرة. بعد الحذف يوضع الختم الجديد. التسلسل الآمن: أضف نماذج ← فحص ← تجربة على ملف ← تطبيق. ويمكن التراجع.")
    kb = M([
        [hb("🤖 تشغيل تلقائي الآن (فحص + حذف + ختم)", "k:o:auto")],
        [hb(("✅ " if store.get("old_auto", "1") == "1" else "⬜ ") + "تلقائي للملازم الجديدة", "k:o:ao")],
        [hb("🔎 مراجعة الأشكال المتكررة يدوياً", "k:o:c")],
        [hb("➕ إضافة نموذج يدوي", "k:o:a"), hb(f"🗑 مسح النماذج ({n})", "k:o:dx")],
        [hb("🔍 فحص (بدون تغيير)", "k:o:d")],
        [hb(("✅ " if store.get("old_ai", "1") == "1" else "⬜ ") + "فحص صفحات الصور بالذكاء (@ / إعلان)", "k:o:ai")],
        [hb("🧪 تجربة على ملف", "k:o:t"), hb("▶️ تطبيق على الكل", "k:o:g")],
        [hb("↩️ تراجع عن الاستبدال", "k:o:u")],
        [hb("🔙 رجوع", "k:m")]])
    return t, kb

async def old_scan(ctx, chat, mid, after):
    """Compute and cache first/last page signatures for every library PDF (downloadable ones)."""
    if OLDJOB["run"]:
        return
    OLDJOB.update(run=True, stop=False)
    AIST.update(on=True, budget=150)
    rows = hq("SELECT id, file_id, COALESCE(stamped,0), COALESCE(orig_fid,''), COALESCE(fsize,0) FROM h_files WHERE kind='document'")
    todo = []
    for hid, fid, st, orig, fs in rows:
        sf = src_fid(fid, st, orig)
        if (fs or 0) > MAX_DL:
            continue
        r0 = hq("SELECT tf FROM h_sig WHERE fid=?", (sf,))
        if not r0 or r0[0][0] is None:
            todo.append(sf)
    done, bad, last = 0, 0, 0.0
    async def show(final=False):
        nonlocal last
        if not final and time.time() - last < 3:
            return
        try:
            await ctx.bot.edit_message_text(f"{'✅ انتهى الفحص' if final else '⏳ فحص الملازم'} {done}/{len(todo)} · تعذّر {bad}", chat_id=chat, message_id=mid,
                                            reply_markup=None if final else M([[hb("⛔ إيقاف", "k:o:s")]]))
            last = time.time()
        except Exception:
            pass
    try:
        for sf in todo:
            if OLDJOB["stop"]:
                break
            try:
                data = await asyncio.wait_for(fetch_file(ctx, sf), 60)
                o = await asyncio.wait_for(asyncio.to_thread(pdf_scan, data), 90)
                del data
                af = al = ""
                refs = old_refs()
                if AIST["on"] and store.get("old_ai", "1") == "1" and AIST["budget"] > 0:
                    for k in ("f", "l"):
                        j = o["j" + k]
                        if j and not old_match(o["s" + k], refs)[1] and not o["t" + k]:
                            v = await ai_promo(j)
                            AIST["budget"] -= 1
                            if v is None:
                                AIST["on"] = False
                                break
                            if k == "f":
                                af = v
                            else:
                                al = v
                            await asyncio.sleep(0.5)
                hq("INSERT OR REPLACE INTO h_sig(fid,pages,sf,sl,tf,tl,af,al) VALUES(?,?,?,?,?,?,?,?)", (sf, o["n"], o["sf"], o["sl"], o["tf"], o["tl"], af, al), False)
            except Exception as e:
                bad += 1
                log.warning("old scan failed: %r", e)
                hq("INSERT OR REPLACE INTO h_sig(fid,pages,sf,sl,tf,tl,af,al) VALUES(?,?,?,?,?,?,?,?)", (sf, 0, "", "", "", "", "", ""), False)
            done += 1
            await show()
            await asyncio.sleep(0.3)
    finally:
        OLDJOB["run"] = False
        backup_soon()
        await show(True)
    try:
        if after == "d":
            await old_dry(ctx, chat)
        elif after == "c":
            await old_cands(ctx, chat)
        elif after == "auto":
            ad, rj, un = await old_auto_adopt(ctx)
            await ctx.bot.send_message(chat, f"🤖 الأشكال المتكررة: اعتُمد {ad} · رُفض {rj} · غير محسوم (تُركت) {un}")
            m = await ctx.bot.send_message(chat, "⏳ جاري الاستبدال...", reply_markup=M([[hb("⛔ إيقاف", "k:o:s")]]))
            await old_apply(ctx, m.chat_id, m.message_id)
    except Exception:
        log.exception("old after-scan")

def old_hits(review=False):
    """Pages to remove: confirmed shape, or promo text page (handle + little text), or AI-judged promo image page.
    review=True returns files with a handle on first/last page but real content (never removed)."""
    refs = old_refs()
    out = []
    for hid, title, fid, st, orig in hq("SELECT id, title, file_id, COALESCE(stamped,0), COALESCE(orig_fid,'') FROM h_files WHERE kind='document'"):
        r = hq("SELECT pages, sf, sl, COALESCE(tf,''), COALESCE(tl,''), COALESCE(af,''), COALESCE(al,'') FROM h_sig WHERE fid=?", (src_fid(fid, st, orig),))
        if not r:
            continue
        n, a, b, tf, tl, af, al = r[0]
        if review:
            if (tf == "r" or tl == "r") and not (tf == "p" or tl == "p"):
                out.append((hid, title))
            continue
        mf = old_match(a, refs)[1] or tf == "p" or af == "p"
        ml = n > 1 and (old_match(b, refs)[1] or tl == "p" or al == "p")
        if mf or ml:
            out.append((hid, title, mf, ml, n))
    return out

async def old_dry(ctx, chat):
    hits = old_hits()
    rv = old_hits(True)
    nf = sum(1 for h in hits if h[2])
    nl = sum(1 for h in hits if h[3])
    total = hq("SELECT COUNT(*) FROM h_sig")[0][0]
    t = f"🔍 نتيجة الفحص (لا تغيير): {len(hits)} ملزمة فيها ختم قديم من أصل {total} مفحوصة · صفحة أولى: {nf} · صفحة أخيرة: {nl}\n"
    for hid, title, mf, ml, n in hits[:15]:
        t += f"• {trim(title, 40)} ({'أولى' if mf else ''}{' + ' if mf and ml else ''}{'أخيرة' if ml else ''})\n"
    if len(hits) > 15:
        t += f"… و{len(hits) - 15} أخرى\n"
    if rv:
        t += f"\nتُركت دون حذف (فيها @ لكن لها محتوى حقيقي): {len(rv)}\n"
    if not AIST["on"]:
        t += "\n⚠️ حصة الذكاء امتلأت أثناء الفحص: بعض الصفحات الصورية لم تُفحص بالذكاء. أعد 🔍 فحص لاحقاً لإكمالها.\n"
    t += "\nالخطوة التالية: 🧪 تجربة على ملف ثم ▶️ تطبيق."
    await ctx.bot.send_message(chat, t)

def old_clusters(minfiles=3, limit=8):
    rows = hq("SELECT fid, sf, sl FROM h_sig")
    cl = []
    for fid, a, b in rows:
        for sg, kind in ((a, "أولى"), (b, "أخيرة")):
            if not sg:
                continue
            for c in cl:
                if sig_dist(sg, c["sig"]) <= 12:
                    c["fids"].add(fid)
                    c["k"][kind] = c["k"].get(kind, 0) + 1
                    break
            else:
                cl.append({"sig": sg, "fids": {fid}, "k": {kind: 1}})
    cl = [c for c in cl if len(c["fids"]) >= minfiles]
    cl.sort(key=lambda c: -len(c["fids"]))
    refs = old_refs()
    no = []
    try:
        no = json.loads(store.get("old_no", "") or "[]")
    except Exception:
        pass
    return [c for c in cl if not old_match(c["sig"], refs)[1] and not any(sig_dist(c["sig"], z) <= 12 for z in no)][:limit]

async def old_cluster_jpeg(ctx, c):
    fid = next(iter(c["fids"]))
    data = await fetch_file(ctx, fid)
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    pg = r.pages[0] if "أولى" in c["k"] else r.pages[len(r.pages) - 1]
    return page_jpeg(pg)

async def old_auto_adopt(ctx):
    """Auto mode: a repeating image page becomes an old-stamp shape only if the AI judges it promo-only. Returns (adopted, rejected, unknown)."""
    ad = rj = un = 0
    for c in old_clusters(3, 8):
        try:
            j = await old_cluster_jpeg(ctx, c)
            v = await ai_promo(j) if j else None
        except Exception:
            v = None
        if v == "p":
            refs = old_refs()
            refs.append({"s": c["sig"], "n": f"auto{len(refs) + 1}"})
            store.set("old_refs", json.dumps(refs[-40:]))
            ad += 1
        elif v == "n":
            try:
                no = json.loads(store.get("old_no", "") or "[]")
            except Exception:
                no = []
            no.append(c["sig"])
            store.set("old_no", json.dumps(no[-60:]))
            rj += 1
        else:
            un += 1
        await asyncio.sleep(0.5)
    return ad, rj, un

async def old_cands(ctx, chat):
    """Manual review of repeating image-only first/last pages."""
    cl = old_clusters(3, 8)
    if not cl:
        await ctx.bot.send_message(chat, "ما لقيت صفحات صورة متكررة (3 ملفات فأكثر) غير مغطاة بالنماذج.")
        return
    OLDC.clear()
    await ctx.bot.send_message(chat, f"🔎 وجدت {len(cl)} شكل متكرر. اعتمد الأشكال التي هي ختم قديم فقط (الصفحات الحقيقية المصوّرة لا تتكرر عادةً):")
    for i, c in enumerate(cl):
        OLDC[i] = c
        try:
            fid = next(iter(c["fids"]))
            data = await fetch_file(ctx, fid)
            from pypdf import PdfReader
            r = PdfReader(io.BytesIO(data))
            pg = r.pages[0] if "أولى" in c["k"] else r.pages[len(r.pages) - 1]
            best = max(list(pg.images), key=lambda im: im.image.width * im.image.height).image
            best = best.convert("RGB")
            best.thumbnail((500, 500))
            bio = io.BytesIO()
            best.save(bio, "JPEG", quality=70)
            bio.seek(0)
            ks = " + ".join(f"{k} {v}" for k, v in c["k"].items())
            await ctx.bot.send_photo(chat, bio, caption=f"الشكل {i + 1}: يتكرر في {len(c['fids'])} ملف ({ks})",
                                     reply_markup=M([[hb("✅ هذا ختم قديم", f"k:o:p:{i}"), hb("❌ لا", f"k:o:n:{i}")]]))
        except Exception as e:
            log.warning("cand thumb failed: %r", e)
            await ctx.bot.send_message(chat, f"الشكل {i + 1}: يتكرر في {len(c['fids'])} ملف (تعذّر عرض الصورة)",
                                       reply_markup=M([[hb("✅ هذا ختم قديم", f"k:o:p:{i}"), hb("❌ لا", f"k:o:n:{i}")]]))

async def old_fix_one(ctx, data, cfg, live_ai=False, cache=None):
    """Returns (cleaned_bytes, final_bytes_or_None, removed_first, removed_last) or None when nothing matches."""
    o = await asyncio.to_thread(pdf_scan, data)
    refs = old_refs()
    af = al = ""
    if live_ai and store.get("old_ai", "1") == "1":
        for k in ("f", "l"):
            if o["j" + k] and not old_match(o["s" + k], refs)[1] and not o["t" + k]:
                v = await ai_promo(o["j" + k])
                if k == "f":
                    af = v or ""
                else:
                    al = v or ""
    elif cache:
        af, al = cache
    mf = old_match(o["sf"], refs)[1] or o["tf"] == "p" or af == "p"
    ml = o["n"] > 1 and (old_match(o["sl"], refs)[1] or o["tl"] == "p" or al == "p")
    if not (mf or ml):
        return None
    cleaned = await asyncio.to_thread(clean_pdf, data, mf, ml)
    final = None
    if cfg and cfg["on"]:
        final, _ = await asyncio.to_thread(stamp_pdf, cleaned, cfg)
    return cleaned, final, mf, ml

async def old_apply(ctx, chat, mid):
    if OLDJOB["run"]:
        return
    OLDJOB.update(run=True, stop=False)
    hits = old_hits()
    done, ok, fail, last = 0, 0, 0, 0.0
    async def show(final=False):
        nonlocal last
        if not final and time.time() - last < 3:
            return
        try:
            await ctx.bot.edit_message_text(f"{'✅ انتهى' if final else '⏳'} استبدال الأختام القديمة {done}/{len(hits)} · ✔ {ok} · ⚠️ {fail}", chat_id=chat, message_id=mid,
                                            reply_markup=None if final else M([[hb("⛔ إيقاف", "k:o:s")]]))
            last = time.time()
        except Exception:
            pass
    try:
        for hid, title, mf, ml, n in hits:
            if OLDJOB["stop"]:
                break
            try:
                fid, st, orig, fn = hq("SELECT file_id, COALESCE(stamped,0), COALESCE(orig_fid,''), COALESCE(fname,'') FROM h_files WHERE id=?", (hid,))[0]
                cfg = stamp_cfg()
                async with STAMP_LOCK:
                    data = await asyncio.wait_for(fetch_file(ctx, src_fid(fid, st, orig)), 60)
                    cr = hq("SELECT COALESCE(af,''), COALESCE(al,'') FROM h_sig WHERE fid=?", (src_fid(fid, st, orig),))
                    res = await asyncio.wait_for(old_fix_one(ctx, data, cfg, cache=(cr[0] if cr else None)), 240)
                    del data
                    if not res:
                        fail += 1
                        done += 1
                        continue
                    cleaned, final, _, _ = res
                    fname = fn or "handout.pdf"
                    cfid = await store_doc(ctx.bot, cleaned, fname)
                    if final:
                        ffid = await store_doc(ctx.bot, final, fname)
                        hq("UPDATE h_files SET prev_fid=?, prev_orig=?, prev_st=?, file_id=?, orig_fid=?, stamped=1 WHERE id=?", (fid, orig, st, ffid, cfid, hid), False)
                    else:
                        hq("UPDATE h_files SET prev_fid=?, prev_orig=?, prev_st=?, file_id=?, orig_fid='', stamped=0 WHERE id=?", (fid, orig, st, cfid, hid), False)
                ok += 1
            except Exception as e:
                fail += 1
                log.warning("old apply failed %s: %r", hid, e)
            done += 1
            await show()
            await asyncio.sleep(0.5)
    finally:
        OLDJOB["run"] = False
        backup_soon()
        await show(True)

async def old_auto_ingest(ctx, rec):
    """New handout: remove a stamp/promo FIRST/LAST page automatically (never other pages). Silent on any doubt."""
    try:
        if store.get("old_auto", "1") != "1" or int(rec.get("fsize", 0) or 0) > MAX_DL:
            return ""
        async with STAMP_LOCK:
            data = await asyncio.wait_for(fetch_file(ctx, rec["fid"]), 60)
            res = await asyncio.wait_for(old_fix_one(ctx, data, None, live_ai=True), 120)
            del data
            if not res:
                return ""
            cleaned, _, mf, ml = res
            rec["fid"] = await store_doc(ctx.bot, cleaned, rec.get("fname") or "handout.pdf")
        rec["old_removed"] = (1 if mf else 0) + (1 if ml else 0)
        return "🧽 حُذفت صفحة إعلان/ختم قديم.\n"
    except Exception as e:
        log.warning("old auto ingest failed: %r", e)
        return ""

async def old_cb(q, ctx, parts):
    x = parts[2] if len(parts) > 2 else ""
    chat = q.message.chat_id
    if x == "a":
        ctx.user_data["admin_wait"] = "kso:a"
        await q.message.reply_text("➕ أرسل نموذج الختم القديم: (1) صورة الصفحة، أو (أفضل) (2) ملف PDF فيه الصفحة مع كتابة في التعليق: 1 إذا الختم أول صفحة، أو 0 إذا آخر صفحة. يمكنك إضافة عدة نماذج، واحداً بعد الآخر.")
        return
    if x == "dx":
        store.set("old_refs", "[]")
        OLDST["tested"] = False
    elif x == "ao":
        store.set("old_auto", "0" if store.get("old_auto", "1") == "1" else "1")
    elif x == "auto":
        if OLDJOB["run"]:
            await q.message.reply_text("⏳ هناك عملية جارية.")
            return
        m = await q.message.reply_text("⏳ جاري فحص الملازم...", reply_markup=M([[hb("⛔ إيقاف", "k:o:s")]]))
        asyncio.create_task(old_scan(ctx, m.chat_id, m.message_id, "auto"))
        return
    elif x == "ai":
        store.set("old_ai", "0" if store.get("old_ai", "1") == "1" else "1")
    elif x == "c":
        if OLDJOB["run"]:
            await q.message.reply_text("⏳ هناك عملية جارية.")
            return
        m = await q.message.reply_text("⏳ جاري فحص الملازم...", reply_markup=M([[hb("⛔ إيقاف", "k:o:s")]]))
        asyncio.create_task(old_scan(ctx, m.chat_id, m.message_id, "c"))
        return
    elif x == "d":
        if OLDJOB["run"]:
            await q.message.reply_text("⏳ هناك عملية جارية.")
            return
        m = await q.message.reply_text("⏳ جاري فحص الملازم...", reply_markup=M([[hb("⛔ إيقاف", "k:o:s")]]))
        asyncio.create_task(old_scan(ctx, m.chat_id, m.message_id, "d"))
        return
    elif x == "t":
        ctx.user_data["admin_wait"] = "kso:t"
        await q.message.reply_text("🧪 أرسل ملف PDF فيه ختم قديم لأعالجه لك وأرجعه (لا يُحفظ في المكتبة).")
        return
    elif x == "p":
        c = OLDC.get(int(parts[3]))
        if c:
            refs = old_refs()
            refs.append({"s": c["sig"], "n": f"auto{len(refs) + 1}"})
            store.set("old_refs", json.dumps(refs[-40:]))
            await q.message.reply_text(f"✅ اعتُمد ختماً قديماً (المجموع {len(refs)}). بعد اعتماد كل الأشكال اضغط 🔍 فحص.")
        return
    elif x == "n":
        c = OLDC.get(int(parts[3]))
        if c:
            try:
                no = json.loads(store.get("old_no", "") or "[]")
            except Exception:
                no = []
            no.append(c["sig"])
            store.set("old_no", json.dumps(no[-60:]))
            await q.message.reply_text("👍 تم، لن يظهر هذا الشكل مرة ثانية.")
        return
    elif x == "g":
        if not OLDST["tested"]:
            await q.message.reply_text("جرّب على ملف أولاً (🧪 تجربة على ملف) وتأكد أن النتيجة صحيحة.")
            return
        hits = old_hits()
        if not hits:
            await q.message.reply_text("لا توجد ملازم مطابقة في الفحص المحفوظ. شغّل 🔍 فحص أولاً.")
            return
        await q.message.reply_text(f"سيتم تعديل {len(hits)} ملزمة (حذف صفحة الختم القديم" + (" ووضع الختم الجديد" if (stamp_cfg() or {}).get("on") else "") + "). النسخة السابقة تبقى ويمكن التراجع. متأكد؟",
                                   reply_markup=M([[hb("✅ نعم، طبّق", "k:o:g1"), hb("إلغاء", "k:o:m")]]))
        return
    elif x == "g1":
        if OLDJOB["run"] or not OLDST["tested"]:
            await q.message.reply_text("⏳ لا يمكن البدء الآن.")
            return
        m = await q.message.reply_text("⏳ جاري الاستبدال...", reply_markup=M([[hb("⛔ إيقاف", "k:o:s")]]))
        asyncio.create_task(old_apply(ctx, m.chat_id, m.message_id))
        return
    elif x == "s":
        OLDJOB["stop"] = True
        return
    elif x == "u":
        n = hq("SELECT COUNT(*) FROM h_files WHERE COALESCE(prev_fid,'')<>''")[0][0]
        await q.message.reply_text(f"↩️ سيعاد الوضع السابق لـ {n} ملزمة (قبل الاستبدال). متأكد؟", reply_markup=M([[hb("✅ نعم، تراجع", "k:o:u1"), hb("إلغاء", "k:o:m")]]))
        return
    elif x == "u1":
        n = hq("SELECT COUNT(*) FROM h_files WHERE COALESCE(prev_fid,'')<>''")[0][0]
        hq("UPDATE h_files SET file_id=prev_fid, orig_fid=COALESCE(prev_orig,''), stamped=COALESCE(prev_st,0), prev_fid='' WHERE COALESCE(prev_fid,'')<>''", (), False)
        backup_soon()
        await q.message.reply_text(f"↩️ أُعيدت {n} ملزمة لوضعها السابق.")
        return
    t, kb = old_menu()
    try:
        await q.message.edit_text(t, reply_markup=kb)
    except Exception:
        await q.message.reply_text(t, reply_markup=kb)

async def old_input(update, ctx, act):
    msg = update.message
    chat = msg.chat_id
    if act == "kso:a":
        d = msg.document
        cap = (msg.caption or "").strip()
        try:
            if d and ((d.mime_type or "") == "application/pdf" or (d.file_name or "").lower().endswith(".pdf")):
                if cap not in ("0", "1"):
                    ctx.user_data["admin_wait"] = act
                    await msg.reply_text("أعد إرسال الـPDF مع كتابة 1 (الختم أول صفحة) أو 0 (آخر صفحة) في التعليق.")
                    return True
                data = await fetch_file(ctx, d.file_id)
                from pypdf import PdfReader
                r = PdfReader(io.BytesIO(data))
                pg = r.pages[0] if cap == "1" else r.pages[len(r.pages) - 1]
                sig = await asyncio.to_thread(page_sig, pg)
                if not sig:
                    ctx.user_data["admin_wait"] = act
                    await msg.reply_text("هذه الصفحة ليست صورة فقط (فيها نص أو لا صورة قابلة للقراءة). أرسل ملفاً آخر أو صورة الصفحة.")
                    return True
            else:
                fid = msg.photo[-1].file_id if msg.photo else (d.file_id if d and (d.mime_type or "").startswith("image/") else None)
                if not fid:
                    ctx.user_data["admin_wait"] = act
                    await msg.reply_text("أرسل صورة أو PDF.")
                    return True
                data = await fetch_file(ctx, fid)
                from PIL import Image
                sig = await asyncio.to_thread(lambda: img_sig(Image.open(io.BytesIO(data))))
        except Exception as e:
            log.exception("old ref")
            ctx.user_data["admin_wait"] = act
            await msg.reply_text("تعذّرت القراءة (%s: %s)." % (type(e).__name__, str(e)[:100]))
            return True
        refs = old_refs()
        refs.append({"s": sig, "n": f"m{len(refs) + 1}"})
        store.set("old_refs", json.dumps(refs[-40:]))
        ctx.user_data["admin_wait"] = act
        await msg.reply_text(f"✅ أُضيف النموذج (المجموع {len(refs)}). أرسل نموذجاً آخر، أو اضغط رجوع. (صورة الصفحة قد تختلف قليلاً عن صورتها داخل الـPDF، الـPDF أدق.)",
                             reply_markup=M([[hb("🔙 قائمة الأختام القديمة", "k:o:m")]]))
        return True
    if act == "kso:t":
        d = msg.document
        if not d or not ((d.mime_type or "") == "application/pdf" or (d.file_name or "").lower().endswith(".pdf")):
            ctx.user_data["admin_wait"] = act
            await msg.reply_text("أرسل ملف PDF.")
            return True
        if (d.file_size or 0) > MAX_DL:
            await msg.reply_text("الملف أكبر من 20MB.")
            return True
        try:
            data = await fetch_file(ctx, d.file_id)
            n, a, b = await asyncio.to_thread(pdf_sigs, data)
            da, ma = old_match(a)
            db_, mb = old_match(b)
            res = await old_fix_one(ctx, data, stamp_cfg(), live_ai=True)
        except Exception as e:
            log.exception("old test")
            await msg.reply_text("تعذّرت المعالجة (%s: %s)" % (type(e).__name__, str(e)[:100]))
            return True
        if not res:
            fmt = lambda s, d_: ("ليست صورة فقط" if not s else f"فرق {d_}/256")
            await msg.reply_text(f"ما لقيت صفحة ختم/إعلان للحذف (لا تطابق نموذجاً، ولا صفحة نصية فيها @ أو t.me مع محتوى قليل، ولا حكم الذكاء). الصفحة الأولى: {fmt(a, da)} · الأخيرة: {fmt(b, db_)} (المطابقة تحتاج ≤ {OLD_HAM}). أضف نموذجاً أقرب.")
            return True
        cleaned, final, mf, ml = res
        out = final or cleaned
        OLDST["tested"] = True
        await msg.reply_document(InputFile(io.BytesIO(out), filename="old_stamp_test.pdf"),
                                 caption=f"🧪 حُذفت: {'الصفحة الأولى ' if mf else ''}{'الصفحة الأخيرة' if ml else ''} · الصفحات {n} ← {n - int(mf) - int(ml and n > 1)}" + (" · ووُضع الختم الجديد" if final else " · (الختم الجديد معطّل)") + "\nإذا النتيجة صحيحة اضغط ▶️ تطبيق على الكل من القائمة.")
        return True
    return True

async def stamp_input(update, ctx, act):
    msg = update.message
    if act.startswith("kso"):
        return await old_input(update, ctx, act)
    if act.startswith("ksi:"):
        slot = act.split(":")[1]
        if slot == "l" and (msg.text or "").strip() == "-":
            store.set("stamp_img_l", "")
            await msg.reply_text("✅ ستستعمل الصفحة الأخيرة نفس صورة الأولى.")
            t, kb = stamp_menu(); await msg.reply_text(t, reply_markup=kb)
            return True
        fid = None
        if msg.photo:
            fid = msg.photo[-1].file_id
        elif msg.sticker and not getattr(msg.sticker, "is_animated", False) and not getattr(msg.sticker, "is_video", False):
            fid = msg.sticker.file_id
        elif msg.document:
            dn = (msg.document.file_name or "").lower()
            if (msg.document.mime_type or "").startswith("image/") or dn.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic")):
                fid = msg.document.file_id
            else:
                ctx.user_data["admin_wait"] = act
                await msg.reply_text("هذا ليس ملف صورة (%s). أرسل PNG أو JPG." % (msg.document.mime_type or dn or "؟"))
                return True
        if not fid:
            ctx.user_data["admin_wait"] = act
            await msg.reply_text("أرسل صورة (PNG أو JPG) وليس نصاً.")
            return True
        try:
            data = await fetch_file(ctx, fid)
            png = await asyncio.to_thread(prep_stamp_image, data)
        except Exception as e:
            log.exception("stamp image")
            ctx.user_data["admin_wait"] = act
            await msg.reply_text("تعذّرت قراءة الصورة (%s: %s). أرسل صورة PNG أو JPG أخرى." % (type(e).__name__, str(e)[:120]))
            return True
        import base64 as b64
        store.set("stamp_img_" + slot, b64.b64encode(png).decode())
        if slot == "f" and not store.get("stamp_on", ""):
            store.set("stamp_on", "1")
        await msg.reply_text("✅ تم حفظ الصورة.")
        t, kb = stamp_menu(); await msg.reply_text(t, reply_markup=kb)
        return True
    if act == "ksx":
        d = msg.document
        cfg = stamp_cfg()
        if not d or not cfg:
            ctx.user_data["admin_wait"] = act
            await msg.reply_text("أرسل ملف PDF.")
            return True
        if (d.file_size or 0) > MAX_DL:
            await msg.reply_text("الملف أكبر من 20MB.")
            return True
        try:
            data = await fetch_file(ctx, d.file_id)
            out, n = await asyncio.to_thread(stamp_pdf, data, cfg)
            await msg.reply_document(InputFile(io.BytesIO(out), filename="stamped_" + (d.file_name or "test.pdf")), caption=f"🧪 نتيجة التجربة ({n} صفحة مختومة)")
        except Exception as e:
            log.warning("stamp test: %r", e)
            await msg.reply_text("❌ تعذّر ختم هذا الملف.")
        return True
    return False

async def ing_finalize(status, rec, ctx):
    """Save the record if complete, otherwise ask for the missing field (reply to this message)."""
    for need, label in (("stage", "المرحلة (مثال: السادس الإعدادي العلمي)"), ("subject", "المادة (مثال: الفيزياء)")):
        if not rec.get(need):
            rec["need"] = need
            PENDING[status.message_id] = rec
            if need == "stage":
                opts = hq("SELECT id, name FROM h_stages ORDER BY id LIMIT 8")
            else:
                st = get_stage(rec["stage"], create=False)
                opts = hq("SELECT id, name FROM h_subjects WHERE stage_id=? ORDER BY id LIMIT 8", (st,)) if st else []
            kb = M([[hb(trim(n, 30), f"i:p:{i}")] for i, n in opts]) if opts else None
            await status.edit_text(f"{rec.get('note', '')}❓ لم أستطع تحديد {label} للملف:\n📄 {trim(rec['name'], 60)}\n\n↩️ اضغط خياراً أو اكتب الجواب بالرد على هذه الرسالة.", reply_markup=kb)
            return
    rec.pop("need", None)
    sid = get_stage(rec["stage"])
    jid = get_subject(sid, rec["subject"])
    title = compose_title(rec["title"] or stem(rec["name"]), rec["teacher"])
    dup = find_dup(rec["uid"], rec.get("fname", ""), rec.get("fsize", 0))
    if not dup:
        n = nrm(title)
        dup = next(((i, t) for i, t in hq("SELECT id, title FROM h_files WHERE subject_id=?", (jid,)) if nrm(t) == n), None)
    if dup:
        PENDING.pop(status.message_id, None)
        cleanup_empty(jid)
        await status.edit_text(f"⏭ تم تخطيها، موجودة مسبقاً: {dup[1]}")
        return
    rec["note"] = rec.get("note", "") + await old_auto_ingest(ctx, rec)
    rec["note"] = rec.get("note", "") + await apply_stamp(ctx, rec)
    hq("INSERT INTO h_files(subject_id,title,file_id,kind,added,teacher,file_uid,year,quality,fname,fsize,ftype,stamped,orig_fid) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
       (jid, title, rec["fid"], "document", time.time(), rec["teacher"], rec["uid"], rec.get("year", ""), rec.get("quality", ""),
        rec.get("fname", ""), int(rec.get("fsize", 0) or 0), rec.get("type", "") or classify_type(title), int(rec.get("stamped", 0)), rec.get("orig_fid", "")), False)
    backup_soon()
    hid = hq("SELECT id FROM h_files ORDER BY id DESC LIMIT 1")[0][0]
    PENDING.pop(status.message_id, None)
    text, kb = ing_summary(hid)
    await status.edit_text(rec.get("note", "") + text, reply_markup=kb)

def find_dup(uid, fname, fsize):
    """Existing handout id for the same Telegram file, or the same file name and size."""
    if uid:
        r = hq("SELECT id, title FROM h_files WHERE file_uid=? LIMIT 1", (uid,))
        if r:
            return r[0]
    if fname and fsize:
        r = hq("SELECT id, title FROM h_files WHERE fname=? AND fsize=? LIMIT 1", (fname, int(fsize)))
        if r:
            return r[0]
    return None

async def ingest_pdf(update, ctx):
    msg = update.message
    doc = msg.document
    cap = (msg.caption or "").strip()
    status = await msg.reply_text(f"⏳ جاري قراءة: {trim(doc.file_name or 'ملف', 50)}")
    info = {"stage": "", "subject": "", "teacher": "", "title": "", "type": "", "year": "", "quality": ""}
    note = ""
    try:
        async with ING_LOCK:
            d = find_dup(doc.file_unique_id or "", doc.file_name or "", doc.file_size or 0)
            if d:
                await status.edit_text(f"⏭ تم تخطيها، موجودة مسبقاً: {d[1]}")
                return
            try:
                info = await asyncio.wait_for(analyze_pdf(ctx, doc, cap), 100)
            except Exception as e:
                log.warning("ingest analyze failed: %r", e)
                note = "⚠️ تعذرت قراءة الملف بالذكاء الاصطناعي، اعتمدت على اسم الملف.\n"
            g = guess_from_name(doc.file_name or "", cap)
            for k in info:
                if not info[k]:
                    info[k] = g[k]
            rec = dict(info, fid=doc.file_id, uid=doc.file_unique_id or "", name=doc.file_name or "ملزمة", note=note,
                       fname=doc.file_name or "", fsize=doc.file_size or 0)
            await ing_finalize(status, rec, ctx)
    except Exception:
        log.exception("ingest failed")
        try:
            await status.edit_text(f"❌ تعذر إضافة الملف: {trim(doc.file_name or 'ملف', 50)}\nأعد إرساله بعد قليل.")
        except Exception:
            pass


# ---------------- bulk ingest of forwarded channel posts ----------------

from collections import deque
from types import SimpleNamespace as NS
BULK = {}   # chat id -> state of the current/last bulk run (memory only)

def bulk_text(st, final=False):
    tot = max(st["total"], st.get("found", 0))
    t = (f"✅ اكتملت المعالجة ({st['done']}/{tot})" if final else f"⏳ {st['done']}/{tot}")
    if st.get("found"):
        t = "📥 الأرشيف " + t
    t += f" · ✅ {st['ok']} · ⏭ {st['dup']} مكرر"
    if st["stamped"]:
        t += f" · 🖼 {st['stamped']} مختومة"
    if st["nostamp"]:
        t += f" · ⚠️ {st['nostamp']} بدون ختم"
    if st["review"]:
        t += f" · ❓ {len(st['review'])} تحتاج مراجعة"
    if st["fail"]:
        t += f" · ❌ {st['fail']} فشلت"
    if final and st["fail"]:
        t += "\n(الفاشلة: أعد تحويلها لاحقاً)"
    return t

def bulk_kb(st):
    if st["review"]:
        return M([[hb(f"🔎 راجع المشكوك فيها ({len(st['review'])})", "i:n")], [hb("🗑 تجاهلها", "i:z")]])
    return None

async def bulk_edit(ctx, chat, st, final=False):
    if not st["msg"]:
        return
    now = time.time()
    if not final and (now < st["next"] or now - st["last"] < 3):
        return
    try:
        await ctx.bot.edit_message_text(bulk_text(st, final), chat_id=chat, message_id=st["msg"].message_id,
                                        reply_markup=bulk_kb(st) if final else (M([[hb("⏸ إيقاف مؤقت", "w:p"), hb("⛔ إلغاء", "w:s")]]) if st.get("bf") else None))
        st["last"] = time.time()
    except Exception as e:
        ra = getattr(e, "retry_after", None)
        if ra:
            st["next"] = time.time() + float(getattr(ra, "total_seconds", lambda: ra)())  + 1
        elif "not modified" not in str(e).lower():
            log.warning("bulk edit: %r", e)

async def bulk_save(rec, ctx):
    sid = get_stage(rec["stage"])
    jid = get_subject(sid, rec["subject"])
    title = compose_title(rec["title"] or stem(rec["name"]), rec["teacher"])
    dup = find_dup(rec["uid"], rec.get("fname", ""), rec.get("fsize", 0))
    if not dup:
        n = nrm(title)
        same = [(i, t, fn, fs) for i, t, fn, fs in hq("SELECT id, title, fname, fsize FROM h_files WHERE subject_id=?", (jid,)) if nrm(t) == n]
        if same:
            fn, fs = rec.get("fname", ""), int(rec.get("fsize", 0) or 0)
            if any(not (fn and f2 and (fn != f2 or fs != s2)) for _, _, f2, s2 in same):
                dup = same[0]
            else:   # same generic title but clearly a different file: keep it, make the title unique
                title = f"{title} ({trim(stem(fn), 30)})"
    if dup:
        cleanup_empty(jid)
        return "dup"
    await old_auto_ingest(ctx, rec)
    await apply_stamp(ctx, rec)
    hq("INSERT INTO h_files(subject_id,title,file_id,kind,added,teacher,file_uid,year,quality,fname,fsize,ftype,stamped,orig_fid) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
       (jid, title, rec["fid"], "document", time.time(), rec["teacher"], rec["uid"], rec.get("year", ""), rec.get("quality", ""),
        rec.get("fname", ""), int(rec.get("fsize", 0) or 0), rec.get("type", "") or classify_type(title), int(rec.get("stamped", 0)), rec.get("orig_fid", "")), False)
    backup_soon()
    return "ok"

async def bulk_one(item, st, ctx):
    if find_dup(item["uid"], item["name"], item["size"]):
        st["dup"] += 1
        return
    doc = NS(file_id=item["fid"], file_name=item["name"], file_size=item["size"], file_unique_id=item["uid"])
    cap = item["cap"]
    g = guess_from_name(item["name"], cap)
    info = {k: "" for k in ("stage", "subject", "teacher", "title", "type", "year", "quality")}
    lite = st.get("bf") and store.get("gem_lite", "1") == "1" and g["stage"] and g["subject"]
    async with ING_LOCK:
        try:
            if not lite:
                info = await asyncio.wait_for(analyze_pdf(ctx, doc, cap, bulk=True, no_file=bool(g["stage"] and g["subject"])), 90)
        except Exception as e:
            log.warning("bulk analyze failed: %r", e)
    for k in info:
        if not info[k]:
            info[k] = g[k]
    rec = dict(info, fid=item["fid"], uid=item["uid"], name=item["name"] or "ملزمة", note="",
               fname=item["name"], fsize=item["size"])
    if not rec["stage"] or not rec["subject"]:
        st["review"].append(rec)
        return
    r = await bulk_save(rec, ctx)
    log.info("bulk %s %s size=%s stamped=%s fail=%s", r, (rec.get("fname") or "")[:40], rec.get("fsize"), rec.get("stamped"), rec.get("stamp_fail"))
    st["ok" if r == "ok" else "dup"] += 1
    if r == "ok":
        if rec.get("stamped"):
            st["stamped"] += 1
        elif rec.get("stamp_fail"):
            st["nostamp"] += 1

async def bulk_worker(chat, st, ctx):
    try:
        while True:
            while st["q"]:
                item = st["q"].popleft()
                try:
                    await bulk_one(item, st, ctx)
                except Exception:
                    log.exception("bulk item failed")
                    st["fail"] += 1
                st["done"] += 1
                if st["done"] % 4 == 0:
                    await bulk_edit(ctx, chat, st)
                await asyncio.sleep(0.3)
            await asyncio.sleep(st.get("grace", 8))
            if not st["q"]:
                break
    finally:
        st["closed"] = True
        st["running"] = False
        await bulk_edit(ctx, chat, st, final=True)

def bulk_new_state():
    return {"q": deque(), "total": 0, "done": 0, "ok": 0, "dup": 0, "fail": 0, "stamped": 0, "nostamp": 0, "review": [], "msg": None,
            "last": 0.0, "next": 0.0, "closed": False, "init": asyncio.Lock(), "running": False, "found": 0, "bf": False, "grace": 8, "silent": False}

async def bulk_add(update, ctx):
    msg = update.message
    doc = msg.document
    from_watch = bool(W["uid"] and update.effective_user and update.effective_user.id == W["uid"])
    chat = ADMIN_BACKUP_CHAT if from_watch else msg.chat_id
    item = {"fid": doc.file_id, "uid": doc.file_unique_id or "", "name": doc.file_name or "", "size": doc.file_size or 0,
            "cap": (msg.caption or "").strip()[:1500]}
    st = BULK.get(chat)
    if not st or st["closed"]:
        st = bulk_new_state()
        st["silent"] = bool(from_watch)
        if from_watch:
            st["grace"] = 120
        BULK[chat] = st
    st["total"] += 1
    st["q"].append(item)
    async with st["init"]:
        if st["msg"] is None:
            st["msg"] = await ctx.bot.send_message(chat, "⏳ بدأت معالجة الملازم المحوّلة من القناة، سأحدّث هذه الرسالة فقط.",
                                                   disable_notification=bool(st.get("silent")))
    if not st["running"]:
        st["running"] = True
        asyncio.create_task(bulk_worker(chat, st, ctx))

async def bulk_review_cb(q, a, ctx):
    st = BULK.get(q.message.chat_id)
    if a == "z":
        if st:
            st["review"].clear()
        await q.message.edit_text("🗑 تم تجاهل الملفات المشكوك فيها.")
        return
    if not st or not st["review"]:
        await q.message.edit_text("لا توجد ملفات للمراجعة.")
        return
    rec = st["review"].pop(0)
    status = await q.message.reply_text("⏳")
    await ing_finalize(status, rec, ctx)
    left = len(st["review"])
    try:
        if left:
            await q.message.edit_reply_markup(bulk_kb(st))
        else:
            await q.message.edit_reply_markup(None)
    except Exception:
        pass

# ---------------- secondary-account watcher (Telethon) ----------------

import secrets as _secrets
import tornado.web
W = {"client": None, "uid": 0, "uname": "", "chans": {}, "q": deque(), "task": None, "bf": None, "fwd_task": None, "bot_ent": None}
LOGINS = {}          # one-time token -> login state (memory only)
WATCH_ENABLED = True

def _fernet():
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(b"tgsess|" + TOKEN.encode()).digest())
    return Fernet(key)

def tg_save(cfg):
    store.set("tg_cfg", _fernet().encrypt(json.dumps(cfg).encode()).decode() if cfg else "")

def tg_load():
    v = store.get("tg_cfg", "")
    if not v:
        return None
    try:
        return json.loads(_fernet().decrypt(v.encode()).decode())
    except Exception:
        log.warning("tg_cfg cannot be decrypted (bot token changed?)")
        return None

def src_list():
    try:
        return json.loads(store.get("src_list", "") or "[]")
    except Exception:
        return []

def src_save(lst):
    store.set("src_list", json.dumps(lst, ensure_ascii=False))

def bf_get():
    try:
        return json.loads(store.get("bf_state", "") or "{}")
    except Exception:
        return {}

def bf_put(d):
    store.set("bf_state", json.dumps(d))

def is_pdf_msg(m):
    d = getattr(m, "document", None)
    if not d:
        return False
    if (getattr(d, "mime_type", "") or "").lower() == "application/pdf":
        return True
    for a in getattr(d, "attributes", []) or []:
        fn = getattr(a, "file_name", None)
        if fn and fn.lower().endswith(".pdf"):
            return True
    return False

async def watch_notify(text, kb=None):
    try:
        await APP.bot.send_message(ADMIN_BACKUP_CHAT, text, disable_notification=True, reply_markup=kb)
    except Exception:
        log.exception("watch notify")

async def watch_start():
    """Connect the saved session (if any). Never logs in by itself."""
    if not WATCH_ENABLED or W["client"]:
        return False
    cfg = tg_load()
    if not cfg:
        return False
    try:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession
        c = TelegramClient(StringSession(cfg["session"]), int(cfg["api_id"]), cfg["api_hash"],
                           flood_sleep_threshold=120, device_model="TalibBot", system_version="1.0", app_version="1.0")
        await c.connect()
        if not await c.is_user_authorized():
            await c.disconnect()
            await watch_notify("⚠️ جلسة الحساب المراقب لم تعد صالحة. اربط الحساب من جديد من لوحة «قناة مصدر».")
            return False
        me = await c.get_me()
        W.update(client=c, uid=me.id, uname=me.username or me.first_name or str(me.id))
        bu = (await APP.bot.get_me()).username
        W["bot_ent"] = await c.get_entity(bu)
        await watch_resolve()
        c.add_event_handler(watch_on_new, events.NewMessage(incoming=True))
        if not W["fwd_task"]:
            W["fwd_task"] = asyncio.create_task(watch_forwarder())
        bf = bf_get()
        if bf.get("state") == "run":
            W["task"] = asyncio.create_task(bf_run(bf["cid"], resume=True))
        log.info("watcher connected as %s", W["uname"])
        return True
    except Exception:
        log.exception("watch start failed")
        W["client"] = None
        return False

async def watch_resolve():
    c = W["client"]
    W["chans"] = {}
    for s in src_list():
        try:
            W["chans"][s["id"]] = s
        except Exception:
            pass

async def watch_on_new(ev):
    try:
        m = ev.message
        cid = ev.chat_id
        s = W["chans"].get(cid)
        if not s or s.get("paused") or not is_pdf_msg(m):
            return
        W["q"].append((cid, m.id))
    except Exception:
        log.exception("watch handler")

BIG_STAMP_MAX = 60 * 1024 * 1024   # RAM guard on Render free (512MB)
W["pre"] = {}   # fname -> ts : files the watcher already stamped and uploaded (bot must not re-stamp)

async def watch_big(cid, mid):
    """File > 20MB: download via MTProto, stamp, upload to the bot chat as the watcher account.
    Returns True if handled (uploaded stamped), False to fall back to a plain forward."""
    cfg = stamp_cfg()
    if not cfg or not cfg["on"]:
        return False
    c = W["client"]
    m = await c.get_messages(cid, ids=mid)
    d = getattr(m, "document", None)
    if not m or not d or d.size <= MAX_DL or d.size > BIG_STAMP_MAX:
        return False
    fname = "handout.pdf"
    for a in d.attributes or []:
        if getattr(a, "file_name", None):
            fname = a.file_name
    try:
        async with STAMP_LOCK:
            data = await asyncio.wait_for(c.download_media(m, file=bytes), 600)
            out, n = await asyncio.wait_for(asyncio.to_thread(stamp_pdf, data, cfg), 240)
            del data
            bio = io.BytesIO(out)
            bio.name = fname
            W["pre"][fname] = time.time()
            await c.send_file(W["bot_ent"], bio, caption=(m.message or ""), force_document=True,
                              attributes=[a for a in (d.attributes or []) if getattr(a, "file_name", None)],
                              mime_type="application/pdf")
            del out, bio
        log.info("big stamped %s", fname)
        return True
    except Exception:
        log.exception("big stamp failed")
        W["pre"].pop(fname, None)
        return False

async def watch_forward(cid, ids):
    """Forward posts to the bot chat; returns False when forwards are blocked for this channel."""
    from telethon import errors
    c = W["client"]
    try:
        cfg = stamp_cfg()
        if cfg and cfg["on"]:
            msgs = await c.get_messages(cid, ids=list(ids))
            rest = []
            for m in msgs:
                d = getattr(m, "document", None)
                if m and d and MAX_DL < d.size <= BIG_STAMP_MAX:
                    if not await watch_big(cid, m.id):
                        rest.append(m.id)
                else:
                    rest.append(m.id if m else None)
            ids = [i for i in rest if i]
            if not ids:
                return True
    except Exception:
        log.exception("big split")
    for attempt in range(3):
        try:
            await c.forward_messages(W["bot_ent"], ids, from_peer=cid)
            return True
        except errors.FloodWaitError as e:
            log.warning("flood wait %ss", e.seconds)
            await asyncio.sleep(e.seconds + 5)
        except errors.ChatForwardsRestrictedError:
            return False
        except Exception as e:
            log.warning("forward failed: %r", e)
            await asyncio.sleep(10)
    return True

def bot_queue_len():
    st = BULK.get(ADMIN_BACKUP_CHAT)
    return len(st["q"]) if st and not st["closed"] else 0

async def watch_forwarder():
    """Live posts: small batches, paced."""
    while True:
        await asyncio.sleep(6)
        try:
            if not W["client"] or not W["q"]:
                continue
            by = {}
            while W["q"] and sum(len(v) for v in by.values()) < 10:
                cid, mid = W["q"].popleft()
                by.setdefault(cid, []).append(mid)
            for cid, ids in by.items():
                ok = await watch_forward(cid, ids)
                if not ok:
                    await watch_notify("⚠️ هذه القناة تمنع التحويل (حفظ المحتوى مقيّد)، لا يمكن نسخ منشوراتها.")
                await asyncio.sleep(4)
        except Exception:
            log.exception("watch forwarder")

async def bf_run(cid, resume=False):
    """Archive copy: document posts only, resumable by saved cursor, throttled with backpressure."""
    from telethon.tl.types import InputMessagesFilterDocument
    bf = bf_get()
    c = W["client"]
    if not c:
        return
    try:
        bf.update(cid=cid, state="run")
        bf_put(bf)
        ent = await c.get_entity(cid)
        oldest_first = bf.get("dir", "old") == "old"
        total = (await c.get_messages(ent, limit=0, filter=InputMessagesFilterDocument)).total
        st = BULK.get(ADMIN_BACKUP_CHAT)
        if not st or st["closed"]:
            st = bulk_new_state()
            BULK[ADMIN_BACKUP_CHAT] = st
            st["silent"] = True
        st["found"] = total
        st["bf"] = True
        st["grace"] = 120
        cursor = int(bf.get("cursor", 0) or 0)
        kw = {"reverse": True, "min_id": cursor} if oldest_first else ({"max_id": cursor} if cursor else {})
        batch, last = [], cursor
        async def flush():
            nonlocal batch, last
            if not batch:
                return True
            ok = await watch_forward(cid, batch)
            if not ok:
                bf.update(state="blocked")
                bf_put(bf)
                await watch_notify("⛔ القناة تمنع التحويل (حفظ المحتوى مقيّد). توقف النسخ.")
                return False
            last = batch[-1] if oldest_first else batch[-1]
            bf["cursor"] = last
            bf["sent"] = int(bf.get("sent", 0)) + len(batch)
            bf_put(bf)
            batch = []
            await asyncio.sleep(25)
            while bot_queue_len() > 25:
                if bf_get().get("state") != "run":
                    break
                await asyncio.sleep(10)
            return True
        async for m in c.iter_messages(ent, filter=InputMessagesFilterDocument, wait_time=2, **kw):
            if bf_get().get("state") != "run":
                break
            while bf_get().get("state") == "pause":
                await asyncio.sleep(10)
            if not is_pdf_msg(m):
                continue
            batch.append(m.id)
            if len(batch) >= 15:
                if not await flush():
                    return
        if bf_get().get("state") == "run":
            await flush()
            bf = bf_get()
            bf.update(state="done")
            bf_put(bf)
            await watch_notify(f"✅ انتهى نسخ أرشيف القناة (أُرسل {bf.get('sent', 0)} منشور للمعالجة).")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("backfill failed")
        bf = bf_get()
        bf["state"] = "error"
        bf_put(bf)
        await watch_notify("⚠️ توقف نسخ الأرشيف بسبب خطأ. اضغط استئناف من لوحة «قناة مصدر».")
    finally:
        st = BULK.get(ADMIN_BACKUP_CHAT)
        if st:
            st["bf"] = False
            st["found"] = 0

# ---- web login ----

LOGIN_CSS = ("<meta name=viewport content='width=device-width,initial-scale=1'><meta charset=utf-8>"
             "<style>body{font-family:system-ui,'Noto Sans Arabic',sans-serif;background:#FFF6E5;direction:rtl;padding:20px;color:#2b2140}"
             ".c{max-width:420px;margin:auto;background:#fff;border-radius:24px;padding:22px;box-shadow:0 6px 0 #D9D0FF}"
             "h2{color:#7C5CFC;margin-top:0}input{width:100%;padding:12px;margin:6px 0 14px;border:2px solid #D9D0FF;border-radius:14px;font-size:16px;direction:ltr;text-align:left}"
             "button{width:100%;padding:14px;border:0;border-radius:16px;background:#7C5CFC;color:#fff;font-size:17px;font-weight:700}"
             ".e{background:#FFE3E3;padding:10px;border-radius:12px;margin-bottom:12px}small{color:#777}</style>")

def login_page(body, err=""):
    e = f"<div class=e>{html_esc(err)}</div>" if err else ""
    return f"<!doctype html><html lang=ar>{LOGIN_CSS}<div class=c>{e}{body}</div></html>"

def html_esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def sent_type_text(t):
    n = type(t).__name__ if t is not None else ""
    return {"SentCodeTypeApp": "داخل تطبيق تيليگرام (محادثة Telegram) على جهاز مسجّل بنفس الحساب",
            "SentCodeTypeSms": "رسالة SMS", "SentCodeTypeCall": "مكالمة صوتية", "SentCodeTypeFlashCall": "مكالمة فلاش",
            "SentCodeTypeMissedCall": "مكالمة فائتة (آخر أرقام الرقم هي الكود)", "SentCodeTypeFirebaseSms": "SMS عبر التطبيق",
            "SentCodeTypeEmailCode": "بريد إلكتروني", "CodeTypeSms": "رسالة SMS", "CodeTypeCall": "مكالمة صوتية", "CodeTypeFlashCall": "مكالمة فلاش", "CodeTypeMissedCall": "مكالمة فائتة", "CodeTypeFragmentSms": "SMS عبر Fragment"}.get(n, n or "غير معروف")

def login_form(step, st=None):
    st = st or {}
    if step == "start":
        return ("<h2>🔑 ربط الحساب المراقب</h2><p><small>استعمل بيانات الحساب الثاني فقط. هذه الصفحة تعمل مرة واحدة ولا تُحفظ البيانات إلا مشفّرة.</small></p>"
                "<form method=post><input type=hidden name=a value=start>api_id<input name=api_id inputmode=numeric required>"
                "api_hash<input name=api_hash required autocomplete=off>رقم الهاتف مع المفتاح (+964...) <small>(للكود فقط، يُترك فارغاً مع QR)</small><input name=phone>"
                "<button name=m value=code>إرسال الكود</button><div style='height:10px'></div>"
                "<button name=m value=qr style='background:#2BB673'>🔳 الدخول عبر QR (بدون كود)</button></form>")
    if step == "code":
        nxt = st.get("next_t")
        info = "طريقة الإرسال: <b>" + html_esc(sent_type_text(st.get("sent_t"))) + "</b>"
        if st.get("timeout"):
            info += f"<br>مدة الصلاحية للتالي: {int(st['timeout'])} ثانية"
        if nxt:
            info += "<br>الطريقة التالية: " + html_esc(sent_type_text(nxt))
        rs = ""
        if nxt:
            rs = ("<form method=post><input type=hidden name=a value=resend><button style='background:#F59E0B'>↻ إعادة الإرسال بالطريقة التالية</button></form><div style='height:10px'></div>")
        return ("<h2>📩 أدخل الكود</h2><p><small>" + info + "</small></p>"
                "<form method=post><input type=hidden name=a value=code>الكود<input name=code inputmode=numeric required autocomplete=off><button>تأكيد</button></form><div style='height:10px'></div>"
                + rs + "<form method=post><input type=hidden name=a value=qr><button style='background:#2BB673'>🔳 جرّب QR بدلاً من الكود</button></form>")
    if step == "qr":
        svg = ""
        try:
            import segno, io as _io
            bio = _io.BytesIO()
            segno.make(st["qr"].url, error="m").save(bio, kind="svg", scale=5, border=2, xmldecl=False, svgns=True)
            svg = bio.getvalue().decode()
        except Exception:
            svg = "<p>تعذّر رسم QR</p>"
        return ("<meta http-equiv=refresh content=2><h2>🔳 دخول عبر QR</h2><p><small>من الحساب الثاني في تيليگرام: الإعدادات ← الأجهزة ← ربط جهاز سطح مكتب، ثم امسح الرمز. (الصفحة تتحدّث تلقائياً)</small></p>"
                "<div style='background:#fff;text-align:center;direction:ltr'>" + svg + "</div>"
                "<p><small>رابط هذه الشاشة لفتحها على جهاز ثاني: <span style='direction:ltr;display:block;word-break:break-all'>" + html_esc(st.get("qurl", "")) + "</span></small></p>"
                "<p><a href='" + html_esc(st["qr"].url) + "'>أو اضغط هنا إن كنت تفتح الصفحة من نفس الهاتف</a></p>")
    if step == "pw":
        return ("<h2>🔐 كلمة سر التحقق بخطوتين</h2><form method=post><input type=hidden name=a value=pw>"
                "<input name=pw type=password required autocomplete=off><button>تأكيد</button></form>")
    return "<h2>✅ تم ربط الحساب</h2><p>يمكنك إغلاق هذه الصفحة والعودة إلى البوت.</p>"

async def qr_begin(st):
    st["qr"] = await st["client"].qr_login()
    st["step"] = "qr"
    if not st.get("qtok"):
        st["qtok"] = _secrets.token_urlsafe(24)
        LOGINS[st["qtok"]] = st
        st["exp"] = max(st["exp"], time.time() + 900)
        base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
        st["qurl"] = f"{base}/tglqr/{st['qtok']}"
        try:
            await watch_notify("🔳 رابط QR (افتحه على جهاز ثاني مثل الكمبيوتر، ثم امسحه من تيليگرام الحساب الثاني: الإعدادات ← الأجهزة ← ربط جهاز). يعمل 15 دقيقة، لا تشاركه:\n" + st["qurl"], None)
        except Exception:
            log.exception("qr notify")

def login_drop(st):
    for k in [k for k, v in LOGINS.items() if v is st]:
        LOGINS.pop(k, None)

class TgLoginH(tornado.web.RequestHandler):
    qr_only = False
    def prepare(self):
        self.set_header("Cache-Control", "no-store")
        self.set_header("Referrer-Policy", "no-referrer")
        self.set_header("X-Robots-Tag", "noindex")
        self.st = LOGINS.get(self.path_args[0] if self.path_args else "")
        if self.qr_only and self.st and self.st.get("qtok") != self.path_args[0]:
            self.st = None
        if not self.st or self.st["exp"] < time.time():
            LOGINS.pop(self.path_args[0] if self.path_args else "", None)
            self.set_status(404)
            self.finish(login_page("<h2>انتهت صلاحية الرابط</h2><p>اطلب رابطاً جديداً من لوحة الأدمن.</p>"))

    async def get(self, token):
        st = self.st
        if st["step"] == "qr" and not self.qr_only and st.get("qtok"):
            self.finish(login_page(login_form("qr", st)))
            return
        if st["step"] == "qr":
            qr = st["qr"]
            lock = st.setdefault("lock", asyncio.Lock())
            res = "wait"
            try:
                async with lock:
                    try:
                        await qr.wait(timeout=1.2)
                        res = "ok"
                    except asyncio.TimeoutError:
                        if qr.expires and qr.expires.timestamp() < time.time() + 10:
                            await qr.recreate()
                    except Exception as e:
                        if type(e).__name__ == "SessionPasswordNeededError":
                            res = "pw"
                        else:
                            raise
                if res == "pw":
                    st["step"] = "pw"
                    self.finish(login_page(login_form("pw", st)))
                    return
                if res == "ok" and await st["client"].is_user_authorized():
                    await login_done(st)
                    login_drop(st)
                    self.finish(login_page(login_form("done")))
                    return
            except Exception as e:
                log.warning("qr step failed: %r", e)
                st["step"] = "start"
                msg = "فشل الدخول عبر QR: " + type(e).__name__
                self.finish(login_page("<h2>تعذّر الدخول</h2><p>ارجع لصفحة الإدخال وابدأ من جديد.</p>" if self.qr_only else login_form("start"), msg))
                return
        self.finish(login_page(login_form(st["step"], st)))

    async def post(self, token):
        st = self.st
        st["tries"] += 1
        if st["tries"] > 12:
            LOGINS.pop(token, None)
            self.finish(login_page("<h2>محاولات كثيرة</h2>"))
            return
        a = self.get_body_argument("a", "")
        try:
            from telethon import TelegramClient, errors
            from telethon.sessions import StringSession
            if a == "start" and st["step"] == "start":
                api_id = int(self.get_body_argument("api_id").strip())
                api_hash = self.get_body_argument("api_hash").strip()
                phone = self.get_body_argument("phone", "").strip().replace(" ", "")
                c = TelegramClient(StringSession(), api_id, api_hash, device_model="TalibBot")
                await c.connect()
                st.update(client=c, api_id=api_id, api_hash=api_hash, phone=phone)
                if self.get_body_argument("m", "code") == "qr":
                    await qr_begin(st)
                    self.finish(login_page(login_form("qr", st)))
                    return
                if not phone:
                    st["step"] = "start"
                    raise ValueError("nophone")
                sent = await c.send_code_request(phone)
                st.update(hash=sent.phone_code_hash, step="code", sent_t=sent.type, next_t=getattr(sent, "next_type", None), timeout=getattr(sent, "timeout", None), resends=0)
                log.info("tg code sent: type=%s next=%s timeout=%s", type(sent.type).__name__, type(getattr(sent, "next_type", None)).__name__, getattr(sent, "timeout", None))
            elif a == "resend" and st["step"] == "code":
                from telethon.tl.functions.auth import ResendCodeRequest
                if st.get("resends", 0) >= 2 or time.time() - st.get("last_resend", 0) < 30:
                    raise ValueError("resend limit")
                st["last_resend"] = time.time()
                st["resends"] = st.get("resends", 0) + 1
                sent = await st["client"](ResendCodeRequest(st["phone"], st["hash"]))
                st.update(hash=sent.phone_code_hash, sent_t=sent.type, next_t=getattr(sent, "next_type", None), timeout=getattr(sent, "timeout", None))
                log.info("tg code resent: type=%s next=%s", type(sent.type).__name__, type(getattr(sent, "next_type", None)).__name__)
                self.finish(login_page(login_form("code", st)))
                return
            elif a == "qr" and st["step"] == "code":
                await qr_begin(st)
                self.finish(login_page(login_form("qr", st)))
                return
            elif a == "code" and st["step"] == "code":
                try:
                    await st["client"].sign_in(st["phone"], self.get_body_argument("code").strip().replace(" ", ""), phone_code_hash=st["hash"])
                except errors.SessionPasswordNeededError:
                    st["step"] = "pw"
                    self.finish(login_page(login_form("pw", st)))
                    return
            elif a == "pw" and st["step"] == "pw":
                await st["client"].sign_in(password=self.get_body_argument("pw"))
            else:
                self.finish(login_page(login_form(st["step"], st)))
                return
            if st["step"] != "code" or await st["client"].is_user_authorized():
                if await st["client"].is_user_authorized():
                    await login_done(st)
                    login_drop(st)
                    self.finish(login_page(login_form("done")))
                    return
            self.finish(login_page(login_form(st["step"], st)))
        except Exception as e:
            log.warning("tg login step failed: %r", e)
            msg = {"PhoneCodeInvalidError": "الكود غير صحيح", "PhoneCodeExpiredError": "انتهى الكود، ابدأ من جديد",
                   "PasswordHashInvalidError": "كلمة السر غير صحيحة", "ApiIdInvalidError": "api_id أو api_hash غير صحيح",
                   "PhoneNumberInvalidError": "رقم الهاتف غير صحيح", "FloodWaitError": "انتظر قليلاً ثم أعد المحاولة (حدّ تيليگرام): " + str(getattr(e, "seconds", "")) + "ث",
                   "ValueError": "أدخل رقم الهاتف، أو انتظر 30 ثانية قبل إعادة الإرسال (حد أقصى مرتان)",
                   "PhoneCodeHashEmptyError": "ابدأ من جديد", "SendCodeUnavailableError": "لا توجد طريقة إرسال أخرى متاحة، جرّب QR",
                   "PhonePasswordFloodError": "محاولات كثيرة، انتظر ساعات أو جرّب QR"}.get(type(e).__name__, "تعذّرت العملية، تحقق من البيانات")
            self.finish(login_page(login_form(st["step"]), msg))

class TgQrH(TgLoginH):
    qr_only = True
    async def post(self, token):
        self.set_status(405)
        self.finish()

async def login_done(st):
    c = st["client"]
    sess = c.session.save()
    me = await c.get_me()
    await c.disconnect()
    tg_save({"api_id": st["api_id"], "api_hash": st["api_hash"], "session": sess, "uid": me.id})
    if W["client"]:
        try:
            await W["client"].disconnect()
        except Exception:
            pass
        W["client"] = None
    ok = await watch_start()
    await watch_notify(f"✅ تم ربط الحساب المراقب ({'@' + me.username if me.username else me.first_name}). " + ("متصل." if ok else "تعذّر الاتصال، راجع السجلات."))

def watch_status_text():
    cfg_ok = bool(W["client"])
    t = "➕ قنوات المصدر (الحساب المراقب)\n"
    t += f"الحساب: {'✅ ' + str(W['uname']) if cfg_ok else ('⚠️ مربوط لكن غير متصل' if tg_load() else '❌ غير مربوط')}\n\n"
    lst = src_list()
    bf = bf_get()
    if not lst:
        t += "لا توجد قنوات بعد."
    for s in lst:
        flag = "⏸" if s.get("paused") else "🟢"
        t += f"{flag} @{s['u']}"
        if bf.get("cid") == s["id"] and bf.get("state"):
            t += {"run": " · 📥 النسخ جارٍ", "pause": " · ⏸ النسخ متوقف مؤقتاً", "done": " · ✅ تم نسخ الأرشيف", "blocked": " · ⛔ التحويل ممنوع", "error": " · ⚠️ توقف بخطأ", "stop": " · ⛔ أُوقف"}.get(bf["state"], "") + f" ({bf.get('sent', 0)} أُرسل)"
        t += "\n"
    return t

def watch_menu():
    kb = [[hb("🔑 ربط الحساب" if not W["client"] else "🔑 إعادة الربط", "w:l")]]
    if W["client"]:
        kb.append([hb("➕ إضافة قناة", "w:a")])
    bf = bf_get()
    for s in src_list():
        row = [hb(f"📥 أرشيف @{s['u']}"[:30], f"w:b:{s['id']}"), hb("▶" if s.get("paused") else "⏸", f"w:t:{s['id']}"), hb("🗑", f"w:d:{s['id']}")]
        kb.append(row)
    if bf.get("state") == "run":
        kb.append([hb("⏸ إيقاف مؤقت للنسخ", "w:p"), hb("⛔ إلغاء النسخ", "w:s")])
    elif bf.get("state") in ("pause", "error"):
        kb.append([hb("▶ استئناف النسخ", "w:r"), hb("⛔ إلغاء النسخ", "w:s")])
    kb.append([hb(("✅ " if store.get("gem_lite", "1") == "1" else "⬜ ") + "الذكاء فقط عند الحاجة (للأرشيف)", "w:g")])
    if tg_load():
        kb.append([hb("🔌 فصل الحساب", "w:x")])
    kb.append([hb("🔙 رجوع", "a:main")])
    return M(kb)

async def watch_cb(update, ctx):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("للأدمن فقط", show_alert=True)
        return
    parts = q.data.split(":")
    a = parts[1]
    await q.answer()
    if a == "l":
        tok = _secrets.token_urlsafe(24)
        LOGINS[tok] = {"exp": time.time() + 900, "step": "start", "tries": 0}
        base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
        await q.message.reply_text("🔑 رابط ربط الحساب (يعمل مرة واحدة ولمدة 15 دقيقة، لا تشاركه):\n" + f"{base}/tglogin/{tok}\n\nأدخل البيانات في الصفحة وليس في المحادثة.",
                                   disable_web_page_preview=True)
        return
    if a == "a":
        ctx.user_data["admin_wait"] = "wadd"
        await q.message.reply_text("أرسل @username للقناة العامة (مثال: @mychannel).")
        return
    if a == "b":
        cid = int(parts[2])
        await q.message.edit_text("📥 نسخ كل الأرشيف: بأي ترتيب؟", reply_markup=M([
            [hb("⬆️ من الأقدم للأحدث", f"w:bo:{cid}:old"), hb("⬇️ من الأحدث للأقدم", f"w:bo:{cid}:new")], [hb("🔙 رجوع", "w:m")]]))
        return
    if a == "bo":
        if not W["client"]:
            await q.message.reply_text("اربط الحساب أولاً.")
            return
        if W["task"] and not W["task"].done():
            await q.message.reply_text("هناك نسخ جارٍ بالفعل.")
            return
        cid, d = int(parts[2]), parts[3]
        bf_put({"cid": cid, "dir": d, "cursor": 0, "sent": 0, "state": "run"})
        W["task"] = asyncio.create_task(bf_run(cid))
        await q.message.reply_text("📥 بدأ نسخ الأرشيف. سأحدّث رسالة تقدّم واحدة صامتة.")
    elif a in ("p", "r", "s"):
        bf = bf_get()
        if a == "p" and bf.get("state") == "run":
            bf["state"] = "pause"
        elif a == "r" and bf.get("state") in ("pause", "error"):
            bf["state"] = "run"
            bf_put(bf)
            if not W["task"] or W["task"].done():
                W["task"] = asyncio.create_task(bf_run(bf["cid"], resume=True))
        elif a == "s":
            bf["state"] = "stop"
        bf_put(bf)
    elif a == "g":
        store.set("gem_lite", "0" if store.get("gem_lite", "1") == "1" else "1")
    elif a == "t":
        lst = src_list()
        for s in lst:
            if s["id"] == int(parts[2]):
                s["paused"] = 0 if s.get("paused") else 1
        src_save(lst)
        await watch_resolve()
    elif a == "d":
        src_save([s for s in src_list() if s["id"] != int(parts[2])])
        await watch_resolve()
    elif a == "x":
        if W["client"]:
            try:
                await W["client"].log_out()
            except Exception:
                pass
            W["client"] = None
        tg_save(None)
    t, kb = watch_status_text(), watch_menu()
    try:
        await q.message.edit_text(t, reply_markup=kb)
    except Exception:
        await q.message.reply_text(t, reply_markup=kb)

async def watch_input(update, ctx, act):
    msg = update.message
    if act == "wadd":
        txt = (msg.text or "").strip()
        m = re.search(r"(?:t\.me/|@)?([A-Za-z][A-Za-z0-9_]{3,31})", txt)
        if not m or not W["client"]:
            await msg.reply_text("اربط الحساب أولاً، ثم أرسل @username صحيحاً.")
            return True
        uname = m.group(1)
        try:
            from telethon.tl.functions.channels import JoinChannelRequest
            from telethon import utils
            ent = await W["client"].get_entity(uname)
            if not getattr(ent, "broadcast", False):
                await msg.reply_text("هذا ليس قناة عامة.")
                return True
            cid = utils.get_peer_id(ent)
            try:
                await W["client"](JoinChannelRequest(ent))
            except Exception as e:
                log.warning("join: %r", e)
            lst = [s for s in src_list() if s["id"] != cid] + [{"id": cid, "u": uname, "title": getattr(ent, "title", ""), "paused": 0}]
            src_save(lst)
            await watch_resolve()
            await msg.reply_text(f"✅ أُضيفت @{uname}. المنشورات الجديدة تُنسخ تلقائياً. لنسخ الأرشيف استعمل «📥 أرشيف».")
        except Exception as e:
            log.warning("add source: %r", e)
            await msg.reply_text("تعذّر العثور على القناة. تأكد من الاسم وأنها عامة.")
        await msg.reply_text(watch_status_text(), reply_markup=watch_menu())
        return True
    return False

async def ing_reply(update, ctx):
    msg = update.message
    rep = msg.reply_to_message
    rec = PENDING.get(rep.message_id) if rep else None
    if not rec or not rec.get("need") or not msg.text:
        return False
    rec[rec["need"]] = msg.text.strip()[:80]
    await ing_finalize(rep, rec, ctx)
    return True

async def ing_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("للأدمن فقط", show_alert=True)
        return
    parts = q.data.split(":")
    a = parts[1]
    await q.answer()
    if a in ("n", "z"):
        await bulk_review_cb(q, a, ctx)
        return
    if a == "p":
        rec = PENDING.get(q.message.message_id)
        need = rec.get("need") if rec else None
        if not need:
            await q.message.edit_text("انتهت صلاحية هذا الطلب، أعد إرسال الملف.")
            return
        tbl = "h_stages" if need == "stage" else "h_subjects"
        r = hq(f"SELECT name FROM {tbl} WHERE id=?", (int(parts[2]),))
        if r:
            rec[need] = r[0][0]
            await ing_finalize(q.message, rec, ctx)
        return
    hid = int(parts[2])
    if a == "b":
        text, kb = ing_summary(hid)
        if not text:
            await q.message.edit_text("🗑 هذه الملزمة محذوفة.")
            return
        await q.message.edit_text(text, reply_markup=kb)
    elif a == "e":
        await q.message.edit_reply_markup(M([
            [hb("✏️ العنوان", f"i:w:title:{hid}"), hb("👨‍🏫 المعلم", f"i:w:teacher:{hid}")],
            [hb("🎓 المرحلة", f"i:w:stage:{hid}"), hb("📘 المادة", f"i:w:subject:{hid}")],
            [hb("📅 السنة", f"i:w:year:{hid}"), hb("🔎 الدقة", f"i:w:quality:{hid}")],
            [hb("🗂 النوع", f"i:w:type:{hid}")],
            [hb("🔙 رجوع", f"i:b:{hid}")]]))
    elif a == "w":
        field, hid = parts[2], int(parts[3])
        ctx.user_data["admin_wait"] = f"ie:{field}:{hid}"
        names = {"title": "العنوان الجديد", "teacher": "اسم المعلم الجديد", "stage": "اسم المرحلة الجديد", "subject": "اسم المادة الجديد", "year": "السنة (مثل 2027، أو - للحذف)", "quality": "الدقة (مثل سكنر، أو - للحذف)", "type": "النوع (ملازم، وزاريات، أسئلة، ملخصات، اختبارات، أو أي اسم)"}
        await q.message.reply_text(f"✏️ أرسل {names[field]}:")
    elif a == "d":
        r = hq("SELECT title FROM h_files WHERE id=?", (hid,))
        if not r:
            await q.message.edit_text("🗑 هذه الملزمة محذوفة.")
            return
        await q.message.edit_text(f"⚠️ حذف «{r[0][0]}» نهائياً؟",
                                  reply_markup=M([[hb("🗑 نعم، احذف", f"i:x:{hid}"), hb("↩️ تراجع", f"i:b:{hid}")]]))
    elif a == "x":
        r = hq("SELECT title, subject_id FROM h_files WHERE id=?", (hid,))
        if r:
            hq("DELETE FROM h_files WHERE id=?", (hid,), False)
            cleanup_empty(r[0][1])
            backup_soon()
            await q.message.edit_text(f"🗑 تم حذف: {r[0][0]}")
        else:
            await q.message.edit_text("🗑 هذه الملزمة محذوفة.")

async def ing_edit_input(update, ctx, act):
    msg = update.message
    _, field, hid = act.split(":")
    hid = int(hid)
    text = (msg.text or "").strip()[:100]
    r = hq("SELECT title, teacher, subject_id FROM h_files WHERE id=?", (hid,))
    if not text or not r:
        await msg.reply_text("أرسل نصاً صحيحاً، أو الملزمة محذوفة.")
        return True
    title, teacher, jid = r[0]
    if not teacher:
        e = eff_teacher("", title)
        teacher = "" if e == GENERAL else e
    if field == "type":
        hq("UPDATE h_files SET ftype=? WHERE id=?", ("" if text == "-" else text, hid), False)
    elif field in ("year", "quality"):
        hq(f"UPDATE h_files SET {field}=? WHERE id=?", ("" if text == "-" else text, hid), False)
    elif field == "title":
        hq("UPDATE h_files SET title=? WHERE id=?", (text, hid), False)
    elif field == "teacher":
        if teacher and teacher in title:
            title = title.replace(teacher, text)
        else:
            title = compose_title(title, text)
        hq("UPDATE h_files SET title=?, teacher=? WHERE id=?", (title, text, hid), False)
    else:
        sid, subj = hq("SELECT stage_id, name FROM h_subjects WHERE id=?", (jid,))[0]
        if field == "stage":
            sid = get_stage(text)
        else:
            subj = text
        new_j = get_subject(sid, subj)
        hq("UPDATE h_files SET subject_id=? WHERE id=?", (new_j, hid), False)
        if new_j != jid:
            cleanup_empty(jid)
    backup_soon()
    t, kb = ing_summary(hid)
    await msg.reply_text("تم التعديل:\n" + t, reply_markup=kb)
    return True

# ---------------- main ----------------

async def keepalive():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return
    await asyncio.sleep(60)
    async with httpx.AsyncClient(timeout=30) as c:
        while True:
            try:
                await c.get(url + "/health")
            except Exception as e:
                log.warning("keepalive failed: %s", e)
            await asyncio.sleep(540)

async def list_models():
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": GKEY, "pageSize": 100})
        if r.status_code != 200:
            log.warning("LISTMODELS %s: %s", r.status_code, r.text[:200])
            return
        names = [m["name"].replace("models/", "") for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        log.info("LISTMODELS ok: %s", ",".join(names))
        # probe each fallback once so logs show real status codes
        async with httpx.AsyncClient(timeout=60) as c:
            for m in [MODEL] + FALLBACKS:
                try:
                    pr = await c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                                      params={"key": GKEY}, json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                                      "generationConfig": {"maxOutputTokens": 16}})
                    log.info("PROBE %s -> %s", m, pr.status_code)
                except Exception as e:
                    log.info("PROBE %s -> error %s", m, e)
    except Exception as e:
        log.warning("LISTMODELS error %s", e)

async def post_init(app):
    global store, APP
    APP = app
    await restore_backup(app)
    store = Store(DB_PATH)
    old_db_init()
    asyncio.create_task(backup_loop())
    asyncio.create_task(keepalive())
    async def _ws():
        await asyncio.sleep(20)
        await watch_start()
    asyncio.create_task(_ws())

async def on_error(update, ctx):
    log.warning("handler error: %r", ctx.error)

def build_app():
    app = Application.builder().token(TOKEN).concurrent_updates(True).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^a:"))
    app.add_handler(CallbackQueryHandler(verify_cb, pattern=r"^g:verify$"))
    app.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(handout_cb, pattern=r"^h:"))
    app.add_handler(CallbackQueryHandler(ing_cb, pattern=r"^i:"))
    app.add_handler(CallbackQueryHandler(stamp_cb, pattern=r"^k:"))
    app.add_handler(CallbackQueryHandler(watch_cb, pattern=r"^w:"))
    app.add_handler(CallbackQueryHandler(quiz_cb, pattern=r"^q"))
    app.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, drop_pin_notice))
    app.add_handler(MessageHandler(~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    return app

# ---------------- web app (Telegram Mini App) ----------------

import tornado.web

WEBAPP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp.html")

def check_init(raw):
    """Validate Telegram WebApp initData. Returns user dict or None."""
    try:
        d = dict(parse_qsl(raw, keep_blank_values=True))
        got = d.pop("hash", "")
        dcs = "\n".join(f"{k}={d[k]}" for k in sorted(d))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
        if not got or not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(d.get("auth_date", "0")) > 86400:
            return None
        return json.loads(d["user"])
    except Exception:
        return None

class PageH(tornado.web.RequestHandler):
    def get(self):
        with open(WEBAPP_FILE, "rb") as f:
            body = f.read()
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.set_header("Cache-Control", "no-store")
        self.write(body)

class HealthH(tornado.web.RequestHandler):
    def get(self):
        self.write("ok")

class WebhookH(tornado.web.RequestHandler):
    async def post(self):
        update = Update.de_json(json.loads(self.request.body), self.application.settings["tgapp"].bot)
        await self.application.settings["tgapp"].update_queue.put(update)
        self.write("ok")

class ApiH(tornado.web.RequestHandler):
    def fail(self, code, msg):
        self.set_status(code)
        self.write({"error": msg})

    async def post(self, name):
        tgapp = self.application.settings["tgapp"]
        user = check_init(self.request.headers.get("X-Init-Data", ""))
        if not user:
            return self.fail(401, "افتح التطبيق من داخل البوت")
        uid = user["id"]
        adm = uid in ADMIN_IDS or (user.get("username") or "").lower() in ADMIN_USERNAMES
        if not adm:
            if store.is_banned(uid):
                return self.fail(403, "تم حظرك من استخدام البوت")
            if store.on("maintenance"):
                return self.fail(503, "البوت في وضع الصيانة حالياً")
        try:
            body = json.loads(self.request.body or b"{}")
        except Exception:
            body = {}
        if name == "library":
            out = []
            for sid, sname in store.q("SELECT id,name FROM h_stages ORDER BY id", fetch=True):
                subs = []
                for jid, jname in store.q("SELECT id,name FROM h_subjects WHERE stage_id=? ORDER BY id", (sid,), fetch=True):
                    tch = lib_tree(jid)
                    subs.append({"id": jid, "name": jname, "n": sum(t["n"] for t in tch), "teachers": tch})
                out.append({"id": sid, "name": sname, "subjects": subs})
            store.event(uid, "webapp")
            return self.write({"stages": out})
        if name == "search":
            qtxt = str(body.get("q", ""))[:80]
            def kk(v):
                v = str(body.get(v, "") or "")
                return sid_of(nrm(v)) if v else None
            files, total = search_files(qtxt, stage=kk("stage"), subject=kk("subject"), teacher=(sid_of(str(body.get("teacher"))) if body.get("teacher") else None), limit=30)
            if qtxt.strip():
                log_search(uid, qtxt)
            return self.write({"total": total, "files": [{k: f[k] for k in ("id", "title", "stage", "subject", "teacher", "type")} for f in files]})
        if name == "download":
            r = store.q("SELECT title,file_id,kind FROM h_files WHERE id=?", (int(body.get("id", 0)),), fetch=True)
            if not r:
                return self.fail(404, "الملزمة غير متوفرة")
            title, fid, kind = r[0]
            try:
                if kind == "photo":
                    await tgapp.bot.send_photo(uid, fid, caption=hcaption(int(body.get("id", 0))) or f"📄 {title}")
                else:
                    await tgapp.bot.send_document(uid, fid, caption=hcaption(int(body.get("id", 0))) or f"📄 {title}")
            except Exception:
                log.exception("webapp send failed")
                return self.fail(500, "تعذر الإرسال، اضغط /start في البوت أولاً")
            store.q("UPDATE h_files SET downloads=downloads+1 WHERE id=?", (int(body.get("id")),))
            store.event(uid, "handout")
            return self.write({"ok": True})
        if name == "timer":
            mins = 45 if int(body.get("mins", 25)) >= 45 else 25
            store.event(uid, "timer")
            asyncio.create_task(run_timer(tgapp.bot, uid, mins, 5 if mins == 25 else 10))
            return self.write({"ok": True})
        if name == "tools":
            return self.write({"tools": [dict(t, hint=MODE_PROMPTS.get(t["id"], "")) for t in TOOLS]})
        if name == "run":
            mode = body.get("mode")
            tool = next((t for t in TOOLS if t["id"] == mode), None)
            if not tool or mode not in MODE_ASKS:
                return self.fail(400, "أداة غير معروفة")
            parts = []
            f = body.get("file")
            if f:
                try:
                    data = base64.b64decode(f.get("b64", ""))
                except Exception:
                    return self.fail(400, "ملف غير صالح")
                if len(data) > 15 * 1024 * 1024:
                    return self.fail(413, "الملف كبير (الحد 15 ميغابايت)")
                p, err = blob_parts(data, f.get("mime", ""), f.get("name", ""))
                if err:
                    return self.fail(400, err)
                parts += p
            text = (body.get("text") or "").strip()[:20000]
            if text:
                parts.append({"text": text})
            if not parts:
                return self.fail(400, "أرسل نصاً أو ملفاً أولاً")
            try:
                result = await run_ai(mode, parts)
            except RuntimeError as e:
                return self.fail(502, str(e))
            store.event(uid, mode)
            sent = False
            if mode == "pdf":
                try:
                    pdf = build_pdf("ورقة مراجعة", result)
                    await tgapp.bot.send_document(uid, document=pdf, filename="مراجعة.pdf", caption="📄 ورقة المراجعة جاهزة!")
                    sent = True
                except Exception:
                    log.exception("webapp pdf failed")
            return self.write({"text": result, "pdf_sent": sent})
        if name == "quiz":
            topic = (body.get("topic") or "").strip()[:200]
            if not topic:
                return self.fail(400, "اكتب اسم المادة أو الموضوع")
            try:
                qs = await make_quiz(topic)
            except RuntimeError as e:
                return self.fail(502, str(e))
            store.event(uid, "quiz")
            if not qs:
                return self.fail(502, "تعذر إنشاء الاختبار، جرّب موضوعاً آخر")
            return self.write({"questions": qs})
        return self.fail(404, "not found")

async def serve(url):
    global store
    app = build_app()
    await app.initialize()
    await post_init(app)
    await app.start()
    await app.bot.set_webhook(url=f"{url.rstrip('/')}/{TOKEN}", allowed_updates=Update.ALL_TYPES)
    web = tornado.web.Application([
        (rf"/{re.escape(TOKEN)}", WebhookH),
        (r"/app", PageH), (r"/", PageH), (r"/health", HealthH), (r"/tglogin/([A-Za-z0-9_\-]+)", TgLoginH), (r"/tglqr/([A-Za-z0-9_\-]+)", TgQrH),
        (r"/api/(\w+)", ApiH),
    ], tgapp=app)
    web.listen(int(os.environ.get("PORT", "10000")), address="0.0.0.0")
    try:
        import PIL, pypdf, reportlab
        log.info("stamp libs ok: PIL %s pypdf %s reportlab %s", PIL.__version__, pypdf.__version__, reportlab.Version)
    except Exception:
        log.exception("stamp libs missing")
    log.info("web server + webhook up")
    import signal
    async def _bye():
        try:
            if _backup_dirty:
                await asyncio.wait_for(do_backup(), 20)
                log.info("shutdown backup done")
        except Exception:
            log.exception("shutdown backup")
        os._exit(0)
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(_bye()))
    except Exception:
        log.exception("sigterm hook")
    await asyncio.Event().wait()

if __name__ == "__main__":
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:
        asyncio.run(serve(url))
    else:
        build_app().run_polling()
