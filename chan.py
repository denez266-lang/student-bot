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
            if r and r[0][0] and r[0][0] > 0 and CH["id"]:
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
    return buf.getvalue() if buf.tell() <= 7 * 1024 * 1024 else None

async def vision_stage(page1):
    """Gemini reads the header ovals/graphics on page 1 (stage + subject). Returns (canonical stage or "", subject)."""
    if not page1:
        return "", ""
    prompt = ("في أعلى الصفحة الأولى من هذه الملزمة غالباً شكلان (بيضاويان أو شريطان): أحدهما مكتوب فيه المرحلة الدراسية (مثل الخامس العلمي) "
              "والآخر المادة (مثل مادة الحاسوب). اقرأهما كما هما مكتوبان فقط. لا تخمّن ولا تأخذ المرحلة من اسم وحدة أو فصل؛ إن لم يظهرا بوضوح اترك الحقل فارغاً. "
              'الصيغة: {"stage":"","subject":""}')
    parts = [{"text": prompt}, {"inline_data": {"mime_type": "application/pdf", "data": __import__('base64').b64encode(page1).decode()}}]
    out = await asyncio.wait_for(G['gemini'](parts, system=G['ING_SYS'], temperature=0.0, max_tokens=300, fast=True), 45)
    m = re.search(r"\{.*\}", out, re.S)
    d = __import__('json').loads(m.group(0)) if m else {}
    subj = str(d.get("subject") or "").strip()
    subj = re.sub(r"^(مادة|ماده)\s+", "", subj)
    return G['canon_stage'](str(d.get("stage") or "")), subj

async def fix_stages(vision=False):
    """One pass: canonical stage names (merge duplicates), then re-sort entries by the stage written in the file name or first PDF pages."""
    moved, renamed, unresolved = [], 0, 0
    try:   # 1) canonical stage names; merge stages that are the same stage
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
                    hq("UPDATE h_files SET subject_id=? WHERE subject_id=?", (nj, jid), False)
                    hq("DELETE FROM h_subjects WHERE id=?", (jid,), False)
                hq("DELETE FROM h_stages WHERE id=?", (sid,), False)
                renamed += 1
    except Exception:
        log().exception("stage names")
    checked = 0
    rows = hq("SELECT f.id, COALESCE(f.fname,''), COALESCE(f.title,''), f.file_id, COALESCE(f.fsize,0), j.name, s.name, f.subject_id FROM h_files f "
              "JOIN h_subjects j ON f.subject_id=j.id JOIN h_stages s ON j.stage_id=s.id WHERE f.kind='document'")
    if vision:
        rows = sorted(rows, key=lambda r: 0 if not G["canon_stage"](r[6]) else 1)
    unres_ids = []
    for hid, fname, title, fid, fsize, subj, stage, jid in rows:
        try:
            new = G["stage_from_text"](fname)
            if not new and fid and 0 < fsize <= G["MAX_DL"] and fname.lower().endswith(".pdf"):
                data = await asyncio.wait_for(G["fetch_file"](NS_CTX(), fid), 40)
                text, _ = await asyncio.wait_for(asyncio.to_thread(G["pdf_head"], data), 25)
                new = G["stage_from_text"](text)
                if not new and vision:
                    try:
                        new, _vj = await vision_stage(await asyncio.to_thread(page1_pdf, data))
                    except Exception as e:
                        log().warning("vision %s: %r", hid, e)
                    await asyncio.sleep(2)
                del data
                checked += 1
                await asyncio.sleep(0.5)
            if not new and not G["canon_stage"](stage):
                unresolved += 1
                unres_ids.append((hid, title[:40], stage))
            if new and G["nrm"](new) != G["nrm"](G["canon_stage"](stage) or stage):
                sid = G["get_stage"](new)
                nj = G["get_subject"](sid, subj)
                hq("UPDATE h_files SET subject_id=? WHERE id=?", (nj, hid), False)
                G["cleanup_empty"](jid)
                moved.append((hid, stage, new))
                r = hq("SELECT ch_msg FROM h_files WHERE id=?", (hid,))
                if r and r[0][0] and r[0][0] > 0 and CH["id"]:
                    await _retry(G["APP"].bot.edit_message_caption, CH["id"], r[0][0], caption=(G["hcaption"](hid) or "")[:1024])
                    await asyncio.sleep(2)
        except Exception as e:
            log().warning("stage fix %s: %r", hid, e)
    G["backup_soon"]()
    log().info("stage fix: %d entries moved, %d stages renamed/merged, %d pdfs read, %d still without a valid stage: %s | moved: %s", len(moved), renamed, checked, unresolved, unres_ids[:20], moved[:40])


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
    G["APP"].add_handler(CommandHandler("chanregroup", chanregroup), group=-1)
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
        if G["store"].get("stage_v2", "") != "1":
            await fix_stages(vision=True)
            G["store"].set("stage_v2", "1")
    asyncio.create_task(_fix())
    asyncio.create_task(loop())
