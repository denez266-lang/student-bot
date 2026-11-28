# -*- coding: utf-8 -*-
"""Handout channel: processed handouts are mirrored to a channel and served with copyMessage."""
import asyncio, time, re
from telegram.error import RetryAfter

G = {}
CH = {"id": 0}


def hq(*a):
    return G["hq"](*a)


def log():
    return G["log"]


async def _retry(fn, *a, **k):
    for i in range(3):
        try:
            return await fn(*a, **k)
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 1)
    return await fn(*a, **k)


async def resolve():
    if CH["id"]:
        return CH["id"]
    st = G["store"]
    v = st.get("chan_id", "")
    if v.lstrip("-").isdigit():
        CH["id"] = int(v)
        return CH["id"]
    ch = await G["APP"].bot.get_chat("@" + (st.get("chan_user", "") or "TalibIAi"))
    CH["id"] = ch.id
    st.set("chan_id", str(ch.id))
    return CH["id"]


async def send(bot, uid, hid):
    """Copy the channel post to the student. False means: use the old file_id path."""
    try:
        r = hq("SELECT ch_msg, ch_fid, file_id FROM h_files WHERE id=?", (hid,))
        if not r or not r[0][0] or r[0][0] < 1 or r[0][1] != r[0][2] or not CH["id"]:
            return False
        try:
            await _retry(bot.copy_message, uid, CH["id"], r[0][0], protect_content=False)
        except Exception as e:
            s = str(e).lower()
            if "not found" in s or "message to copy" in s or "can't be copied" in s:   # post deleted: forget it, the loop reposts it
                hq("UPDATE h_files SET ch_msg=NULL, ch_fid=NULL WHERE id=?", (hid,), False)
                log().warning("chan post gone for handout %s: %s", hid, e)
                await G["watch_notify"](f"⚠️ منشور ملزمة #{hid} محذوف من القناة، سيُعاد نشره تلقائياً.")
            else:
                log().warning("chan copy failed for %s: %r", hid, e)
            return False
        return True
    except Exception:
        log().exception("chan send")
        return False


async def post_one(hid, title, fid, old):
    bot = G["APP"].bot
    cap = (G["hcaption"](hid) or f"📄 {title}")[:1024]
    try:
        m = await _retry(bot.send_document, CH["id"], fid, caption=cap, disable_notification=True)
        hq("UPDATE h_files SET ch_msg=?, ch_fid=? WHERE id=?", (m.message_id, fid, hid), False)
        if old and old > 0:
            try:
                await bot.delete_message(CH["id"], old)
            except Exception:
                pass
        return True
    except Exception as e:
        log().warning("chan post failed %s: %r", hid, e)
        hq("UPDATE h_files SET ch_msg=-1, ch_fid=? WHERE id=?", (fid, hid), False)   # do not retry until the file changes
        return False


async def test():
    """One-time self-test of posting and copying, results go to the log and the admin chat."""
    g, bot, W = G, G["APP"].bot, G["W"]
    adm = g["ADMIN_BACKUP_CHAT"]
    out, cleanup, ent = [], [], None
    try:
        cid = await resolve()
        me = await bot.get_me()
        mem = await bot.get_chat_member(cid, me.id)
        out.append(f"bot role in channel: {mem.status}")
        m = await bot.send_message(cid, "اختبار نشر البوت")
        cleanup.append(("b", m.message_id))
        out.append("bot posted text: OK")
        c = W.get("client")
        ent = None
        if c:
            try:
                ent = await c.get_entity("@" + (g["store"].get("chan_user", "") or "TalibIAi"))
                wm = await c.send_message(ent, "اختبار نشر الحساب المراقب")
                cleanup.append(("w", wm.id))
                out.append("watcher posted text: OK")
            except Exception as e:
                out.append(f"watcher post FAILED: {e!r}")
        else:
            out.append("watcher not connected")
        r = hq("SELECT file_id, fsize, title FROM h_files WHERE kind='document' AND COALESCE(fsize,0)>0 ORDER BY fsize DESC LIMIT 1")
        if r:
            t0 = time.time()
            bm = await bot.send_document(cid, r[0][0], caption="اختبار ملف كبير", disable_notification=True)
            t1 = time.time()
            cleanup.append(("b", bm.message_id))
            cp = await bot.copy_message(adm, cid, bm.message_id, protect_content=False)
            t2 = time.time()
            cleanup.append(("a", cp.message_id))
            out.append(f"big file {r[0][1] >> 20} MB: bot post by file_id {t1 - t0:.1f}s, bot copyMessage to admin chat {t2 - t1:.1f}s: OK")
        if c and ent:
            best = None
            async for mm in c.iter_messages(W["bot_ent"], limit=300):
                d = getattr(mm, "document", None)
                if d and (not best or d.size > best.document.size):
                    best = mm
            if best:
                t0 = time.time()
                wp = await c.send_file(ent, best.document, caption="اختبار ملف الحساب المراقب")
                t1 = time.time()
                cleanup.append(("w", wp.id))
                cp = await bot.copy_message(adm, cid, wp.id, protect_content=False)
                t2 = time.time()
                cleanup.append(("a", cp.message_id))
                out.append(f"watcher post of {best.document.size >> 20} MB doc {t1 - t0:.1f}s, bot copy to admin chat {t2 - t1:.1f}s: OK")
        ok = True
    except Exception as e:
        ok = False
        out.append(f"TEST FAILED: {e!r}")
    for k, i in cleanup:
        try:
            if k == "b":
                await bot.delete_message(CH["id"], i)
            elif k == "a":
                await bot.delete_message(adm, i)
            elif k == "w" and W.get("client"):
                await W["client"].delete_messages(ent, [i])
        except Exception:
            pass
    log().info("CHANTEST %s | %s", "PASS" if ok else "FAIL", " | ".join(out))
    try:
        await g["watch_notify"]("🧪 اختبار القناة: " + ("نجح" if ok else "فشل") + "\n" + "\n".join(out))
    except Exception:
        pass
    return ok

BAD_T = ("وزاري", "مسائل", "اسئل", "ملخص", "ملزم", "فصل", "دفتر", "واجب", "اختبار", "امتحان", "كلاميات", "للاستاذ", "pdf", "مراجع", "قطع", "نماذج", "اسقاط", "املاء", "قواعد", "انشاء",
         "فيزياء", "كيمياء", "رياضيات", "احياء", "انكليزي", "انجليزي", "عربي", "اسلامي", "حاسوب", "جغرافي", "تاريخ", "علوم", "ادبي", "علمي", "اعدادي", "متوسط", "جزء", "المنهج", "كتاب")


def teacher_ok(t):
    """A real teacher name has no digits and no handout/subject/stage words (those mean the file name was taken for a name)."""
    t = (t or "").strip()
    if not t:
        return False
    if re.search(r"\d", t) or len(t.split()) > 4:
        return False
    n = G.get("nrm", lambda x: x)(t)
    return not any(n.find(G.get("nrm", lambda x: x)(w)) >= 0 for w in BAD_T)


async def fix_teachers():
    """One pass: drop teacher values that are really file-name fragments (never invent a name) and fix the channel captions."""
    fixed = []
    for hid, title, teacher in hq("SELECT id, title, COALESCE(teacher,'') FROM h_files WHERE COALESCE(teacher,'')<>''"):
        if not teacher_ok(teacher):
            suf = f" - أ. {teacher}"
            nt = title[: -len(suf)].strip() if title.endswith(suf) else title
            hq("UPDATE h_files SET teacher='', title=? WHERE id=?", (nt, hid), False)
            fixed.append(hid)
    if fixed:
        G["backup_soon"]()
    log().info("teacher cleanup: %d entries cleared %s", len(fixed), fixed[:30])
    for hid in fixed:
        try:
            r = hq("SELECT ch_msg FROM h_files WHERE id=?", (hid,))
            if False and r and r[0][0] and r[0][0] > 0 and CH["id"]:   # channel left alone for now (user)
                await _retry(G["APP"].bot.edit_message_caption, CH["id"], r[0][0], caption=(G["hcaption"](hid) or "")[:1024])
                await asyncio.sleep(2)
        except Exception as e:
            log().warning("caption fix %s: %r", hid, e)


def page1_pdf(data):
    from pypdf import PdfReader, PdfWriter
    r = PdfReader(__import__('io').BytesIO(data))
    if r.is_encrypted:
        try:
            r.decrypt("")
        except Exception:
            return None
    w = PdfWriter()
    w.add_page(r.pages[0])
    buf = __import__('io').BytesIO()
    w.write(buf)
    return buf.getvalue() if buf.tell() <= 3 * 1024 * 1024 else None

async def vision_stage(page1):
    """Gemini reads the header ovals/graphics on page 1 (stage + subject). Returns (canonical stage or "", subject)."""
    if not page1:
        return "", ""
    prompt = ("في أعلى الصفحة الأولى من هذه الملزمة غالباً شكلان (بيضاويان أو شريطان): أحدهما مكتوب فيه المرحلة الدراسية (مثل الخامس العلمي) "
              "والآخر المادة (مثل مادة الحاسوب). إن كُتب بجانب المرحلة فرع (الأحيائي أو التطبيقي) فاكتبه مع المرحلة (مثل: الخامس العلمي الأحيائي). اقرأهما كما هما مكتوبان فقط. لا تخمّن ولا تأخذ المرحلة من اسم وحدة أو فصل؛ إن لم يظهرا بوضوح اترك الحقل فارغاً. "
              'الصيغة: {"stage":"","subject":""}')
    parts = [{"text": prompt}, {"inline_data": {"mime_type": "application/pdf", "data": __import__('base64').b64encode(page1).decode()}}]
    out = await asyncio.wait_for(G['gemini'](parts, system=G['ING_SYS'], temperature=0.0, max_tokens=300, fast=False), 150)
    m = re.search(r"\{.*\}", out, re.S)
    d = __import__('json').loads(m.group(0)) if m else {}
    subj = str(d.get("subject") or "").strip()
    subj = re.sub(r"^(مادة|ماده)\s+", "", subj)
    return G['canon_stage'](str(d.get("stage") or "")), subj

async def infer_stage(text, page1, subj, title):
    """Gemini judges the stage from the content/level when it is not written. Returns (canonical stage or "", confidence 0-100)."""
    prompt = ("حدّد المرحلة الدراسية العراقية لهذه الملزمة من محتواها ومستواها ومواضيعها حسب المنهج العراقي، علماً أن المرحلة غير مكتوبة صراحةً. "
              "المراحل الصالحة فقط: الأول المتوسط، الثاني المتوسط، الثالث المتوسط، الرابع الإعدادي، الخامس الإعدادي، السادس الإعدادي (مع العلمي أو الأدبي إن أمكن). "
              f"المادة: {subj or '-'}. العنوان: {title or '-'}.\n"
              'أجب JSON فقط: {"stage":"","confidence":0,"reason":""}. confidence رقم من 0 إلى 100، ولا تعطِ رقماً عالياً (85 فأكثر) إلا إذا كانت المواضيع تخص مرحلة واحدة بوضوح.')
    parts = [{"text": prompt}]
    if text:
        parts.append({"text": "نص أول صفحات الملف:\n" + text[:3000]})
    if page1:
        parts.append({"inline_data": {"mime_type": "application/pdf", "data": __import__('base64').b64encode(page1).decode()}})
    out = await asyncio.wait_for(G['gemini'](parts, system=G['ING_SYS'], temperature=0.0, max_tokens=300, fast=False), 150)
    m = re.search(r"\{.*\}", out, re.S)
    d = __import__('json').loads(m.group(0)) if m else {}
    try:
        conf = int(float(d.get("confidence") or 0))
    except Exception:
        conf = 0
    return G['canon_stage'](str(d.get("stage") or "")), conf


async def resume_archive():
    """Resume the archive from the saved cursor after the stage pass paused it."""
    try:
        st_ = G["store"]
        bf = G["bf_get"]()
        log().info("archive state: %s cursor %s sent %s", bf.get("state"), bf.get("cursor"), bf.get("sent"))
        if bf.get("state") in ("pause", "error") and bf.get("cid"):   # user: start/resume the archive by itself, no button
            bf["state"] = "run"
            G["bf_put"](bf)
            W = G["W"]
            if not W.get("task") or W["task"].done():
                W["task"] = asyncio.create_task(G["bf_run"](bf["cid"], resume=True))
            log().info("archive resumed from cursor %s", bf.get("cursor"))
        st_.set("bf_paused_stage", "0")
    except Exception:
        log().exception("resume archive")


async def _gtry(fn, errs, streak):
    """Gemini is often busy: retry with pauses; count a final failure."""
    for k in range(3):
        try:
            r = await fn()
            streak.clear()
            return r
        except Exception as e:
            if k == 2:
                errs.append(repr(e)[:60])
                streak.append(1)
                raise
            await asyncio.sleep(30)


import contextvars, json, secrets, os
import tornado.web
LANE = contextvars.ContextVar("lane", default="main")
_GK = {"bad": {}, "n": {}, "ptr": {"main": 0, "chat": 1, "stage": 2}}
KT = {}   # one-time page token -> expiry


def _extra_keys():
    try:
        v = G["store"].get("gkeys", "")
        if v:
            return [k for k in json.loads(G["_fernet"]().decrypt(v.encode()).decode()) if isinstance(k, str)]
    except Exception:
        log().warning("gkeys unreadable")
    return []


def all_keys():
    ks = [G["GKEY"]]
    return ks + [k for k in _extra_keys() if k not in ks]


def gk():
    """Next Gemini key: round-robin per lane (main/chat/stage start at different keys), skipping keys cooling after a 429."""
    ks, now, lane = all_keys(), time.time(), LANE.get()
    p = _GK["ptr"].get(lane, 0) % len(ks)
    for i in range(len(ks)):
        k = ks[(p + i) % len(ks)]
        if _GK["bad"].get(k, 0) <= now:
            _GK["ptr"][lane] = (p + i + 1) % len(ks)
            return k
    return min(ks, key=lambda x: _GK["bad"].get(x, 0))


def gk_bad(k):
    n = _GK["n"].get(k, 0) + 1
    _GK["n"][k] = n
    _GK["bad"][k] = time.time() + min(900, 30 * 2 ** (n - 1))


def gk_ok(k):
    _GK["n"][k] = 0
    _GK["bad"].pop(k, None)


async def gkeys_cmd(update, ctx):
    if not G["is_admin"](update):
        return
    tok = secrets.token_urlsafe(24)
    KT[tok] = time.time() + 900
    base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    ks, now = all_keys(), time.time()
    cool = sum(1 for k in ks if _GK["bad"].get(k, 0) > now)
    await update.message.reply_text(f"🔑 مفاتيح Gemini: {len(ks)} فعّال ({cool} في استراحة مؤقتة بعد ضغط الحصة).\n"
                                    f"رابط إضافة مفاتيح جديدة (صالح 15 دقيقة، لا تشاركه):\n{base}/gkeys/{tok}\n"
                                    "الصق المفاتيح في الصفحة وليس في المحادثة.", disable_web_page_preview=True)


class KeysH(tornado.web.RequestHandler):
    def prepare(self):
        self.set_header("Cache-Control", "no-store")
        self.set_header("Referrer-Policy", "no-referrer")
        self.set_header("X-Robots-Tag", "noindex")
        if KT.get(self.path_args[0], 0) < time.time():
            KT.pop(self.path_args[0], None)
            self.set_status(404)
            self.finish(G["login_page"]("<h2>انتهت صلاحية الرابط</h2><p>أرسل /gkeys للبوت ليعطيك رابطاً جديداً.</p>"))

    def _form(self, msg=""):
        n = len(all_keys())
        return G["login_page"]("<h2>🔑 مفاتيح Gemini</h2>" + msg + f"<p><small>المفاتيح الفعّالة الآن: {n}. المفاتيح لا تُعرض بعد الحفظ وتُحفظ مشفّرة.</small></p>"
                               "<form method=post><input type=hidden name=a value=add>"
                               "<textarea name=keys rows=6 autocomplete=off placeholder='AIza...' style='width:100%;padding:12px;margin:6px 0 14px;border:2px solid #D9D0FF;border-radius:14px;font-size:15px;direction:ltr;text-align:left'></textarea>"
                               "<small>مفتاح في كل سطر (مفاتيح مجانية من aistudio.google.com/apikey، يفضل من حسابات مختلفة).</small><br><br>"
                               "<button>➕ إضافة المفاتيح</button></form>"
                               "<hr><h2>DeepSeek</h2><p><small>" + ("✅ مفتاح DeepSeek محفوظ." if ds_key() else "لا يوجد مفتاح DeepSeek.") + " يُستعمل للدردشة والمهام النصية، والباقي على Gemini.</small></p>"
                               "<form method=post><input type=hidden name=a value=ds><input name=dskey type=password autocomplete=off placeholder='sk-...'><button>💾 حفظ مفتاح DeepSeek</button></form>"
                               "<form method=post style='margin-top:10px'><input type=hidden name=a value=dsclear><button style='background:#d9534f'>🗑 حذف مفتاح DeepSeek</button></form>"
                               "<hr><h2>Groq (مجاني)</h2><p><small>" + (f"✅ مفاتيح Groq المحفوظة: {len(gr_keys())}." if gr_keys() else "لا يوجد مفتاح Groq.") + " مفتاح في كل سطر، يُستعمل للدردشة والمهام النصية.</small></p>"
                               "<form method=post><input type=hidden name=a value=gr><textarea name=grkeys rows=3 autocomplete=off placeholder='gsk_...' style='width:100%;padding:12px;margin:6px 0 14px;border:2px solid #D9D0F0;border-radius:12px'></textarea><button>💾 حفظ مفاتيح Groq</button></form>"
                               "<form method=post style='margin-top:10px'><input type=hidden name=a value=grclear><button style='background:#d9534f'>🗑 حذف مفاتيح Groq</button></form>"
                               + _xform("cbr", "Cerebras (مجاني)", "csk-...", "cb") + _xform("orr", "OpenRouter (مجاني)", "sk-or-v1-...", "or") +
                               "<form method=post style='margin-top:14px'><input type=hidden name=a value=clear><button style='background:#d9534f'>🗑 حذف المفاتيح الإضافية</button></form>")

    def get(self, tok):
        self.finish(self._form())

    async def _xpost(self, a):
        import httpx
        p, kind = ("cbr", "cb") if a.startswith("cb") else ("orr", "or")
        st = _XP[p]["store"]
        if a.endswith("clear"):
            G["store"].set(st, "")
            _XP[p]["rest"].clear()
            self.finish(self._form("<p><b>✅ تم حذف المفاتيح.</b></p>"))
            return
        pat, vurl = (r"csk-[A-Za-z0-9]{20,150}", "https://api.cerebras.ai/v1/models") if p == "cbr" else (r"sk-or-v1-[A-Za-z0-9]{20,150}", "https://openrouter.ai/api/v1/key")
        ks = [x.strip().strip("\"'`") for x in re.split(r"[\s,]+", self.get_body_argument(kind + "keys", "")) if x.strip()]
        old, msgs, new = x_keys(p), [], []
        async with httpx.AsyncClient(timeout=20) as c:
            for k in ks[:5]:
                tail = "…" + k[-4:]
                if not re.fullmatch(pat, k):
                    msgs.append(f"{tail}: صيغة غير صحيحة")
                    continue
                try:
                    r = await c.get(vurl, headers={"Authorization": "Bearer " + k})
                except Exception:
                    msgs.append(f"{tail}: تعذر الاتصال")
                    continue
                if r.status_code in (200, 429):
                    if k not in old and k not in new:
                        new.append(k)
                        msgs.append(f"{tail}: ✅ تمت الإضافة")
                    else:
                        msgs.append(f"{tail}: موجود مسبقاً")
                elif r.status_code in (401, 403):
                    msgs.append(f"{tail}: ❌ مفتاح غير صحيح")
                else:
                    msgs.append(f"{tail}: ردّ غير متوقع ({r.status_code})")
        if new:
            G["store"].set(st, G["_fernet"]().encrypt(json.dumps((old + new)[:8]).encode()).decode())
            log().info("%s keys saved: %d new", p, len(new))
            try:
                G["backup_soon"]()
            except Exception:
                pass
        self.finish(self._form("<div class=e>" + "<br>".join(msgs or ["لم يتم إدخال أي مفتاح."]) + "</div>"))

    async def post(self, tok):
        import httpx
        a = self.get_body_argument("a", "")
        if a in ("cb", "or", "cbclear", "orclear"):
            await self._xpost(a)
            return
        if a == "dsclear":
            G["store"].set("dskey", "")
            _DS["until"] = 0
            self.finish(self._form("<p><b>✅ حُذف مفتاح DeepSeek.</b></p>"))
            return
        if a == "grclear":
            G["store"].set("grkeys", "")
            self.finish(self._form("<p><b>✅ حُذفت مفاتيح Groq.</b></p>"))
            return
        if a == "gr":
            import httpx
            ks = [x.strip().strip("\"'`") for x in re.split(r"[\s,]+", self.get_body_argument("grkeys", "")) if x.strip()]
            old, msgs, new = gr_keys(), [], []
            async with httpx.AsyncClient(timeout=20) as c:
                for k in ks[:5]:
                    tail = "…" + k[-4:]
                    if not re.fullmatch(r"gsk_[A-Za-z0-9]{20,150}", k):
                        msgs.append(f"{tail}: صيغة غير صحيحة (يبدأ بـ gsk_)")
                        continue
                    try:
                        r = await c.get("https://api.groq.com/openai/v1/models", headers={"Authorization": "Bearer " + k})
                    except Exception:
                        msgs.append(f"{tail}: تعذر الاتصال")
                        continue
                    if r.status_code in (200, 429):
                        if k not in old and k not in new:
                            new.append(k)
                            msgs.append(f"{tail}: ✅ تمت الإضافة")
                        else:
                            msgs.append(f"{tail}: موجود مسبقاً")
                    elif r.status_code in (401, 403):
                        msgs.append(f"{tail}: ❌ مفتاح غير صحيح")
                    else:
                        msgs.append(f"{tail}: ردّ غير متوقع ({r.status_code})")
            if new:
                G["store"].set("grkeys", G["_fernet"]().encrypt(json.dumps((old + new)[:8]).encode()).decode())
                log().info("groq keys saved: %d new", len(new))
            self.finish(self._form("<div class=e>" + "<br>".join(msgs or ["لم يتم إدخال أي مفتاح."]) + "</div>"))
            return
        if a == "ds":
            import httpx
            k = self.get_body_argument("dskey", "").strip().strip("\"'`")
            tail = "…" + k[-4:]
            if not re.fullmatch(r"[A-Za-z0-9_\-.~]{20,200}", k):
                self.finish(self._form(f"<div class=e>{tail}: صيغة المفتاح غير صحيحة (انسخه كاملاً من platform.deepseek.com/api_keys)</div>"))
                return
            try:
                async with httpx.AsyncClient(timeout=20) as c:
                    r = await c.get("https://api.deepseek.com/user/balance", headers={"Authorization": "Bearer " + k})
            except Exception:
                self.finish(self._form("<div class=e>تعذر الاتصال بـ DeepSeek، أعد المحاولة.</div>"))
                return
            if r.status_code == 401:
                self.finish(self._form(f"<div class=e>{tail}: مفتاح غير صحيح.</div>"))
                return
            if r.status_code != 200:
                self.finish(self._form(f"<div class=e>{tail}: ردّ غير متوقع من DeepSeek (رمز {r.status_code}).</div>"))
                return
            avail = None
            try:
                avail = bool(r.json().get("is_available"))
            except Exception:
                pass
            G["store"].set("dskey", G["_fernet"]().encrypt(k.encode()).decode())
            _DS["until"] = 0
            try:
                G["backup_soon"]()
            except Exception:
                pass
            log().info("deepseek key saved, balance available: %s", avail)
            warn = "" if avail else "<div class=e>تنبيه: الرصيد غير كافٍ حالياً، اشحن الرصيد من platform.deepseek.com (Top up) وإلا سيعمل البوت على Gemini.</div>"
            self.finish(self._form("<p><b>✅ حُفظ مفتاح DeepSeek (مشفّراً).</b></p>" + warn))
            return
        if a == "clear":
            G["store"].set("gkeys", "")
            self.finish(self._form("<p><b>✅ حُذفت المفاتيح الإضافية.</b></p>"))
            return
        raw = self.get_body_argument("keys", "")
        cand = []
        for k in re.split(r"[\s,;]+", raw):
            k = k.strip("\"'`<>()[]{}").split("=", 1)[-1].strip("\"'`")
            if k and k not in cand:
                cand.append(k)
        have = all_keys()
        extra = _extra_keys()
        added, bad, dup, why = 0, [], 0, []
        async with httpx.AsyncClient(timeout=20) as c:
            for k in cand[:20]:
                if k in have:
                    dup += 1
                    continue
                tail = "…" + k[-4:]
                if not re.fullmatch(r"[A-Za-z0-9_\-.~]{20,200}", k):
                    bad.append(f"{tail}: صيغة المفتاح غير صحيحة (أحرف غريبة أو مقطوع)")
                    why.append("format")
                    continue
                try:
                    r = await c.get("https://generativelanguage.googleapis.com/v1beta/models", params={"pageSize": 1}, headers={"x-goog-api-key": k})
                except Exception:
                    bad.append(f"{tail}: تعذر الاتصال بخدمة Google، أعد المحاولة")
                    why.append("network")
                    continue
                msg = ""
                try:
                    msg = str(r.json().get("error", {}).get("message", "")).lower()
                except Exception:
                    pass
                if r.status_code in (200, 429):   # 429 = the key is real, only rate limited
                    extra.append(k)
                    added += 1
                    why.append("ok" if r.status_code == 200 else "ok-429")
                elif r.status_code == 400 and ("not valid" in msg or "invalid" in msg):
                    bad.append(f"{tail}: مفتاح غير صالح (تأكد من نسخه كاملاً من aistudio.google.com/apikey)")
                    why.append("invalid")
                elif r.status_code == 403:
                    bad.append(f"{tail}: المفتاح مرفوض (الواجهة Generative Language API غير مفعّلة لمشروعه أو المفتاح مقيّد)")
                    why.append("403")
                else:
                    bad.append(f"{tail}: ردّ غير متوقع من Google (رمز {r.status_code})")
                    why.append(str(r.status_code))
        if added:
            G["store"].set("gkeys", G["_fernet"]().encrypt(json.dumps(extra).encode()).decode())
            try:
                G["backup_soon"]()
            except Exception:
                pass
        log().info("gkeys page: added %d, rejected %d, duplicate %d, reasons %s", added, len(bad), dup, ",".join(why))
        msg = f"<p><b>✅ أُضيف {added} مفتاح.</b></p>" + (f"<p>مكرر/موجود: {dup}</p>" if dup else "") + \
              (f"<div class=e>لم يُقبل:<br>{'<br>'.join(html_join([b]) for b in bad)}</div>" if bad else "")
        self.finish(self._form(msg))


def html_join(xs):
    return ", ".join(str(x).replace("<", "").replace(">", "") for x in xs)


IDENT = ("\n\nهويتك: اسمك «طالب AI» (Talib AI)، مساعد دراسي طوّره المطوّر Denez (@DenezZE). "
         "إذا سُئلت من أنت أو ما اسمك أو من صنعك أو من طوّرك أو ما النموذج الذي تعمل به، فأجب بأنك «طالب AI» من تطوير Denez (@DenezZE)، "
         "ولا تذكر اسم أي شركة أو نموذج ذكاء اصطناعي (مثل Google وGemini وDeepSeek وOpenAI وGroq). لا تنكر أنك ذكاء اصطناعي. لا تذكر هذا إلا عند السؤال عن هويتك.")


def ident(system):
    return system if (not system or system == G.get("ING_SYS")) else system + IDENT


CHAT_SYS = ("أنت مساعد ذكي ودود يتحدث مع طلاب عراقيين. أجب دائماً بالعربية وباختصار (من سطرين إلى خمسة أسطر)، بلغة بسيطة وواضحة، "
            "بدون عناوين ولا رموز تنسيق مثل ** أو #. للأسئلة الدراسية اشرح خطوة بخطوة وباختصار. إذا لم تفهم الرسالة اطلب التوضيح بجملة قصيرة.")
def _xform(p, title, ph, kind):
    n = len(x_keys(p))
    return ("<hr><h2>" + title + "</h2><p><small>" + (f"✅ مفاتيح محفوظة: {n}." if n else "لا يوجد مفتاح.") + " مفتاح في كل سطر، احتياطي تلقائي للدردشة والمهام النصية.</small></p>"
            f"<form method=post><input type=hidden name=a value={kind}><textarea name={kind}keys rows=3 autocomplete=off placeholder='{ph}' style='width:100%;padding:12px;margin:6px 0 14px;border:2px solid #D9D0F0;border-radius:12px;direction:ltr;text-align:left'></textarea><button>💾 حفظ المفاتيح</button></form>"
            f"<form method=post style='margin-top:10px'><input type=hidden name=a value={kind}clear><button style='background:#d9534f'>🗑 حذف المفاتيح</button></form>")


_CH = {"sem": None, "last": {}, "hour": {}}


_DS = {"until": 0, "day": 0, "n": 0}


def ds_key():
    try:
        v = G["store"].get("dskey", "")
        return G["_fernet"]().decrypt(v.encode()).decode() if v else ""
    except Exception:
        return ""


async def ds_one(parts, system, temperature=0.5, max_tokens=4000, model="deepseek-flash"):
    """DeepSeek (OpenAI-compatible) for plain text work. Returns None (caller uses Gemini) when there is no key, the request has images/audio/PDF text,
    it is archive/stage classification, or DeepSeek fails or is resting."""
    try:
        key = ds_key()
        now = time.time()
        if not key or now < _DS["until"] or not system or system == G["ING_SYS"]:
            return None
        if any(("inline_data" in p) or str(p.get("text", "")).startswith("محتوى ملف PDF") for p in parts):
            return None
        d = int(now // 86400)
        if _DS["day"] != d:
            _DS["day"], _DS["n"] = d, 0
        if _DS["n"] >= 3000:   # daily safety cap so the balance cannot drain
            return None
        _DS["n"] += 1
        import httpx
        solve = system == G.get("SOLVE_SYS")
        body = {"model": model, "stream": False, "temperature": temperature,
                "max_tokens": 16000 if solve else min(int(max_tokens), 4000),
                "messages": [{"role": "system", "content": system + IDENT}, {"role": "user", "content": "\n\n".join(str(p.get("text", "")) for p in parts)}],
                "thinking": {"type": "enabled" if solve else "disabled"}}
        if solve:
            body["reasoning_effort"] = "high" if model == "deepseek-v4-pro" else "low"
        async with httpx.AsyncClient(timeout=httpx.Timeout(110 if solve else 25)) as c:
            r = await c.post("https://api.deepseek.com/chat/completions", headers={"Authorization": "Bearer " + key}, json=body)
        if r.status_code == 200:
            text = str(r.json()["choices"][0]["message"].get("content") or "").strip()
            return text or None
        _DS["until"] = now + (900 if r.status_code in (401, 402) else 30)
        log().warning("deepseek %s, using Gemini", r.status_code)
    except Exception as e:
        _DS["until"] = time.time() + 30
        log().warning("deepseek failed (%s), using Gemini", type(e).__name__)
    return None


_GR = {"rest": {}, "i": 0, "day": 0, "n": 0}


def gr_keys():
    try:
        v = G["store"].get("grkeys", "")
        return [k for k in json.loads(G["_fernet"]().decrypt(v.encode()).decode()) if isinstance(k, str)] if v else []
    except Exception:
        return []


async def groq_call(parts, system, temperature=0.5, max_tokens=4000):
    """Groq free plan (openai/gpt-oss-120b). Keys rotate; a key gets a rest on 429 (retry-after). None -> caller falls back."""
    try:
        ks = gr_keys()
        if not ks or not system or system == G["ING_SYS"]:
            return None
        if any(("inline_data" in p) or str(p.get("text", "")).startswith("محتوى ملف PDF") for p in parts):
            return None
        if sum(len(str(p.get("text", ""))) for p in parts) > 24000:
            return None
        now = time.time()
        live = [k for k in ks if _GR["rest"].get(k, 0) < now]
        if not live:
            return None
        d = int(now // 86400)
        if _GR["day"] != d:
            _GR["day"], _GR["n"] = d, 0
        if _GR["n"] >= 5000:
            return None
        import httpx
        solve = system == G.get("SOLVE_SYS")
        body = {"model": "openai/gpt-oss-120b", "stream": False, "temperature": temperature,
                "max_completion_tokens": 12000 if solve else max(1500, min(int(max_tokens), 4000)),
                "reasoning_effort": "medium" if solve else "low", "include_reasoning": False,
                "messages": [{"role": "system", "content": system + IDENT}, {"role": "user", "content": "\n\n".join(str(p.get("text", "")) for p in parts)}]}
        async with httpx.AsyncClient(timeout=httpx.Timeout(60 if solve else 20)) as c:
            for _ in range(min(2, len(live))):
                _GR["i"] += 1
                k = live[_GR["i"] % len(live)]
                live = [x for x in live if x != k]
                _GR["n"] += 1
                r = await c.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": "Bearer " + k}, json=body)
                if r.status_code == 200:
                    t = str(r.json()["choices"][0]["message"].get("content") or "").strip()
                    if t:
                        return t
                    continue
                if r.status_code == 429:
                    try:
                        ra = float(r.headers.get("retry-after", 60))
                    except Exception:
                        ra = 60
                    _GR["rest"][k] = time.time() + min(max(ra, 10), 900)
                elif r.status_code in (401, 403):
                    _GR["rest"][k] = time.time() + 900
                log().warning("groq %s, next", r.status_code)
    except Exception as e:
        log().warning("groq failed (%s)", type(e).__name__)
    return None


_XP = {
    "cbr": {"store": "cbkeys", "url": "https://api.cerebras.ai/v1/chat/completions", "model": "gpt-oss-120b", "rest": {}, "i": 0, "day": 0, "n": 0, "cap": 3000},
    "orr": {"store": "orkeys", "url": "https://openrouter.ai/api/v1/chat/completions", "model": "openrouter/free", "rest": {}, "i": 0, "day": 0, "n": 0, "cap": 400},
}


def x_keys(p):
    try:
        v = G["store"].get(_XP[p]["store"], "")
        return [k for k in json.loads(G["_fernet"]().decrypt(v.encode()).decode()) if isinstance(k, str)] if v else []
    except Exception:
        return []


async def x_call(p, parts, system, temperature=0.5, max_tokens=4000):
    """Cerebras (gpt-oss-120b) or OpenRouter free router. Keys rotate, rest on 429. None -> caller falls back."""
    try:
        P = _XP[p]
        ks = x_keys(p)
        if not ks or not system or system == G["ING_SYS"]:
            return None
        if any(("inline_data" in q) or str(q.get("text", "")).startswith("محتوى ملف PDF") for q in parts):
            return None
        if sum(len(str(q.get("text", ""))) for q in parts) > 24000:
            return None
        now = time.time()
        live = [k for k in ks if P["rest"].get(k, 0) < now]
        if not live:
            return None
        d = int(now // 86400)
        if P["day"] != d:
            P["day"], P["n"] = d, 0
        if P["n"] >= P["cap"]:
            return None
        import httpx
        solve = system == G.get("SOLVE_SYS")
        body = {"model": P["model"], "stream": False, "temperature": temperature,
                "max_tokens": 12000 if solve else max(1500, min(int(max_tokens), 4000)),
                "messages": [{"role": "system", "content": system + IDENT}, {"role": "user", "content": "\n\n".join(str(q.get("text", "")) for q in parts)}]}
        if p == "cbr":
            body["reasoning_effort"] = "medium" if solve else "low"
        async with httpx.AsyncClient(timeout=httpx.Timeout(60 if solve else 25)) as c:
            for _ in range(min(2, len(live))):
                P["i"] += 1
                k = live[P["i"] % len(live)]
                live = [x for x in live if x != k]
                P["n"] += 1
                r = await c.post(P["url"], headers={"Authorization": "Bearer " + k}, json=body)
                if r.status_code == 200:
                    t = str(r.json()["choices"][0]["message"].get("content") or "").strip()
                    if t:
                        return t
                    continue
                if r.status_code == 429:
                    try:
                        ra = float(r.headers.get("retry-after", 60))
                    except Exception:
                        ra = 60
                    P["rest"][k] = time.time() + min(max(ra, 10), 900)
                elif r.status_code in (401, 402, 403):
                    P["rest"][k] = time.time() + 900
                log().warning("%s %s, next", p, r.status_code)
    except Exception as e:
        log().warning("%s failed (%s)", p, type(e).__name__)
    return None


async def ds_call(parts, system, temperature=0.5, max_tokens=4000, model="deepseek-flash", only=False):
    """Text work: DeepSeek, then Groq, Cerebras, OpenRouter (each only if it has a key); None means use Gemini."""
    r = await ds_one(parts, system, temperature, max_tokens, model)
    if r or only:
        return r
    r = await groq_call(parts, system, temperature, max_tokens)
    if r:
        return r
    r = await x_call("cbr", parts, system, temperature, max_tokens)
    if r:
        return r
    return await x_call("orr", parts, system, temperature, max_tokens)


CHAT_MODELS = ["gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.7-flash"]


_SV = {}
_SOLVED = contextvars.ContextVar("SOLVED", default=None)


def clean_math(t):
    """Plain readable math: strip LaTeX leftovers and markdown."""
    t = str(t or "")
    for _ in range(3):
        t = re.sub(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"(\1)/(\2)", t)
        t = re.sub(r"\\sqrt\s*\{([^{}]*)\}", r"√(\1)", t)
    for a, z in (("\\times", "×"), ("\\cdot", "·"), ("\\div", "÷"), ("\\pm", "±"), ("\\leq", "≤"), ("\\geq", "≥"), ("\\neq", "≠"),
                 ("\\approx", "≈"), ("\\pi", "π"), ("\\theta", "θ"), ("\\Delta", "Δ"), ("\\rightarrow", "→"), ("\\to", "→"), ("\\left", ""), ("\\right", ""),
                 ("\\(", ""), ("\\)", ""), ("\\[", ""), ("\\]", ""), ("\\,", " "), ("\\text", "")):
        t = t.replace(a, z)
    t = re.sub(r"\^2\b", "²", t)
    t = re.sub(r"\^3\b", "³", t)
    t = re.sub(r"\^\{([^{}]*)\}", r"^(\1)", t)
    t = re.sub(r"[$`]+|\*\*|^#{1,6}\s*", "", t, flags=re.M)
    return t.strip()


async def solve(parts):
    """Photomath-style: Gemini reads the photo, the strongest text engine solves (thinking), Gemini is the fallback."""
    g, ask, sys_ = G["gemini"], G["MODE_ASKS"].get("solve", ""), G["SOLVE_SYS"]
    problem, rest = "", []
    imgs = [p for p in parts if "inline_data" in p]
    txt = [p for p in parts if "inline_data" not in p]
    if imgs:
        try:
            problem = (await asyncio.wait_for(g(imgs + [{"text": "انسخ نص المسألة أو المسائل الموجودة في الصورة كما هي حرفياً، مع المعادلات والأرقام بدقة بنص عادي (بدون LaTeX). لا تحلّها. إذا كانت الصورة غير واضحة أو لا تحتوي مسألة فاكتب فقط: غير واضحة"}],
                      system="أنت قارئ دقيق للمسائل المدرسية من الصور.", temperature=0, max_tokens=1500, fast=True), 50)).strip()
        except Exception as e:
            log().warning("solve ocr failed (%s), direct", type(e).__name__)
            problem = ""
        if problem and problem.replace(".", "").strip() == "غير واضحة":
            return "📷 لم أستطع قراءة المسألة بوضوح. صوّرها مرة أخرى من قرب وبإضاءة جيدة، أو اكتبها نصاً."
    if imgs and not problem:
        out = await g(parts + [{"text": ask}], system=sys_, temperature=0.2, max_tokens=16384)
        out = clean_math(out)
        _SOLVED.set(("", out))
        return out
    full = ([{"text": "نص المسألة (مقروء من الصورة):\n" + problem}] if problem else []) + txt + [{"text": ask}]
    out = await ds_call(full, sys_, 0.2, 16000, "deepseek-v4-pro" if ds_key() else "deepseek-flash")
    if not out:
        out = await g(full, system=sys_, temperature=0.2, max_tokens=16384)
    out = clean_math(out)
    _SOLVED.set((problem or "\n".join(str(p.get("text", "")) for p in txt)[:1500], out))
    return out


def solve_kb(mode, uid, kb):
    v = _SOLVED.get()
    if mode != "solve" or not v:
        return kb
    _SV[uid] = v
    _SOLVED.set(None)
    return G["M"]([[G["B"]("📖 شرح أكثر", callback_data="m:xs")]] + [list(r) for r in kb.inline_keyboard])


async def explain_more(problem, solution):
    p = ("المسألة:\n" + problem[:1500] + "\n\n" if problem else "") + "الحل السابق:\n" + solution[:6000] + (
        "\n\nاشرح هذا الحل بتفصيل أكثر وبأسلوب مبسّط جداً لطالب لم يفهمه: وضّح سبب كل خطوة والقانون المستخدم، وأعطِ مثالاً صغيراً مشابهاً عند الحاجة، "
        "وانبّه إلى أخطاء الطلاب الشائعة. اكتب بالعربية وبنص عادي (بدون LaTeX وبدون markdown) وبرموز يونيكود مقروءة، واختم بسطر: ✅ الخلاصة: ...")
    parts = [{"text": p}]
    out = await ds_call(parts, G["SOLVE_SYS"], 0.4, 8000, "deepseek-v4-pro" if ds_key() else "deepseek-flash")
    if not out:
        out = await G["gemini"](parts, system=G["SOLVE_SYS"], temperature=0.4, max_tokens=8192)
    return clean_math(out)


async def solve_more(q, ctx):
    uid = q.from_user.id
    v = _SV.get(uid)
    if not v:
        await q.message.reply_text("أرسل المسألة أولاً من زر حل مسألة رياضيات 🙂")
        return
    stop = asyncio.Event()
    asyncio.create_task(_typing(q.message, stop))
    try:
        out = await explain_more(*v)
    except Exception:
        await q.message.reply_text("الخدمة مشغولة الآن، حاول بعد دقيقة.")
        return
    finally:
        stop.set()
    _SV[uid] = (v[0], v[1] + "\n\n" + out)
    try:
        await G["send_long"](q.message, out)
    except Exception:
        await q.message.reply_text(out[:3900])
    await q.message.reply_text("هل تريد شيئاً آخر؟", reply_markup=G["M"]([[G["B"]("📖 شرح أكثر", callback_data="m:xs")], [G["B"]("🏠 القائمة", callback_data="m:menu")]]))


NB_SYS = ("أنت مساعد دراسة يعمل مثل NotebookLM: تجيب فقط من المصادر التي يقدّمها الطالب ولا تضف معلومات من خارجها. "
          "اكتب بالعربية بلغة بسيطة وواضحة. اذكر رقم المصدر بين قوسين مثل [مصدر 1] عند الاقتباس. "
          "إذا لم تجد الجواب في المصادر فقل بوضوح: هذا غير موجود في المصادر المرفوعة. "
          "الرسالة تظهر كنص عادي في تيليجرام: لا تستخدم LaTeX ولا markdown (لا ** ولا ##)، واكتب الرموز بيونيكود مقروء.")
NB_ASKS = {
    "sum": "لخّص المصادر في نقاط مرتبة بعناوين قصيرة، ثم اختم بأهم 5 أفكار يجب حفظها.",
    "q": "أنشئ 8 أسئلة متنوعة (اختيار من متعدد، صح أو خطأ، وسؤال قصير) من المصادر فقط، وضع الجواب الصحيح بعد كل سؤال.",
    "map": "ارسم خريطة ذهنية نصية للمصادر: الموضوع الرئيسي في السطر الأول، ثم الفروع بشرطة (-) وفروع الفروع بمسافتين وشرطة، بدون إطالة.",
}
_NB = {}
NB_MAX = 100000


def nb_get(uid):
    """Sources are kept in memory and saved in the store (survive restarts; backed up)."""
    if uid not in _NB:
        try:
            v = G["store"].get("nb:%s" % uid, "")
            _NB[uid] = [(str(x[0]), str(x[1])) for x in json.loads(v)] if v else []
        except Exception:
            _NB[uid] = []
    return _NB[uid]


def nb_save(uid):
    st = G["store"]
    s = _NB.get(uid) or []
    st.set("nb:%s" % uid, json.dumps(s, ensure_ascii=False) if s else "")
    try:
        idx = [x for x in json.loads(st.get("nb_idx", "[]") or "[]") if x != uid]
    except Exception:
        idx = []
    if s:
        idx.append(uid)
    while len(idx) > 40:   # keep the backup small: oldest notebooks are dropped
        old = idx.pop(0)
        _NB.pop(old, None)
        st.set("nb:%s" % old, "")
    st.set("nb_idx", json.dumps(idx))


def nb_clear(uid):
    _NB[uid] = []
    nb_save(uid)


def nb_add(uid, name, text):
    text = (text or "").strip()
    if len(text) < 30:
        raise RuntimeError("لم أجد نصاً كافياً في هذا المصدر.")
    s = nb_get(uid)
    if len(s) >= 6:
        raise RuntimeError("الحد 6 مصادر. امسح المصادر ثم أضف من جديد 🗑")
    text = text[:50000]
    if sum(len(x[1]) for x in s) + len(text) > NB_MAX:
        raise RuntimeError("حجم المصادر كبير. امسح بعضها وأضف الأهم 🗑")
    s.append((str(name)[:60] or "مصدر", text))
    nb_save(uid)
    return len(s), len(text)


async def nb_extract(data, mime, fname):
    parts, err = G["blob_parts"](data, mime, fname)
    if err:
        raise RuntimeError(err)
    img = [p for p in parts if "inline_data" in p]
    if img and (mime or "").startswith("image/"):
        t = await asyncio.wait_for(G["gemini"](img + [{"text": "انسخ كل النص الموجود في الصورة بدقة كما هو، وإن كانت صورة درس أو مخطط فاشرح محتواها بإيجاز. بدون مقدمات."}],
                                    system="أنت قارئ دقيق للنصوص من الصور.", temperature=0, max_tokens=4000, fast=True), 60)
        return t
    if any("inline_data" in p for p in parts):
        raise RuntimeError("هذا النوع غير مدعوم كمصدر. أرسل PDF أو نصاً أو صورة.")
    return "\n".join(str(p.get("text", "")).replace("محتوى ملف PDF التالي:\n", "") for p in parts)


async def nb_run(uid, kind, text=""):
    LANE.set("chat")
    s = nb_get(uid)
    if not s:
        raise RuntimeError("📓 أضف مصدراً أولاً: أرسل ملف PDF أو صورة أو الصق نصاً طويلاً.")
    now = time.time()
    hr = [t for t in _CH["hour"].get(uid, []) if now - t < 3600]
    if len(hr) >= 40 and uid not in G.get("ADMIN_IDS", ()):
        raise RuntimeError("وصلت للحد المسموح (40 طلباً في الساعة)، حاول لاحقاً 🌿")
    if kind == "ask":
        text = (text or "").strip()[:1500]
        if not text:
            raise RuntimeError("اكتب سؤالك 🙂")
        ask = "سؤال الطالب:\n" + text + "\n\nأجب من المصادر فقط."
    else:
        ask = NB_ASKS[kind]
    src = "\n\n".join("[مصدر %d: %s]\n%s" % (i + 1, n, t) for i, (n, t) in enumerate(s))
    parts = [{"text": "المصادر:\n" + src + "\n\n" + ask}]
    if _CH["sem"] is None:
        _CH["sem"] = asyncio.Semaphore(3)
    lk = _CH.setdefault("lk", {}).setdefault(uid, asyncio.Lock())
    try:
        async with lk:
            async with _CH["sem"]:
                out = await ds_call(parts, NB_SYS, 0.2, 4000, "deepseek-v4-pro" if ds_key() else "deepseek-flash")
                if not out:
                    out = await G["gemini"](parts, system=NB_SYS, temperature=0.2, max_tokens=4096)
    except RuntimeError:
        raise RuntimeError("😅 الذكاء الاصطناعي مشغول الآن، أعد المحاولة بعد دقيقة (مصادرك محفوظة).")
    hr.append(now)
    _CH["hour"][uid] = hr
    return re.sub(r"[*#`]+", "", out).strip()


def nb_kb():
    B = G["B"]
    return G["M"]([[B("📝 ملخص", callback_data="m:nbs"), B("❓ أسئلة", callback_data="m:nbq")],
                   [B("🗺 خريطة ذهنية", callback_data="m:nbm"), B("🗑 مسح المصادر", callback_data="m:nbc")],
                   [B("🛑 إنهاء", callback_data="m:menu")]])


def nb_list(uid):
    s = nb_get(uid)
    return "\n".join("%d) %s" % (i + 1, n) for i, (n, _) in enumerate(s)) or "لا توجد مصادر بعد."


async def nb_cb(q, ctx):
    uid = q.from_user.id
    d = q.data
    if d == "m:nb":
        ctx.user_data.clear()
        ctx.user_data["mode"] = "nb"
        await q.message.reply_text("📓 دفتر المصادر (مثل NotebookLM)\nأرسل مصادرك: ملف PDF أو صورة أو الصق نصاً طويلاً (حتى 6 مصادر). "
                                   "بعدها اسألني وأجيبك من مصادرك فقط، أو اضغط ملخص أو أسئلة أو خريطة ذهنية.\n\nمصادرك الآن:\n" + nb_list(uid), reply_markup=nb_kb())
        return
    ctx.user_data["mode"] = "nb"
    if d == "m:nbc":
        nb_clear(uid)
        await q.message.reply_text("🗑 تم مسح كل المصادر. أرسل مصادر جديدة.", reply_markup=nb_kb())
        return
    kind = {"m:nbs": "sum", "m:nbq": "q", "m:nbm": "map"}.get(d)
    if not kind:
        return
    stop = asyncio.Event()
    asyncio.create_task(_typing(q.message, stop))
    try:
        out = await nb_run(uid, kind)
    except RuntimeError as e:
        await q.message.reply_text(str(e), reply_markup=nb_kb())
        return
    finally:
        stop.set()
    G["store"].event(uid, "notebook")
    await G["send_long"](q.message, out)
    await q.message.reply_text("اسأل سؤالاً عن مصادرك أو اختر 👇", reply_markup=nb_kb())


async def nb_msg(update, ctx):
    msg = update.message
    uid = msg.from_user.id
    try:
        if msg.document or msg.photo:
            d = msg.document
            if d and (d.file_size or 0) > G["MAX_DL"]:
                raise RuntimeError("الملف كبير جداً (الحد 20 ميغابايت).")
            await msg.chat.send_action("typing")
            data = await G["fetch_file"](ctx, d.file_id if d else msg.photo[-1].file_id)
            name = (d.file_name if d else "") or "صورة"
            text = await nb_extract(data, (d.mime_type if d else "image/jpeg") or "", name)
            n, ln = nb_add(uid, name, text)
            await msg.reply_text("✅ أضفت المصدر %d: %s (%d حرف).\nأرسل سؤالك أو أضف مصدراً آخر، أو اختر من الأزرار." % (n, name, ln), reply_markup=nb_kb())
            return
        text = (msg.text or "").strip()
        if not text:
            await msg.reply_text("أرسل ملف PDF أو صورة أو نصاً 🙂", reply_markup=nb_kb())
            return
        if len(text) >= 600:
            n, ln = nb_add(uid, "نص ملصق %d" % (len(nb_get(uid)) + 1), text)
            await msg.reply_text("✅ أضفت النص كمصدر %d (%d حرف)." % (n, ln), reply_markup=nb_kb())
            return
        stop = asyncio.Event()
        asyncio.create_task(_typing(msg, stop))
        try:
            out = await nb_run(uid, "ask", text)
        finally:
            stop.set()
        G["store"].event(uid, "notebook")
        await msg.reply_text(out[:3900], reply_markup=nb_kb())
    except RuntimeError as e:
        await msg.reply_text(str(e), reply_markup=nb_kb())
    except Exception:
        log().exception("nb failed")
        await msg.reply_text("تعذرت قراءة المصدر، جرّب ملفاً آخر أو الصق النص.", reply_markup=nb_kb())


async def nb_api(uid, body):
    act = body.get("act", "state")
    if act == "clear":
        nb_clear(uid)
    elif act == "add":
        f = body.get("file")
        if f:
            import base64 as b64
            data = b64.b64decode(f.get("b64", ""))
            if len(data) > 15 * 1024 * 1024:
                raise RuntimeError("الملف كبير (الحد 15 ميغابايت)")
            text = await nb_extract(data, f.get("mime", ""), f.get("name", ""))
            nb_add(uid, f.get("name") or "ملف", text)
        else:
            nb_add(uid, "نص ملصق %d" % (len(nb_get(uid)) + 1), str(body.get("text", ""))[:60000])
    elif act in ("ask", "sum", "q", "map"):
        out = await nb_run(uid, act, str(body.get("text", "")))
        G["store"].event(uid, "notebook")
        return {"text": out, "srcs": [n for n, _ in nb_get(uid)]}
    return {"srcs": [n for n, _ in nb_get(uid)]}


ENGS = [("dsp", "🧠 ديب سيك برو (الأقوى)"), ("ds", "⚡ ديب سيك (سريع)"), ("grq", "🆓 جروك (مجاني)"), ("cbr", "⚡ سيريبراس (مجاني)"), ("orr", "🌐 اوبن راوتر (مجاني)"), ("gem", "✨ جيميني")]


def engines():
    return [e for e in ENGS if e[0] == "gem" or (e[0] == "grq" and gr_keys()) or (e[0] in ("cbr", "orr") and x_keys(e[0])) or (e[0] in ("dsp", "ds") and ds_key())]


def get_eng(uid):
    av = [e[0] for e in engines()]
    s = G["store"].get("eng:%s" % uid, "")
    return s if s in av else av[0]


def eng_label(e):
    return dict(ENGS).get(e, "")


async def chat_call(prompt, eng="gem"):
    """Fast chat call: fastest lite model first, 9 s per try, next key (not resting) on 429/timeout, then next model; 28 s budget."""
    if eng in ("dsp", "ds"):
        r0 = await ds_call([{"text": prompt}], CHAT_SYS, 0.7, 1200, "deepseek-v4-pro" if eng == "dsp" else "deepseek-flash", True)
        if r0:
            return r0
    if eng == "grq":
        r0 = await groq_call([{"text": prompt}], CHAT_SYS, 0.7, 1200)
        if r0:
            return r0
    if eng in ("cbr", "orr"):
        r0 = await x_call(eng, [{"text": prompt}], CHAT_SYS, 0.7, 1200)
        if r0:
            return r0
    import httpx
    n = len(all_keys())
    plan = [m for m in CHAT_MODELS for _ in range(min(n, 3))][:6]
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "system_instruction": {"parts": [{"text": CHAT_SYS + IDENT}]},
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1200}}
    t0 = time.time()
    async with httpx.AsyncClient(timeout=9) as c:
        for m in plan:
            if time.time() - t0 > 28:
                break
            k = gk()
            try:
                r = await c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent", params={"key": k}, json=body)
            except Exception:
                log().warning("chat gemini timeout/network (%s)", m)
                continue
            if r.status_code == 200:
                try:
                    text = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"] if not p.get("thought")).strip()
                except Exception:
                    text = ""
                gk_ok(k)
                if text:
                    return text
                continue
            if r.status_code == 429:
                gk_bad(k)
            log().warning("chat gemini %s (%s)", r.status_code, m)
    r1 = await ds_call([{"text": prompt}], CHAT_SYS, 0.7, 1200)
    if r1:
        return r1
    raise RuntimeError("الخدمة مزدحمة حالياً، حاول بعد دقيقة.")


async def _typing(msg, stop):
    while not stop.is_set():
        try:
            await msg.chat.send_action("typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), 4)
        except asyncio.TimeoutError:
            pass


async def chat_answer(uid, hist, text):
    """General AI chat (bot + Mini App). hist = [(role, text)], role u/a. Few calls per user, max 3 at once, so the archive/stage pass keep their quota."""
    LANE.set("chat")
    text = (text or "").strip()[:1500]
    if not text:
        raise RuntimeError("اكتب رسالتك نصاً من فضلك 🙂")
    now = time.time()
    adm = uid in G.get("ADMIN_IDS", ())
    hr = [t for t in _CH["hour"].get(uid, []) if now - t < 3600]
    if len(hr) >= 40 and not adm:
        raise RuntimeError("وصلت للحد المسموح (40 رسالة في الساعة) لحماية الخدمة، حاول لاحقاً 🌿")
    if _CH["sem"] is None:
        _CH["sem"] = asyncio.Semaphore(3)
    lines = [("الطالب: " if r == "u" else "المساعد: ") + t[:600] for r, t in hist[-8:]]
    prompt = ("سجل المحادثة السابق:\n" + "\n".join(lines) + "\n\n" if lines else "") + "رسالة الطالب الجديدة:\n" + text + "\n\nأجب عن الرسالة الجديدة فقط."
    try:
        lk = _CH.setdefault("lk", {}).setdefault(uid, asyncio.Lock())
        async with lk:  # one message per user at a time, extra messages wait their turn
            async with _CH["sem"]:
                out = await chat_call(prompt, get_eng(uid))
    except RuntimeError as e:
        m = str(e)
        if "مزدحمة" in m or "تعذر الاتصال" in m:
            raise RuntimeError("😅 الذكاء الاصطناعي مشغول الآن بسبب كثرة الاستخدام. انتظر دقيقة ثم أعد إرسال رسالتك، ومحادثتك محفوظة.")
        raise
    hr.append(now)
    _CH["hour"][uid] = hr
    return re.sub(r"[*#`]+", "", out).strip()[:3900]


def _end_kb(uid=0):
    rows = []
    if uid and len(engines()) > 1:
        rows.append([G["B"]("المحرك: " + eng_label(get_eng(uid)), callback_data="m:eng")])
    rows.append([G["B"]("🛑 إنهاء الدردشة", callback_data="m:menu")])
    return G["M"](rows)


async def chat_eng(q, ctx):
    uid = q.from_user.id
    ids = [e[0] for e in engines()]
    cur = get_eng(uid)
    nx = ids[(ids.index(cur) + 1) % len(ids)]
    G["store"].set("eng:%s" % uid, nx)
    ctx.user_data["mode"] = "aichat"
    await q.message.reply_text("تم اختيار المحرك: " + eng_label(nx) + "\nأكمل محادثتك 👇", reply_markup=_end_kb(uid))


async def chat_cb(q, ctx):
    if q.data.startswith("m:nb"):
        return await nb_cb(q, ctx)
    ctx.user_data.clear()
    ctx.user_data["mode"] = "aichat"
    ctx.user_data["chat"] = []
    await q.message.reply_text("💬 الدردشة مع الذكاء الاصطناعي\nاكتب أي سؤال أو موضوع وسأجيبك باختصار، وأتذكر ما قلناه خلال هذه المحادثة.\nعند الانتهاء اضغط الزر أدناه 👇\nالمحرك الحالي: " + eng_label(get_eng(q.from_user.id)), reply_markup=_end_kb(q.from_user.id))


async def chat_msg(update, ctx):
    if ctx.user_data.get("mode") == "nb":
        return await nb_msg(update, ctx)
    msg = update.message
    text = (msg.text or "").strip()
    if not text:
        await msg.reply_text("أرسل رسالتك نصاً من فضلك 🙂 (الصور والملفات لها أدواتها في القائمة).", reply_markup=_end_kb())
        return
    hist = ctx.user_data.setdefault("chat", [])
    stop = asyncio.Event()
    asyncio.create_task(_typing(msg, stop))
    try:
        out = await chat_answer(msg.from_user.id, hist, text)
    except RuntimeError as e:
        await msg.reply_text(str(e), reply_markup=_end_kb())
        return
    finally:
        stop.set()
    hist += [("u", text[:1500]), ("a", out)]
    del hist[:-16]
    G["store"].event(msg.from_user.id, "chat")
    await msg.reply_text(out, reply_markup=_end_kb(msg.from_user.id))


async def chat_api(uid, body):
    if body.get("op") == "nb":
        return await nb_api(uid, body)
    if body.get("op") == "xs":
        return {"text": await explain_more("", str(body.get("solution", ""))[:6000])}
    if body.get("op") == "eng":
        ids = [e[0] for e in engines()]
        if body.get("set") in ids:
            G["store"].set("eng:%s" % uid, body["set"])
        return {"eng": get_eng(uid), "list": [{"id": i, "t": t} for i, t in engines()]}
    h = []
    for m in (body.get("history") or [])[-12:]:
        if isinstance(m, dict) and m.get("r") in ("u", "a"):
            h.append((m["r"], str(m.get("t", ""))))
    out = await chat_answer(uid, h, str(body.get("text", "")))
    G["store"].event(uid, "chat")
    return out


def merge_stage_names():
    """Canonical stage names; stages that are the same stage (spelling variants) are merged, subjects and files moved."""
    renamed = 0
    try:
        for sid, sname in G["hq"]("SELECT id, name FROM h_stages ORDER BY id"):
            cn = G["canon_stage"](sname)
            if not cn or G["nrm"](cn) == G["nrm"](sname):
                continue
            target = G["get_stage"](cn, create=False)
            if target is None:
                hq("UPDATE h_stages SET name=? WHERE id=?", (cn, sid), False)
                renamed += 1
            elif target != sid:
                for jid, jname in hq("SELECT id, name FROM h_subjects WHERE stage_id=?", (sid,)):
                    nj = G["get_subject"](target, jname)
                    if nj != jid:
                        hq("UPDATE h_files SET subject_id=? WHERE subject_id=?", (nj, jid), False)
                        hq("DELETE FROM h_subjects WHERE id=?", (jid,), False)
                hq("DELETE FROM h_stages WHERE id=?", (sid,), False)
                renamed += 1
        if renamed:
            log().info("stage names merged: %d", renamed)
    except Exception:
        log().exception("stage names")
    return renamed


_ST = ("الأول المتوسط", "الثاني المتوسط", "الثالث المتوسط", "الرابع الإعدادي", "الخامس الإعدادي", "السادس الإعدادي")
_ORD = ("الأول", "الثاني", "الثالث", "الرابع", "الخامس", "السادس")
_TRK = {0: "", 1: " العلمي", 2: " الأدبي", 3: " العلمي الأحيائي", 4: " العلمي التطبيقي"}


def _pos(name):
    c = G["canon_stage"](name)
    for n, o in enumerate(_ORD):
        if c.startswith(o + " "):
            return n, (3 if "الأحيائي" in c else 4 if "التطبيقي" in c else 1 if "العلمي" in c else 2 if "الأدبي" in c else 0)
    return None


def menu(parts):
    """Nested library menu: stage -> track (علمي/ادبي/عام) -> sub-track (احيائي/تطبيقي) -> subjects. Returns (text, markup)."""
    B, M = G["B"], G["M"]
    rows = hq("SELECT s.id, s.name, (SELECT COUNT(*) FROM h_files f JOIN h_subjects j ON f.subject_id=j.id WHERE j.stage_id=s.id) FROM h_stages s ORDER BY s.id")
    pos, other, byid = {}, [], {}
    for sid, name, n in rows:
        p = _pos(name)
        byid[sid] = p
        if p is None:
            other.append((sid, name, n))
        elif p not in pos:
            pos[p] = [sid, n]
        else:
            pos[p][1] += n
    cnt = lambda n, ts: sum(pos[(n, t)][1] for t in ts if (n, t) in pos)
    btn = lambda label, cb: [B(label, callback_data=cb)]
    back = lambda cb: [B("🔙 رجوع", callback_data=cb)]

    def subjects(n, t, bk, sid=None, title=None):
        if sid is None:
            sid = pos[(n, t)][0] if (n, t) in pos else 0
        subj = hq("SELECT j.id, j.name, (SELECT COUNT(*) FROM h_files f WHERE f.subject_id=j.id) FROM h_subjects j WHERE j.stage_id=? ORDER BY j.id", (sid,))
        kb = [btn(f"📖 {G['trim'](r[1])} ({r[2]})", f"h:j:{r[0]}") for r in subj[:50]]
        kb.append(back(bk))
        title = title or f"🎓 {_ST[n]}{_TRK[t]}"
        return (f"{title}\nاختر المادة:" if subj else f"{title}\nلا توجد ملازم في هذا القسم بعد."), M(kb)

    act = parts[1]
    if act == "t":   # a stage row (used by the subject 'back' button)
        sid = int(parts[2])
        p = byid.get(sid)
        if p is None:
            nm = next((r[1] for r in rows if r[0] == sid), "")
            return subjects(0, 0, "h:S:9", sid, f"📂 {nm}")
        n, t = p
        return subjects(n, t, "h:s" if n < 3 else f"h:S:{n}" if t in (0, 1, 2) else f"h:S:{n}:1", sid)
    if act == "S" and len(parts) > 2:
        n = int(parts[2])
        t = int(parts[3]) if len(parts) > 3 else None
        if n == 9:
            kb = [btn(f"📂 {G['trim'](nm)} ({k})", f"h:t:{sid}") for sid, nm, k in other[:50]] + [back("h:s")]
            return "📂 ملازم غير مصنفة بعد\nاختر:", M(kb)
        if n < 3:
            return subjects(n, 0, "h:s")
        if t is None:
            kb = []
            if cnt(n, (1, 3, 4)):
                kb.append(btn(f"🔬 العلمي ({cnt(n, (1, 3, 4))})", f"h:S:{n}:1"))
            if cnt(n, (2,)):
                kb.append(btn(f"📖 الأدبي ({cnt(n, (2,))})", f"h:S:{n}:2"))
            if cnt(n, (0,)):
                kb.append(btn(f"📂 عام ({cnt(n, (0,))})", f"h:S:{n}:0"))
            kb.append(back("h:s"))
            return (f"🎓 {_ST[n]}\nاختر الفرع:" if len(kb) > 1 else f"🎓 {_ST[n]}\nلا توجد ملازم بعد."), M(kb)
        if t == 1 and n > 3 and cnt(n, (3, 4)):
            kb = []
            if cnt(n, (3,)):
                kb.append(btn(f"🧬 الأحيائي ({cnt(n, (3,))})", f"h:S:{n}:3"))
            if cnt(n, (4,)):
                kb.append(btn(f"⚙️ التطبيقي ({cnt(n, (4,))})", f"h:S:{n}:4"))
            if cnt(n, (1,)):
                kb.append(btn(f"📂 علمي عام ({cnt(n, (1,))})", f"h:S:{n}:6"))
            kb.append(back(f"h:S:{n}"))
            return f"🎓 {_ST[n]} العلمي\nاختر الفرع:", M(kb)
        if t == 6:
            return subjects(n, 1, f"h:S:{n}:1", title=f"🎓 {_ST[n]} العلمي (عام)")
        return subjects(n, t, f"h:S:{n}" if t in (0, 1, 2) else f"h:S:{n}:1")
    kb = [btn("🔍 بحث", "h:q")]
    for n in range(6):
        kb.append(btn(f"🎓 {_ST[n]} ({cnt(n, (0, 1, 2, 3, 4))})", f"h:S:{n}"))
    if other:
        kb.append(btn(f"📂 غير مصنفة ({sum(o[2] for o in other)})", "h:S:9"))
    kb.append(G["HOME"])
    return "📚 الملازم\nاختر المرحلة الدراسية:", M(kb)


async def fix_stages(vision=False):  # returns the number of Gemini failures
    """One pass: canonical stage names (merge duplicates), then re-sort entries by the stage written in the file name or first PDF pages."""
    moved, renamed, unresolved = [], 0, 0
    LANE.set("stage")
    renamed = merge_stage_names()
    checked = 0
    rows = hq("SELECT f.id, COALESCE(f.fname,''), COALESCE(f.title,''), f.file_id, COALESCE(f.fsize,0), j.name, s.name, f.subject_id FROM h_files f "
              "JOIN h_subjects j ON f.subject_id=j.id JOIN h_stages s ON j.stage_id=s.id WHERE f.kind='document'")
    if vision:
        rows = sorted(rows, key=lambda r: 0 if not G["canon_stage"](r[6]) else 1)
    unres_ids, review, inferred, errs, streak = [], [], [], [], []
    for hid, fname, title, fid, fsize, subj, stage, jid in rows:
        if len(streak) >= 12:   # Gemini really down: stop (retried later)
            errs.extend(["abort"] * 1000)
            break
        try:
            new = G["stage_from_text"](fname)
            if not new and fid and 0 < fsize <= G["MAX_DL"] and fname.lower().endswith(".pdf"):
                data = await asyncio.wait_for(G["fetch_file"](NS_CTX(), fid), 40)
                text, _ = await asyncio.wait_for(asyncio.to_thread(G["pdf_head"], data), 25)
                new = G["stage_from_text"](text)
                p1 = await asyncio.to_thread(page1_pdf, data) if vision else None
                if vision and (not new or (new.endswith("العلمي") and ("الخامس" in new or "السادس" in new))):
                    try:
                        vs, _vj = await _gtry(lambda: vision_stage(p1), errs, streak)
                        if vs and (not new or vs.startswith(new)):
                            new = vs
                    except Exception as e:
                        log().warning("vision %s: %r", hid, e)
                    await asyncio.sleep(6)
                    if not new:   # not written anywhere: infer from content, only high confidence is applied
                        try:
                            ist, conf = await _gtry(lambda: infer_stage(text, p1, subj, title), errs, streak)
                            cur0 = G["canon_stage"](stage)
                            if ist and conf >= 85:
                                new = ist
                                inferred.append((hid, title[:30], stage, ist, conf))
                            elif ist and G["nrm"](ist) != G["nrm"](cur0) and not G["nrm"](cur0).startswith(G["nrm"](ist)):
                                review.append((hid, title[:40], stage, ist, conf))
                            elif not ist and not cur0:
                                review.append((hid, title[:40], stage, "", conf))
                        except Exception as e:
                            log().warning("infer %s: %r", hid, e)
                        await asyncio.sleep(6)
                del data
                checked += 1
                await asyncio.sleep(0.5)
            if not new and not G["canon_stage"](stage):
                unresolved += 1
                unres_ids.append((hid, title[:40], stage))
            cur = G["canon_stage"](stage) or stage
            if new and G["nrm"](new) != G["nrm"](cur) and not G["nrm"](cur).startswith(G["nrm"](new)):
                sid = G["get_stage"](new)
                nj = G["get_subject"](sid, subj)
                hq("UPDATE h_files SET subject_id=? WHERE id=?", (nj, hid), False)
                G["cleanup_empty"](jid)
                moved.append((hid, stage, new))
                # channel captions/posts are NOT touched (user: library only)
        except Exception as e:
            log().warning("stage fix %s: %r", hid, e)
    if vision:
        try:
            G["store"].set("stage_review", __import__('json').dumps(review, ensure_ascii=False))
        except Exception:
            log().exception("stage_review save")
    G["backup_soon"]()
    log().info("stage pass gemini errors: %d", len(errs))
    log().info("stage inference: %d applied (conf>=85) sample %s | %d low-confidence to review", len(inferred), inferred[:5], len(review))
    log().info("stage fix: %d entries moved, %d stages renamed/merged, %d pdfs read, %d still without a valid stage: %s | moved: %s", len(moved), renamed, checked, unresolved, unres_ids[:20], moved[:40])
    return len(errs)


class NS_CTX:
    def __init__(self):
        self.bot = G["APP"].bot


PEND_SQL = ("SELECT f.id, f.title, f.file_id, COALESCE(f.ch_msg,0), COALESCE(f.teacher,''), COALESCE(j.name,''), COALESCE(s.name,''), f.added "
            "FROM h_files f LEFT JOIN h_subjects j ON f.subject_id=j.id LEFT JOIN h_stages s ON j.stage_id=s.id "
            "WHERE f.kind='document' AND COALESCE(f.file_id,'')<>''")


def gkey(r):
    """Teacher first (all of one teacher's handouts together, any subject), then stage, subject, title. No teacher goes last."""
    n = G.get("nrm", lambda x: x)
    t = n(r[4])
    return (G["stage_rank"](r[6]), 0 if t else 1, t, r[5], r[1])


async def regroup(update):
    """Admin: repost every handout already in the channel in teacher order (new post first, then the old one is deleted)."""
    CH["busy"] = True
    n = 0
    try:
        rows = hq(PEND_SQL + " AND f.ch_msg>0")
        for r in sorted(rows, key=gkey):
            if await post_one(r[0], r[1], r[2], r[3]):
                n += 1
            await asyncio.sleep(4)
        G["backup_soon"]()
        await update.message.reply_text(f"✅ انتهت إعادة ترتيب القناة حسب المدرس: {n} ملزمة.")
    except Exception as e:
        log().exception("regroup")
        await update.message.reply_text(f"⚠️ توقفت إعادة الترتيب: {e!r}")
    finally:
        CH["busy"] = False


async def chanregroup(update, ctx):
    if not G["is_admin"](update):
        return
    if (ctx.args or [""])[0] != "yes":
        n = hq("SELECT COUNT(*) FROM h_files WHERE ch_msg>0")[0][0]
        await update.message.reply_text(f"سأعيد نشر {n} ملزمة بالقناة مرتبة حسب المدرس وأحذف المنشورات القديمة (حوالي {n * 4 // 60 + 1} دقيقة). للتأكيد: /chanregroup yes")
        return
    if CH.get("busy"):
        await update.message.reply_text("إعادة الترتيب شغالة الآن.")
        return
    await update.message.reply_text("بدأت إعادة الترتيب. أخبرك عند الانتهاء.")
    asyncio.create_task(regroup(update))



async def loop():
    st = G["store"]
    await asyncio.sleep(40)
    while True:
        try:
            if st.get("chan_test", "") != "done" and G["W"].get("client") and time.time() - float(st.get("chan_test_ts", "0") or 0) > 1800:
                if True:
                    st.set("chan_test_ts", str(time.time()))
                    ok = await test()
                    if ok:
                        st.set("chan_test", "done")
                    if ok and not st.get("chan_mode", ""):
                        st.set("chan_mode", "all")
                        st.set("chan_since", str(time.time()))
            u = unstamped() if st.on("stamp_on") else []
            if len(u) != CH.get("nu"):
                CH["nu"] = len(u)
                log().warning("CHAN not posted (unstamped): %d %s", len(u), " | ".join(x[3] or x[1] for x in u[:15]))
            mode = st.get("chan_mode", "")
            if mode in ("new", "all") and not CH.get("busy") and await resolve():
                since = float(st.get("chan_since", "0") or 0)
                gate = 1 if st.on("stamp_on") else 0
                # entries whose file changed (redo/undo): repost at once
                for hid, title, fid, old in hq("SELECT id, title, file_id, ch_msg FROM h_files WHERE kind='document' AND ch_msg IS NOT NULL AND COALESCE(ch_fid,'')<>file_id AND COALESCE(file_id,'')<>'' LIMIT 3"):
                    await post_one(hid, title, fid, old if old and old > 0 else 0)
                    await asyncio.sleep(4)
                pend = hq(PEND_SQL + " AND f.ch_msg IS NULL AND (? = 'all' OR f.added>=?) AND (COALESCE(f.stamped,0)=1 OR ?=0)", (mode, since, gate))
                if pend:
                    try:
                        running = G["bf_get"]().get("state") == "run"
                    except Exception:
                        running = False
                    oldest = min(r[7] or time.time() for r in pend)
                    # group by teacher: wait for more files while the archive runs, then post teacher by teacher
                    if st.get("chan_group", "1") != "1" or not running or len(pend) >= 150 or time.time() - oldest >= 5400:
                        for r in sorted(pend, key=gkey):
                            if CH.get("busy"):
                                break
                            await post_one(r[0], r[1], r[2], 0)
                            await asyncio.sleep(4)
                        G["backup_soon"]()
                        continue
        except Exception:
            log().exception("chan loop")
        await asyncio.sleep(20)


CONC = 5


async def fast_dl(c, d, path, part=512 * 1024):
    """Pipelined download: CONC part requests in flight on one connection. Falls back to Telethon's own download on any error."""
    import os
    from telethon.tl.functions.upload import GetFileRequest
    from telethon.tl.types import InputDocumentFileLocation
    size = d.size
    own = False
    sender = c._sender
    try:
        loc = InputDocumentFileLocation(id=d.id, access_hash=d.access_hash, file_reference=d.file_reference, thumb_size="")
        if d.dc_id and d.dc_id != c.session.dc_id:
            sender = await c._borrow_exported_sender(d.dc_id)
            own = True
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        sem = asyncio.Semaphore(CONC)

        async def one(o):
            async with sem:
                r = await c._call(sender, GetFileRequest(loc, o, part))
                b = getattr(r, "bytes", None)
                if b is None or len(b) != min(part, size - o):
                    raise RuntimeError("short part")
                os.pwrite(fd, b, o)

        tasks = [asyncio.ensure_future(one(o)) for o in range(0, size, part)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            raise
        finally:
            os.close(fd)
        if os.path.getsize(path) != size:
            raise RuntimeError("size mismatch")
        return path
    except Exception as e:
        log().warning("fast_dl failed, using plain download: %r", e)
        try:
            os.remove(path)
        except Exception:
            pass
        return await c.download_file(d, file=path, part_size_kb=512, file_size=size)
    finally:
        if own:
            try:
                await c._return_exported_sender(sender)
            except Exception:
                pass


async def fast_up(c, path, name, part=512 * 1024):
    """Pipelined upload of a big file (SaveBigFilePart with CONC parts in flight)."""
    import os
    from telethon.tl.functions.upload import SaveBigFilePartRequest
    from telethon.tl.types import InputFileBig
    size = os.path.getsize(path)
    if size <= 10 * 1024 * 1024:
        return await c.upload_file(path, part_size_kb=512, file_name=name)
    try:
        count = (size + part - 1) // part
        fid = int.from_bytes(os.urandom(8), "big", signed=True)
        sem = asyncio.Semaphore(CONC)
        tasks = []

        async def one(i, b):
            try:
                if not await c(SaveBigFilePartRequest(fid, i, count, b)):
                    raise RuntimeError("part rejected")
            finally:
                sem.release()

        try:
            with open(path, "rb") as f:
                for i in range(count):
                    await sem.acquire()
                    b = f.read(part)
                    tasks.append(asyncio.ensure_future(one(i, b)))
                    if i % 16 == 0:
                        tasks = [t for t in tasks if not t.done() or t.exception()]
                        for t in tasks:
                            if t.done() and t.exception():
                                raise t.exception()
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            raise
        return InputFileBig(fid, count, name)
    except Exception as e:
        log().warning("fast_up failed, using plain upload: %r", e)
        return await c.upload_file(path, part_size_kb=512, file_name=name)


def unstamped():
    return hq("SELECT id, title, COALESCE(fsize,0), COALESCE(fname,'') FROM h_files WHERE kind='document' AND COALESCE(stamped,0)=0 ORDER BY id")


async def stagereview(update, ctx):
    if not G["is_admin"](update):
        return
    try:
        rows = __import__('json').loads(G["store"].get("stage_review", "") or "[]")
    except Exception:
        rows = []
    if not rows:
        await update.message.reply_text("لا توجد ملازم بانتظار المراجعة.")
        return
    lines = [f"{r[0]} | {r[1]} | الحالي: {r[2]} | مقترح: {r[3] or '-'} ({r[4]}%)" for r in rows[:40]]
    await update.message.reply_text(f"ملازم مرحلتها غير مؤكدة ({len(rows)}):\n" + "\n".join(lines))


async def chanlist(update, ctx):
    if not G["is_admin"](update):
        return
    u = unstamped()
    n = hq("SELECT COUNT(*) FROM h_files WHERE ch_msg>0")[0][0]
    t = f"📢 منشورة في القناة: {n}\n⚠️ لم تُختم ولم تُنشر: {len(u)}"
    for i, ti, fs, fn in u[:40]:
        t += f"\n#{i} {ti[:50]} ({fs >> 20} MB)"
    await update.message.reply_text(t[:4000])


def start(g):
    G.update(g)
    try:
        merge_stage_names()
    except Exception:
        pass
    for col in ("ch_msg INTEGER", "ch_fid TEXT"):
        try:
            hq("ALTER TABLE h_files ADD COLUMN " + col, (), False)
        except Exception:
            pass
    try:   # user decision 27 Nov: label replacement OFF everywhere (stamp pages, promo removal and channel posting stay)
        if G["store"].get("wm_off_v", "") != "1":
            G["store"].set("wm_auto", "0")
            G["store"].set("wm_off_v", "1")
            log().info("label replacement switched OFF (wm_auto=0)")
    except Exception:
        log().exception("wm off")
    try:   # user decision 27 Nov 4:13 PM: plain copy mode (no stamp, no promo removal, no label replacement); toggles stay in the admin panel
        st = G["store"]
        if st.get("plain_v", "") != "1":
            for k in ("stamp_on", "old_auto", "wm_auto"):
                st.set(k, "0")
            st.set("plain_v", "1")
            log().info("plain copy mode ON: stamp_on=0 old_auto=0 wm_auto=0")
    except Exception:
        log().exception("plain mode")
    try:   # user: keep Gemini classification (the "AI only when needed" archive shortcut was reading teachers from file names)
        if G["store"].get("gem_v", "") != "1":
            G["store"].set("gem_lite", "0")
            G["store"].set("gem_v", "1")
            log().info("gemini classification for every archive file (gem_lite=0)")
    except Exception:
        log().exception("gem")
    from telegram.ext import CommandHandler
    G["APP"].add_handler(CommandHandler("chanlist", chanlist), group=-1)
    G["APP"].add_handler(CommandHandler("stagereview", stagereview), group=-1)
    G["APP"].add_handler(CommandHandler("gkeys", gkeys_cmd), group=-1)
    G["APP"].add_handler(CommandHandler("chanregroup", chanregroup), group=-1)
    from telegram.ext import MessageHandler, filters
    G["APP"].add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.StatusUpdate.ALL, fwd_msg, block=False), group=-2)
    G["APP"].add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.REPLY & ~filters.StatusUpdate.ALL, fw_admin_reply), group=-3)
    async def _cmds():
        await asyncio.sleep(15)
        try:
            from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
            bot = G["APP"].bot
            await bot.set_my_commands([BotCommand("start", "القائمة الرئيسية")], scope=BotCommandScopeDefault())
            adm = [BotCommand("start", "القائمة الرئيسية"), BotCommand("admin", "لوحة الأدمن"), BotCommand("gkeys", "رابط إدخال مفاتيح الذكاء الاصطناعي"),
                   BotCommand("stagereview", "مراجعة الملازم غير المصنفة"), BotCommand("chanlist", "قائمة قنوات المصدر"), BotCommand("myid", "معرّفي")]
            for a in G["ADMIN_IDS"]:
                await bot.set_my_commands(adm, scope=BotCommandScopeChat(chat_id=a))
            log().info("bot commands set: public 1, admin %d for %d admins", len(adm), len(G["ADMIN_IDS"]))
            try:
                from telegram import MenuButtonWebApp, WebAppInfo
                base = os.environ.get("RENDER_EXTERNAL_URL", "")
                if base:
                    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="ابدأ الدراسة", web_app=WebAppInfo(url=base.rstrip("/") + "/app")))
                    log().info("menu button set: web app")
            except Exception as e:
                log().warning("menu button failed (%s)", type(e).__name__)
        except Exception as e:
            log().warning("set commands failed (%s)", type(e).__name__)
    asyncio.create_task(_cmds())
    async def _fix():
        await asyncio.sleep(90)
        try:
            await resolve()
        except Exception:
            pass
        await fix_teachers()
        if G["store"].get("stage_v", "") != "1":
            await fix_stages()
            G["store"].set("stage_v", "1")
        st_ = G["store"]
        if st_.get("stage_v2", "") != "1":
            try:   # user: pause the archive while the stage pass runs (Gemini is shared), resume afterwards
                bf = G["bf_get"]()
                log().info("archive state at stage pass start: %s cursor %s sent %s", bf.get("state"), bf.get("cursor"), bf.get("sent"))
                if bf.get("state") == "run":
                    bf["state"] = "pause"
                    G["bf_put"](bf)
                    st_.set("bf_paused_stage", "1")
                    log().info("archive paused for the stage pass (cursor %s)", bf.get("cursor"))
            except Exception:
                log().exception("pause archive")
            try:
                for _try in range(3):
                    ne = await fix_stages(vision=True)
                    if ne < 60:
                        st_.set("stage_v2", "1")
                        break
                    log().warning("stage pass had %d Gemini failures, retrying in 20 min", ne)
                    await asyncio.sleep(1200)
            finally:
                await resume_archive()
        else:
            await resume_archive()
    asyncio.create_task(_fix())
    asyncio.create_task(loop())


def join_note(u, st):
    """Admin-only notice for a genuinely new user. Never raises, never blocks the user's flow."""
    try:
        if st.get("join_notice", "1") == "0" or u.id in G.get("ADMIN_IDS", ()):
            return
        total = st.q("SELECT COUNT(*) FROM users", fetch=True)[0][0]
        txt = ("👾 شخص جديد دخل البوت\n\n👤 معلومات العضو الجديد:\n• الاسم: %s\n• المعرف: %s\n• الآيدي: %s\n\n• 🌐 اللغة: %s\n\n📊 إجمالي المستخدمين: %s"
               % (u.full_name or "", ("@" + u.username) if u.username else "", u.id, getattr(u, "language_code", "") or "", total))

        async def _send():
            for a in G["ADMIN_IDS"]:
                try:
                    await G["APP"].bot.send_message(a, txt)
                except Exception as e:
                    log().warning("join notice failed (%s)", type(e).__name__)
        asyncio.get_running_loop().create_task(_send())
    except Exception as e:
        log().warning("join_note failed (%s)", type(e).__name__)


_FW = {"q": None, "n": 0, "map": None}
_SECRET = re.compile(r"AIza[0-9A-Za-z_\-]{20,}|gsk_[A-Za-z0-9]{20,}|csk-[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_\-]{20,}|\d{8,}:[A-Za-z0-9_\-]{30,}|/gkeys|\b(password|كلمة المرور|كلمة السر)\b", re.I)


def fw_map():
    """{'m': {'<admin>:<msgid>': uid}, 'l': {'<uid>': lang}} persisted in the store (backed up)."""
    if _FW["map"] is None:
        try:
            _FW["map"] = json.loads(G["store"].get("fwmap", "") or "{}")
        except Exception:
            _FW["map"] = {}
        _FW["map"].setdefault("m", {})
        _FW["map"].setdefault("l", {})
    return _FW["map"]


def fw_save():
    d = fw_map()
    if len(d["m"]) > 1500:
        for k in list(d["m"])[:len(d["m"]) - 1500]:
            d["m"].pop(k, None)
    G["store"].set("fwmap", json.dumps(d))


async def _fw_worker():
    while True:
        chat_id, mid, who, txt, uid, lang = await _FW["q"].get()
        d = fw_map()
        d["l"][str(uid)] = lang
        for a in list(G["ADMIN_IDS"]):
            try:
                m = await G["APP"].bot.forward_message(a, chat_id, mid)
                d["m"]["%s:%s" % (a, m.message_id)] = uid
            except Exception as e:
                try:
                    m = await G["APP"].bot.send_message(a, who + ("\n" + txt if txt else ""))
                    d["m"]["%s:%s" % (a, m.message_id)] = uid
                except Exception:
                    log().warning("fwd failed (%s)", type(e).__name__)
        try:
            fw_save()
        except Exception:
            pass
        await asyncio.sleep(0.6)


def fw_info(uid):
    st = G["store"]
    r = st.q("SELECT name,username,joined,last_seen,banned FROM users WHERE id=?", (uid,), fetch=True)
    if not r:
        return "👤 ID: %s\nلا توجد بيانات محفوظة لهذا المستخدم." % uid
    name, un, joined, seen, banned = r[0]
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.gmtime((t or 0) + 3 * 3600)) if t else "-"
    ev = st.q("SELECT kind, COUNT(*) FROM events WHERE user_id=? GROUP BY kind ORDER BY 2 DESC", (uid,), fetch=True)
    ev24 = st.q("SELECT COUNT(*) FROM events WHERE user_id=? AND ts>?", (uid, time.time() - 86400), fetch=True)[0][0]
    sr = st.q("SELECT COUNT(*) FROM searches WHERE user_id=?", (uid,), fetch=True)[0][0]
    try:
        eng = st.get("eng:%s" % uid, "") or "-"
        nbn = len(nb_get(uid))
    except Exception:
        eng, nbn = "-", 0
    lang = fw_map()["l"].get(str(uid), "-")
    return ("👤 معلومات المستخدم\n• الاسم: %s\n• المعرف: %s\n• الآيدي: %s\n• 🌐 اللغة: %s\n• الانضمام: %s (بتوقيت بغداد)\n• آخر نشاط: %s\n• محظور: %s\n"
            "• نشاط آخر 24 ساعة: %s\n• عمليات البحث: %s\n• المحرك المختار: %s\n• مصادر دفتر المصادر: %s\n• الاستخدام: %s"
            % (name or "-", ("@" + un) if un else "-", uid, lang, fmt(joined), fmt(seen), "نعم" if banned else "لا", ev24, sr, eng, nbn,
               "، ".join("%s ×%s" % (k, n) for k, n in ev) or "-"))


async def fw_admin_reply(update, ctx):
    """Admin replies to a forwarded user message: info word -> user info; anything else -> delivered to that user."""
    from telegram.ext import ApplicationHandlerStop
    try:
        m, u = update.effective_message, update.effective_user
        if not m or not u or u.id not in G["ADMIN_IDS"] or not m.reply_to_message:
            return
        uid = fw_map()["m"].get("%s:%s" % (m.chat_id, m.reply_to_message.message_id))
        if not uid:
            return
        t = (m.text or "").strip().lower()
        if t in ("معلومات", "/info", "info", "معلوماته", "/معلومات"):
            await m.reply_text(fw_info(uid))
        else:
            await G["APP"].bot.copy_message(uid, m.chat_id, m.message_id)
            await m.reply_text("✅ وصلت الرسالة")
    except ApplicationHandlerStop:
        raise
    except Exception as e:
        log().warning("fw reply failed (%s)", type(e).__name__)
        try:
            await update.effective_message.reply_text("❌ تعذر الإرسال (قد يكون المستخدم حظر البوت).")
        except Exception:
            pass
    raise ApplicationHandlerStop


async def fwd_msg(update, ctx):
    """Forward users' private messages to the admins (toggle: store 'fwd', default on). Never blocks, never raises."""
    try:
        m, u = update.effective_message, update.effective_user
        if not m or not u or u.id in G["ADMIN_IDS"] or G["store"].get("fwd", "1") != "1":
            return
        txt = (m.text or m.caption or "")
        if _SECRET.search(txt):
            return
        if _FW["q"] is None:
            _FW["q"] = asyncio.Queue(maxsize=150)
            asyncio.get_running_loop().create_task(_fw_worker())
        who = "👤 %s%s | ID: %s" % (u.full_name or "", (" @" + u.username) if u.username else "", u.id)
        try:
            _FW["q"].put_nowait((m.chat_id, m.message_id, who, txt[:500], u.id, getattr(u, "language_code", "") or "-"))
        except asyncio.QueueFull:
            pass
    except Exception as e:
        log().warning("fwd_msg failed (%s)", type(e).__name__)


def sha(data):
    import hashlib
    return hashlib.sha1(data).hexdigest()[:10]


async def backup_run(bot, chat, path):
    """Silent backup: replace the pinned student.db document IN PLACE (no new chat message, no notification).
    Restore reads the pinned document, so it keeps finding the latest. Fallback: send new silently, pin, delete old."""
    import io
    from telegram import InputFile, InputMediaDocument
    cap = "💾 نسخة احتياطية من بيانات البوت"
    with open(path, "rb") as f:
        data = f.read()
    h = sha(data)
    pin = None
    try:
        pin = (await bot.get_chat(chat)).pinned_message
    except Exception:
        pass
    if pin and pin.document and pin.document.file_name == "student.db":
        try:
            await bot.edit_message_media(chat_id=chat, message_id=pin.message_id,
                                         media=InputMediaDocument(InputFile(io.BytesIO(data), filename="student.db", attach=True), caption=cap))
            log().info("backup saved, %d bytes sha %s (edited in place)", len(data), h)
            return True
        except Exception as e:
            if "not modified" in str(e).lower():
                log().info("backup saved, %d bytes sha %s (unchanged)", len(data), h)
                return True
            log().warning("backup edit failed (%s: %s), sending new", type(e).__name__, str(e)[:100])
    msg = await bot.send_document(chat, InputFile(io.BytesIO(data), filename="student.db"), caption=cap, disable_notification=True)
    log().info("backup saved, %d bytes sha %s (new message)", len(data), h)
    try:
        await bot.pin_chat_message(chat, msg.message_id, disable_notification=True)
    except Exception:
        log().warning("pin failed")
    if pin and pin.message_id != msg.message_id:
        try:
            await bot.delete_message(chat, pin.message_id)
        except Exception:
            pass
    return True
