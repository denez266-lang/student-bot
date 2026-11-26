# -*- coding: utf-8 -*-
"""بوت الطالب الذكي — smart student Telegram bot backed by Gemini."""
import os, io, re, json, base64, logging

import httpx
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M
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

FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Amiri-Regular.ttf")

BASE_SYS = (
    "أنت «الطالب الذكي»: مساعد تعليمي للطلاب. أجب باللغة العربية الواضحة والبسيطة "
    "ما لم يطلب الطالب لغة أخرى صراحة. نظّم إجابتك بعناوين قصيرة ونقاط عندما يكون ذلك مفيداً، "
    "وتجنّب الحشو. إذا كان السؤال خارج الدراسة أجب عنه باختصار مفيد."
)

USERS = set()

# ---------------- Gemini ----------------

async def gemini(parts, system=BASE_SYS, temperature=0.5, max_tokens=8192):
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(GURL, params={"key": GKEY}, json=body)
    if r.status_code != 200:
        log.warning("gemini %s: %s", r.status_code, r.text[:300])
        if r.status_code in (429, 503):
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

# ---------------- menu ----------------

def menu_markup():
    return M([
        [B("📷 اشرح صورة", callback_data="m:explain"), B("📝 أنشئ ملخص", callback_data="m:summary")],
        [B("📚 أنشئ أسئلة", callback_data="m:questions"), B("🧠 اختبرني", callback_data="m:quiz")],
        [B("📅 جدول دراستي", callback_data="m:schedule"), B("🎤 حوّل صوتاً إلى نص", callback_data="m:voice")],
        [B("📄 أنشئ PDF", callback_data="m:pdf"), B("✍️ حل خطوة بخطوة", callback_data="m:solve")],
    ])

MENU_TEXT = (
    "🎓 أهلاً بك في بوت الطالب الذكي!\n\n"
    "اختر ماذا تريد من القائمة، أو أرسل سؤالك مباشرة وسأجيبك:"
)

MODE_PROMPTS = {
    "explain":   "📷 أرسل الصورة الآن (صفحة من كتاب، لوحة، تمرين...) وسأشرحها لك.",
    "summary":   "📝 أرسل النص أو صورة الصفحات أو ملف PDF، وسأصنع لك ملخصاً منظماً.",
    "questions": "📚 أرسل المادة (نصاً أو صورة أو PDF)، وسأنشئ لك أسئلة عليها مع الأجوبة النموذجية.",
    "quiz":      "🧠 عن ماذا تريد أن أختبرك؟ أرسل اسم المادة أو الموضوع (مثال: الفيزياء - الحرارة).",
    "schedule":  "📅 أرسل في رسالة واحدة: المواد التي تدرسها، وتواريخ الامتحانات إن وجدت، وعدد الساعات المتاحة يومياً. وسأرتب لك جدولاً.",
    "voice":     "🎤 أرسل الرسالة الصوتية أو المقطع الصوتي الآن، وسأحوّله إلى نص.",
    "pdf":       "📄 أرسل المحتوى (نصاً أو صورة)، وسأحوّله إلى ورقة مراجعة منظمة بصيغة PDF.",
    "solve":     "✍️ أرسل المسألة (نصاً أو صورة)، وسأحلها لك خطوة بخطوة.",
}

def back_markup():
    return M([[B("🔙 القائمة الرئيسية", callback_data="m:menu")]])

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        USERS.add(update.effective_user.id)
    ctx.user_data.clear()
    await update.message.reply_text(MENU_TEXT, reply_markup=menu_markup())

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text(f"👥 عدد المستخدمين (منذ تشغيل الخدمة): {len(USERS)}")

async def menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "m:menu":
        ctx.user_data.clear()
        await q.message.reply_text(MENU_TEXT, reply_markup=menu_markup())
        return
    mode = data.split(":", 1)[1]
    ctx.user_data.clear()
    ctx.user_data["mode"] = mode
    await q.message.reply_text(MODE_PROMPTS[mode], reply_markup=back_markup())

# ---------------- quiz ----------------

async def start_quiz(msg, ctx, topic):
    await msg.chat.send_action(ChatAction.TYPING)
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
    await f.download_as_memory(out=out)
    return out.getvalue()

async def content_parts(update, ctx):
    """Returns (parts, err) built from the incoming message: text/photo/pdf/voice."""
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
        elif (doc.mime_type or "").startswith("text/") or doc.file_name.lower().endswith(".txt"):
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

# ---------------- main message flow ----------------

MODE_ASKS = {
    "explain":   "اشرح محتوى هذه الصورة شرحاً تعليمياً واضحاً ومفصلاً، وحدد الأفكار الرئيسية واشرح المصطلحات الصعبة.",
    "summary":   "لخّص المحتوى التالي ملخصاً منظماً: الفكرة الرئيسية ثم أهم النقاط على شكل نقاط، ثم خلاصة قصيرة.",
    "questions": "أنشئ 10 أسئلة متنوعة (اختيار من متعدد وصح/خطأ وأسئلة مقالية قصيرة) على المحتوى التالي، ثم ضع الأجوبة النموذجية في قسم منفصل في الأسفل.",
    "solve":     "حل المسألة التالية خطوة بخطوة مع توضيح سبب كل خطوة، واذكر القوانين أو القواعد المستخدمة، ثم اكتب الجواب النهائي بوضوح.",
    "pdf":       "حوّل المحتوى التالي إلى ورقة مراجعة دراسية منظمة بعناوين ونقاط وتعريفات، مناسبة للطباعة والمراجعة قبل الامتحان. استخدم # للعناوين الرئيسية.",
    "schedule":  "بناءً على معلومات الطالب التالية، أنشئ جدول مذاكرة أسبوعياً واقعياً: وزّع المواد على أيام الأسبوع مع أوقات تقريبية وفترات راحة، ورتّب الأولويات حسب قرب الامتحان وصعوبة المادة، وأضف نصائح قصيرة للالتزام بالجدول.",
    "voice":     "انسخ هذا التسجيل الصوتي إلى نص مكتوب بدقة، باللغة التي يتحدث بها. اكتب النص فقط دون تعليق.",
}

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    if update.effective_user:
        USERS.add(update.effective_user.id)
    mode = ctx.user_data.get("mode")

    if mode == "quiz":
        topic = (msg.text or "").strip()
        if not topic:
            await msg.reply_text("أرسل اسم المادة أو الموضوع نصاً لأبني لك اختباراً.", reply_markup=back_markup())
            return
        ctx.user_data.pop("mode", None)
        try:
            await start_quiz(msg, ctx, topic)
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

    if ask:
        if not parts:
            await msg.reply_text(MODE_PROMPTS[mode], reply_markup=back_markup())
            return
        await msg.chat.send_action(ChatAction.TYPING)
        try:
            result = await gemini(parts + [{"text": ask}])
        except RuntimeError as e:
            await msg.reply_text(str(e), reply_markup=back_markup())
            return
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
        return

    # no active mode: general question
    if not parts:
        return
    await msg.chat.send_action(ChatAction.TYPING)
    try:
        result = await gemini(parts)
    except RuntimeError as e:
        await msg.reply_text(str(e))
        return
    await send_long(msg, result)

# ---------------- run ----------------

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(quiz_cb, pattern=r"^qz:"))
    app.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^m:"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if base:
        port = int(os.environ.get("PORT", "10000"))
        log.info("webhook on port %s", port)
        app.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN,
                        webhook_url=f"{base}/{TOKEN}", drop_pending_updates=True)
    else:
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
