# -*- coding: utf-8 -*-
"""بوت الطالب الذكي - smart student bot backed by Gemini, with admin panel."""
import os, io, re, json, base64, time, logging, asyncio, sqlite3, threading

import httpx
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M, InputFile
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

_backup_due = 0.0
APP = None

def backup_soon():
    global _backup_due
    _backup_due = time.time() + 90

async def backup_loop():
    while True:
        await asyncio.sleep(30)
        if _backup_due and time.time() >= _backup_due:
            await do_backup()

async def do_backup():
    global _backup_due
    _backup_due = 0.0
    if not APP or not ADMIN_BACKUP_CHAT or not store:
        return
    try:
        store.db.commit()
        with open(DB_PATH, "rb") as f:
            msg = await APP.bot.send_document(
                ADMIN_BACKUP_CHAT, InputFile(f, filename="student.db"),
                caption="💾 نسخة احتياطية من بيانات البوت - لا تحذف هذه الرسالة المثبتة.")
        try:
            await APP.bot.unpin_all_chat_messages(ADMIN_BACKUP_CHAT)
            await APP.bot.pin_chat_message(ADMIN_BACKUP_CHAT, msg.message_id, disable_notification=True)
        except Exception:
            log.exception("pin failed")
    except Exception:
        log.exception("backup failed")

async def restore_backup(app):
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

async def gemini(parts, system=BASE_SYS, temperature=0.5, max_tokens=8192):
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}
    models = [MODEL] + [m.strip() for m in FALLBACKS if m.strip() != MODEL]
    r = None
    async with httpx.AsyncClient(timeout=180) as c:
        for attempt, m in enumerate(models + models):
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

def menu_markup():
    return M([
        [B("📷 اشرح صورة", callback_data="m:explain"), B("📝 أنشئ ملخص", callback_data="m:summary")],
        [B("📚 أنشئ أسئلة", callback_data="m:questions"), B("🧠 اختبرني", callback_data="m:quiz")],
        [B("📅 جدول دراستي", callback_data="m:schedule"), B("🎤 حوّل صوتاً إلى نص", callback_data="m:voice")],
        [B("📄 أنشئ PDF", callback_data="m:pdf"), B("🧮 حل المسائل", callback_data="m:solve")],
    ])

MODE_PROMPTS = {
    "explain":   "📷 أرسل الصورة الآن (صفحة من كتاب، لوحة، تمرين...) وسأشرحها لك.",
    "summary":   "📝 أرسل النص أو صورة الصفحات أو ملف PDF، وسأصنع لك ملخصاً منظماً.",
    "questions": "📚 أرسل المادة (نصاً أو صورة أو PDF)، وسأنشئ لك أسئلة عليها مع الأجوبة النموذجية.",
    "quiz":      "🧠 عن ماذا تريد أن أختبرك؟ أرسل اسم المادة أو الموضوع (مثال: الفيزياء - الحرارة).",
    "schedule":  "📅 أرسل في رسالة واحدة: المواد التي تدرسها، وتواريخ الامتحانات إن وجدت، وعدد الساعات المتاحة يومياً. وسأرتب لك جدولاً.",
    "voice":     "🎤 أرسل الرسالة الصوتية أو المقطع الصوتي الآن، وسأحوّله إلى نص.",
    "pdf":       "📄 أرسل المحتوى (نصاً أو صورة)، وسأحوّله إلى ورقة مراجعة منظمة بصيغة PDF.",
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
        await q.message.reply_text(store.get("welcome", DEFAULT_WELCOME), reply_markup=menu_markup())
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
    await update.message.reply_text(store.get("welcome", DEFAULT_WELCOME), reply_markup=menu_markup())

async def menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await gate_ok(update, ctx):
        return
    data = q.data
    if data == "m:menu":
        ctx.user_data.clear()
        await q.message.reply_text(store.get("welcome", DEFAULT_WELCOME), reply_markup=menu_markup())
        return
    mode = data.split(":", 1)[1]
    ctx.user_data.clear()
    ctx.user_data["mode"] = mode
    await q.message.reply_text(MODE_PROMPTS[mode], reply_markup=back_markup())

# ---------------- quiz ----------------

async def start_quiz(msg, ctx, topic, uid):
    await msg.chat.send_action(ChatAction.TYPING)
    raw = await gemini(
        [{"text": (
            f"أنشئ 5 أسئلة اختيار من متعدد لاختبار طالب في: «{topic}». "
            "اجعل الأسئلة متدرجة الصعوبة. أعد JSON فقط دون أي نص آخر بهذا الشكل بالضبط:\n"
            '[{"q":"نص السؤال","options":["خيار1","خيار2","خيار3","خيار4"],"answer":0,"why":"شرح قصير للإجابة"}]'
            "\nanswer هو رقم الخيار الصحيح من 0 إلى 3."
        )}],
        temperature=0.6)
    store.event(uid, "quiz")
    m = re.search(r"\[.*\]", raw, re.S)
    qs = None
    if m:
        try:
            qs = json.loads(m.group(0))
        except Exception:
            qs = None
    if not qs or not isinstance(qs, list) or not all(isinstance(x, dict) and "q" in x and "options" in x for x in qs):
        ctx.user_data.clear()
        await msg.reply_text("تعذر إنشاء الاختبار لهذا الموضوع، جرّب موضوعاً آخر.", reply_markup=back_markup())
        return
    qs = [x for x in qs if isinstance(x.get("options"), list) and len(x["options"]) >= 2 and isinstance(x.get("answer"), int)][:8]
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
        if doc.mime_type == "application/pdf":
            data = await fetch_file(ctx, doc.file_id)
            try:
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(data))
                text = "\n".join((p.extract_text() or "") for p in reader.pages[:40])
            except Exception:
                text = ""
            text = text.strip()[:30000]
            if not text:
                return None, "لم أستطع استخراج نص من هذا الملف (قد يكون صوراً ممسوحة). أرسل صور الصفحات بدلاً منه."
            parts.append({"text": "محتوى ملف PDF التالي:\n" + text})
        elif (doc.mime_type or "").startswith("text/") or (doc.file_name or "").lower().endswith(".txt"):
            data = await fetch_file(ctx, doc.file_id)
            parts.append({"text": data.decode("utf-8", "ignore")[:30000]})
        else:
            return None, "هذا النوع من الملفات غير مدعوم. أرسل نصاً أو صورة أو PDF."
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
    "solve":     "حل المسألة أو المسائل الموجودة في المحتوى التالي (نص أو صورة) وفق تعليمات الحل. إذا وُجدت عدة أسئلة فاحلها كلها بالترتيب، وإذا طلب الطالب سؤالاً محدداً فاحله فقط.",
    "pdf":       "حوّل المحتوى التالي إلى ورقة مراجعة دراسية منظمة بعناوين ونقاط وتعريفات، مناسبة للطباعة والمراجعة قبل الامتحان. استخدم # للعناوين الرئيسية.",
    "schedule":  "بناءً على معلومات الطالب التالية، أنشئ جدول مذاكرة أسبوعياً واقعياً: وزّع المواد على أيام الأسبوع مع أوقات تقريبية وفترات راحة، ورتّب الأولويات حسب قرب الامتحان وصعوبة المادة، وأضف نصائح قصيرة للالتزام بالجدول.",
    "voice":     "انسخ هذا التسجيل الصوتي إلى نص مكتوب بدقة، باللغة التي يتحدث بها. اكتب النص فقط دون تعليق.",
}

FEAT_NAMES = {
    "join": "انضمام", "chat": "سؤال مباشر", "explain": "شرح صورة", "summary": "ملخص",
    "questions": "أسئلة", "quiz": "اختبار", "schedule": "جدول", "voice": "صوت إلى نص",
    "pdf": "PDF", "solve": "حل المسائل",
}

# ---------------- message flow ----------------

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    u = update.effective_user
    store.upsert_user(u)
    if await admin_input(update, ctx):
        return
    if not await gate_ok(update, ctx):
        return
    mode = ctx.user_data.get("mode")

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
        result = await (gemini(parts + [{"text": ask}], system=SOLVE_SYS, temperature=0.2, max_tokens=16384) if mode == "solve" else gemini(parts + ([{"text": ask}] if ask else [])))
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
            [B("💾 نسخة احتياطية", callback_data="a:backup")],
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
    return t

def uline(r):
    return f"• {r[1]} (@{r[2] or '—'}) id={r[0]}"

WAIT_PROMPTS = {
    "bc": "📢 أرسل الآن الرسالة التي تريد إذاعتها لكل المستخدمين (نص أو صورة مع تعليق).",
    "welcome": "👋 أرسل نص رسالة الترحيب الجديدة. أرسل - للرجوع للافتراضية.",
    "gch": "📺 أرسل يوزر القناة مثل @mychannel (البوت مشرف فيها).",
    "gmsg": "✏️ أرسل نص رسالة الاشتراك. أرسل - للافتراضية.",
    "maintmsg": "✏️ أرسل نص رسالة الصيانة. أرسل - للافتراضية.",
    "ban": "🚫 أرسل آيدي المستخدم (رقم) لحظره.",
    "unban": "✅ أرسل آيدي المستخدم (رقم) لفك حظره.",
}

async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    ctx.user_data.pop("admin_wait", None)
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
    if act in ("main", "settings", "sub", "users"):
        t, kb = screen(act)
    elif act == "stats":
        t, kb = stats_text(), back_admin()
    elif act in WAIT_PROMPTS:
        ctx.user_data["admin_wait"] = act
        back = {"bc": "main", "welcome": "main", "gch": "sub", "gmsg": "sub", "maintmsg": "settings"}.get(act, "users")
        t, kb = WAIT_PROMPTS[act], back_admin(back)
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
        t, kb = "💾 تم إرسال نسخة احتياطية وتثبيتها في محادثتك. تُحمَّل تلقائياً عند إعادة تشغيل الخدمة.", back_admin()
    else:
        t, kb = screen("main")
    try:
        await q.message.edit_text(t, reply_markup=kb)
    except Exception:
        await q.message.reply_text(t, reply_markup=kb)

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
    asyncio.create_task(backup_loop())
    asyncio.create_task(keepalive())

async def on_error(update, ctx):
    log.warning("handler error: %r", ctx.error)

def build_app():
    app = Application.builder().token(TOKEN).concurrent_updates(True).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^a:"))
    app.add_handler(CallbackQueryHandler(verify_cb, pattern=r"^g:verify$"))
    app.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(quiz_cb, pattern=r"^q"))
    app.add_handler(MessageHandler(~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    return app

if __name__ == "__main__":
    app = build_app()
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:
        port = int(os.environ.get("PORT", "10000"))
        app.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN,
                        webhook_url=f"{url}/{TOKEN}", drop_pending_updates=False)
    else:
        app.run_polling()
