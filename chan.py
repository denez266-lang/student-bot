# -*- coding: utf-8 -*-
"""Handout channel: processed handouts are mirrored to a channel and served with copyMessage."""
import asyncio, time
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
                        st.set("chan_mode", "new")
                        st.set("chan_since", str(time.time()))
            mode = st.get("chan_mode", "")
            if mode in ("new", "all") and await resolve():
                since = float(st.get("chan_since", "0") or 0)
                sql = ("SELECT id, title, file_id, COALESCE(ch_msg,0) FROM h_files WHERE kind='document' AND COALESCE(file_id,'')<>'' AND "
                       "((ch_msg IS NULL AND (? = 'all' OR added>=?)) OR (ch_msg IS NOT NULL AND COALESCE(ch_fid,'')<>file_id)) ORDER BY id LIMIT 3")
                rows = hq(sql, (mode, since))
                for hid, title, fid, old in rows:
                    await post_one(hid, title, fid, old)
                    await asyncio.sleep(4)
                if rows:
                    G["backup_soon"]()
                    continue
        except Exception:
            log().exception("chan loop")
        await asyncio.sleep(20)


def start(g):
    G.update(g)
    for col in ("ch_msg INTEGER", "ch_fid TEXT"):
        try:
            hq("ALTER TABLE h_files ADD COLUMN " + col, (), False)
        except Exception:
            pass
    asyncio.create_task(loop())
