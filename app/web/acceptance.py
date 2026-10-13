"""第 1 階段驗收:同一批影片,用「桌面版的方式」和「網頁版的後端」各跑一次,比對產生的 SRT 是否逐位元組相同。

  python -m app.web.acceptance 影片1.mp4 影片2.mp4 影片3.mp4 [--to en] [--batch 4]

桌面版的方式 = 和 GUI 一樣,直接以 spawn 程序執行 app.jobs.worker.run(輸出放在影片旁邊)。
網頁版的方式 = 建立使用者、項目、工作,由網頁版的執行器領取並交給同一個 worker.run(輸出放在項目資料夾)。
兩邊使用相同的選項與相同的批次大小;影片會複製(或硬連結)到 data\\acceptance\\ 底下,不會動你的原始檔案。
"""
from __future__ import annotations

import argparse
import difflib
import multiprocessing as mp
import os
import queue
import shutil
import sys
import threading
import time
from pathlib import Path


def _place(src: Path, dst: Path) -> None:
    """把影片放進工作資料夾:同一個磁碟優先用硬連結(不佔空間、不花時間),否則複製。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _desktop(videos: list[Path], settings: dict, target: str | None) -> dict[str, list[Path]]:
    """照 GUI 的做法跑一次;回傳 {影片檔名: [產生的 srt]}。"""
    from app.web.runner import _entry

    jobs = [{"idx": i, "path": str(v), "kind": "media", "stream": 0, "overwrite": False}
            for i, v in enumerate(videos)]
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=_entry, args=(jobs, settings, q, target), daemon=True)
    proc.start()
    done = False
    t0 = time.time()
    while not done:
        try:
            m = q.get(timeout=1.0)
        except queue.Empty:
            if not proc.is_alive():
                break
            continue
        if m[0] == "log":
            print(f"  [桌面版] {m[1]}")
        elif m[0] == "file" and m[2] in ("done", "failed", "transcribed", "no_speech", "skipped"):
            print(f"  [桌面版] {videos[m[1]].name}:{m[2]} {m[4]}")
        elif m[0] == "done":
            done = True
    proc.join(30)
    print(f"  [桌面版] 完成,共 {time.time() - t0:.0f} 秒")
    return {v.name: sorted(v.parent.glob(f"{v.stem}.*.srt")) for v in videos}


def _web(videos: list[Path], opts: dict, work: Path, batch: int | None, target: str | None):
    """照網頁版的流程跑一次;回傳({影片檔名: [srt]}, 項目清單)。"""
    os.environ["WEB_DATA_DIR"] = str(work)
    os.environ["WEB_DEV_LOGIN"] = "1"
    os.environ["WEB_BATCH_SIZE"] = str(batch or 0)
    if target:
        os.environ["WEB_WORKER_TARGET"] = target
    from app.web import config as config_mod
    from app.web import jobs, storage
    from app.web.db import DB
    from app.web.runner import Runner

    cfg = config_mod.load()
    db = DB(cfg.db_path)
    user = db.upsert_user("dev", "acceptance", None, "acceptance", status="approved")
    # 網頁版每人有自己的詞彙表;驗收時放入和桌面版相同的 config/glossary.txt,兩邊條件才一致
    from app.translate.glossary import GLOSSARY_FILE

    gf = cfg.glossary_file(user["id"])
    gf.parent.mkdir(parents=True, exist_ok=True)
    if GLOSSARY_FILE.is_file():
        shutil.copyfile(GLOSSARY_FILE, gf)
    items = []
    for v in videos:
        iid = storage.new_id()
        stored = storage.stored_name(v.name, "media", iid)
        _place(v, cfg.item_dir(user["id"], iid) / stored)
        items.append(db.create_item(user["id"], iid, v.name, "media", stored, v.stat().st_size))
    res = jobs.create_job(cfg, db, user, [i["id"] for i in items], "auto", opts)
    print(f"  [網頁版] 已建立工作,共 {len(res['job']['item_ids'])} 個檔案")
    job = db.claim_next_job()
    runner = Runner(cfg, db)
    t = threading.Thread(target=runner.run_job, args=(job,), daemon=True)
    t0 = time.time()
    t.start()
    seen = 0
    while t.is_alive():
        for e in db.events_after(user["id"], job["id"], seen):
            seen = e["seq"]
            if e["kind"] == "log":
                print(f"  [網頁版] {e['data']['text']}")
            elif e["kind"] == "file" and e["data"]["state"] != "running":
                print(f"  [網頁版] {e['data']['state']} {e['data']['message']}")
        time.sleep(1.0)
    t.join()
    for e in db.events_after(user["id"], job["id"], seen):
        if e["kind"] == "log":
            print(f"  [網頁版] {e['data']['text']}")
    final = db.get_job(user["id"], job["id"])
    print(f"  [網頁版] 工作狀態 {final['status']}" + (f":{final['error']}" if final["error"] else "")
          + f",共 {time.time() - t0:.0f} 秒")
    out = {}
    for it in db.list_items(user["id"]):
        d = storage.item_dir(cfg, it)
        out[it["name"]] = sorted(d.glob(f"{it['id']}.*.srt"))
    return out, db.list_items(user["id"])


def _suffix(p: Path, stem: str) -> str:
    return p.name[len(stem):]


def _read(p: Path) -> bytes:
    return p.read_bytes()


def compare(desk: dict[str, list[Path]], web: dict[str, list[Path]], stems: dict[str, tuple[str, str]]) -> bool:
    ok = True
    for name, (dstem, wstem) in stems.items():
        d = {_suffix(p, dstem): p for p in desk.get(name, [])}
        w = {_suffix(p, wstem): p for p in web.get(name, [])}
        print(f"\n● {name}")
        if not d and not w:
            print("  兩邊都沒有產生字幕(例如沒有人聲)。")
            continue
        for suf in sorted(set(d) | set(w)):
            if suf not in d or suf not in w:
                ok = False
                print(f"  ✗ {suf}:只有{'桌面版' if suf in d else '網頁版'}產生")
                continue
            a, b = _read(d[suf]), _read(w[suf])
            if a == b:
                print(f"  ✓ {suf}:完全相同({len(a)} 位元組)")
            else:
                ok = False
                print(f"  ✗ {suf}:不同(桌面版 {len(a)} 位元組,網頁版 {len(b)} 位元組)")
                la = a.decode("utf-8-sig", "replace").splitlines()
                lb = b.decode("utf-8-sig", "replace").splitlines()
                for line in list(difflib.unified_diff(la, lb, "桌面版", "網頁版", lineterm="", n=1))[:24]:
                    print("    " + line)
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.web.acceptance")
    ap.add_argument("videos", nargs="+", type=Path)
    ap.add_argument("--to", dest="tgt", default=None, help="翻譯目標:zh-TW / en / ja(不給就只轉錄)")
    ap.add_argument("--engine", default="m2m100")
    ap.add_argument("--source-language", default="auto")
    ap.add_argument("--batch", type=int, default=4, help="兩邊都固定使用的轉錄批次大小(預設 4)")
    ap.add_argument("--worker", default=None, help="測試用:'模組:函式' 取代真正的 worker.run")
    args = ap.parse_args(argv)

    from app.paths import setup_env

    setup_env()
    from app import settings as settings_mod

    videos = [v.resolve() for v in args.videos]
    for v in videos:
        if not v.is_file():
            print(f"找不到檔案:{v}")
            return 2
    work = Path(os.environ.get("ACCEPTANCE_DIR") or Path(__file__).resolve().parents[2] / "data" / "acceptance")
    work = work / time.strftime("%Y%m%d-%H%M%S")
    print(f"工作資料夾:{work}\n批次大小固定為 {args.batch};翻譯目標:{args.tgt or '(不翻譯)'}")

    # ---- 桌面版 ----
    print("\n[1/3] 桌面版的方式")
    dvideos = []
    for i, v in enumerate(videos):
        dst = work / "desktop" / f"v{i}{v.suffix.lower()}"
        _place(v, dst)
        dvideos.append(dst)
    s = settings_mod.load()
    s.update(translate_to=args.tgt, translation_engine=args.engine, source_language=args.source_language,
             batch_size=args.batch, overwrite=False)
    desk = _desktop(dvideos, s, args.worker)

    # ---- 網頁版 ----
    print("\n[2/3] 網頁版的方式")
    opts = {"translate_to": args.tgt, "translation_engine": args.engine, "source_language": args.source_language}
    web, items = _web(videos, opts, work / "web", args.batch, args.worker)

    # ---- 比對 ----
    print("\n[3/3] 比對")
    desk_by_name = {videos[i].name: desk[dvideos[i].name] for i in range(len(videos))}
    stems = {videos[i].name: (dvideos[i].stem, next(it["id"] for it in items if it["name"] == videos[i].name))
             for i in range(len(videos))}
    ok = compare(desk_by_name, web, stems)
    print("\n結論:" + ("全部相同,通過。" if ok else "有差異,請把上面的輸出貼給我。"))
    return 0 if ok else 1


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
