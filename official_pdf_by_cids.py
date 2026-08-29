#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国立公文書館デジタルアーカイブ — 官方 PDF 下载器(按件名命名, 自动发现子册)
https://www.digital.archives.go.jp/fonds/3611449?&page=9
原理:
  浏览器下载 PDF 调 POST https://www.digital.archives.go.jp/contentDownload/<ID>?type=imagePdf
  表单字段 cid[] 决定页面(路径 ID 必须是这些 cid 所属文档的 ID)。本脚本:
    1. 自动从 /file/(多册容器) 或 /img/(单册) 解析出册/页;
    2. 从 IIIF manifest 抽取每页的 cid(形如 da12/C102787836700);
    3. 按 <=100 页分块 POST contentDownload, 拿到官方原生 PDF(3000x2200), 合并;
    4. 按各册件名(label)命名。
  站点有间歇 Bot 限流(偶发返回 431 字节 HTML 而非 PDF)与大册 100 页上限, 均已用
  退避重试 + 页数校验(合并页数==cid数)处理。

用法:
  # 自动模式(推荐): 直接丢 URL 或 ID
  python3 official_pdf_by_cids.py https://www.digital.archives.go.jp/file/1079248
  python3 official_pdf_by_cids.py https://www.digital.archives.go.jp/img/4426991
  python3 official_pdf_by_cids.py 1079342 --outdir ./out

  # 手动模式: 已有 cid 列表(浏览器表单体, 每行 "cid" 换行 "da12/...")
  python3 official_pdf_by_cids.py --parent 1079342 --cids-file cids.txt --out 遁甲演義.pdf
  # 或 cid 为等差数列时
  python3 official_pdf_by_cids.py --parent 1079342 --cid-prefix da12/C1029109 \
      --start 59300 --end 69200 --step 100 --out 遁甲演義.pdf

依赖: pypdf(合并/校验) + curl(发请求)。
"""
import subprocess, sys, os, time, re, argparse, random, html
# 网络请求统一走 curl(自带证书库), 避免系统 Python 缺失 SSL 根证书导致 CERTIFICATE_VERIFY_FAILED

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REF = "https://www.digital.archives.go.jp/"
CHUNK = 100          # contentDownload 单请求硬上限(101页会被截断成100)
MAX_ATTEMPTS = 8
MAX_TIME = 360        # 单块下载超时(秒), 默认 6 分钟; 大册 100 页约 60-90MB 足够

# ---------- 网络 (curl, 自带证书, 不受系统 Python SSL 缺失影响) ----------
def http_get(url, timeout=30):
    cmd = ["curl", "-s", "--fail", "--max-time", str(timeout),
           "-A", UA, "-H", f"Referer: {REF}", url]
    last = None
    for _ in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
            if r.returncode == 0 and r.stdout:
                return r.stdout
            last = r.returncode
        except Exception as e:
            last = e
        time.sleep(2)
    raise RuntimeError(f"curl 取页面失败: {url} (rc={last})")

def http_get_text(url, timeout=30):
    return http_get(url, timeout).decode("utf-8", "replace")

# ---------- 解析输入 ----------
def resolve(url_or_id):
    s = url_or_id.strip().rstrip("/")
    m = re.search(r"/file/(\d+)", s)
    if m:
        return m.group(1), "file"
    m = re.search(r"/img/(\d+)", s)
    if m:
        return m.group(1), "img"
    m = re.fullmatch(r"\d+", s)
    if m:
        return s, "bare"
    raise SystemExit(f"无法识别的地址: {url_or_id}")

# ---------- 取册/页 ----------
def get_manifest(vid):
    return json.loads(http_get(f"https://www.digital.archives.go.jp/api/iiif/{vid}/manifest.json"))

def manifest_label_cids(vid):
    d = get_manifest(vid)
    label = (d.get("label") or "").strip() or vid
    cids = []
    for c in d.get("sequences", [{}])[0].get("canvases", []):
        for im in c.get("images", []):
            m = re.search(r"(da\d+/C\d+)", im.get("resource", {}).get("@id", ""))
            if m:
                cids.append(m.group(1))
    return label, cids

def get_parent_label(file_id):
    try:
        d = get_manifest(file_id)
        lbl = (d.get("label") or "").strip()
        if lbl:
            return lbl
    except Exception:
        pass
    # 回退: /file/ 页面 <title>(如 "本草綱目 | 国立公文書館デジタルアーカイブ")
    try:
        txt = http_get_text(f"https://www.digital.archives.go.jp/file/{file_id}")
        m = re.search(r"<title>(.*?)</title>", txt, re.I | re.S)
        if m:
            t = re.sub(r"\s*\|?\s*国立公文書館.*$", "", m.group(1), flags=re.S).strip()
            if t:
                return t
    except Exception:
        pass
    return file_id

def get_children(file_id):
    """从 /file/ 页面 HTML 抽取子册 /img/ ID(排除父ID本身)。"""
    txt = http_get_text(f"https://www.digital.archives.go.jp/file/{file_id}")
    ids = []
    seen = set()
    for m in re.finditer(r"/img/(\d+)", txt):
        i = m.group(1)
        if i == file_id:
            continue
        if i not in seen:
            seen.add(i)
            ids.append(i)
    return ids

# ---------- 下载单册 ----------
def post_chunk(vid, cids):
    cmd = ["curl", "-s", "--max-time", str(MAX_TIME), "-A", UA,
           "-H", f"Referer: {REF}", "-X", "POST",
           f"https://www.digital.archives.go.jp/contentDownload/{vid}?type=imagePdf"]
    for c in cids:
        cmd += ["--data-urlencode", f"cid={c}"]
    return subprocess.run(cmd, capture_output=True, timeout=MAX_TIME + 30).stdout

def download_volume(vid, label, out_path):
    """下载单册全部页(分块<=100), 合并校验, 返回总页数或 0。"""
    from pypdf import PdfReader, PdfWriter
    _, cids = manifest_label_cids(vid)
    if not cids:
        print(f"  [warn] {vid} {label}: 无 cid, 跳过"); return 0
    chunks = [cids[i:i + CHUNK] for i in range(0, len(cids), CHUNK)]
    print(f"  册 {vid} 《{label}》 {len(cids)} 页, 分 {len(chunks)} 块")
    parts = []
    for ci, ch in enumerate(chunks, 1):
        data = None
        for att in range(1, MAX_ATTEMPTS + 1):
            data = post_chunk(vid, ch)
            if data[:4] != b"%PDF":
                print(f"    [挑战页] 块{ci} 尝试{att}: magic={data[:4]} size={len(data)} -> 退避")
                time.sleep(min(8 * att, 60) + random.uniform(0, 3))
                continue
            # 校验该块页数==len(ch)
            try:
                tp = f"/tmp/_vol_{vid}_{ci}.pdf"
                open(tp, "wb").write(data)
                np = len(PdfReader(tp).pages)
                if np == len(ch):
                    parts.append(tp)
                    print(f"    [ok] 块{ci}: {np}/{len(ch)} 页, {len(data)//1024}KB")
                    break
                print(f"    [截断] 块{ci} 尝试{att}: {np}/{len(ch)} 页 -> 重试")
            except Exception as e:
                print(f"    [坏PDF] 块{ci} 尝试{att}: {e} -> 重试")
            time.sleep(min(8 * att, 60) + random.uniform(0, 3))
        else:
            print(f"  [FAIL] 册 {vid} 块{ci} 下载失败"); return 0
    # 合并
    w = PdfWriter()
    total = 0
    for p in parts:
        rd = PdfReader(p)
        for pg in rd.pages:
            w.add_page(pg)
        total += len(rd.pages)
    with open(out_path, "wb") as f:
        w.write(f)
    for p in parts:
        try: os.remove(p)
        except OSError: pass
    if total != len(cids):
        print(f"  [warn] {vid} 合并页数 {total} != cid数 {len(cids)}")
    return total

# ---------- 主流程 ----------
def main():
    global MAX_TIME
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", help="URL 或 ID(/file/ /img/ 或纯数字)")
    ap.add_argument("--parent", help="手动模式: 路径 ID")
    ap.add_argument("--cids-file", help="手动模式: 浏览器表单体(含 cid 行)")
    ap.add_argument("--cid-prefix", default="da12/C1029109")
    ap.add_argument("--start", type=int)
    ap.add_argument("--end", type=int)
    ap.add_argument("--step", type=int, default=100)
    ap.add_argument("--out", help="手动模式输出文件")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--max-time", type=int, default=MAX_TIME,
                    help="单块 curl 下载超时(秒), 默认 360=6分钟")
    args = ap.parse_args()
    MAX_TIME = args.max_time

    # ---- 手动 cid 模式 ----
    if args.parent:
        cids = []
        if args.cids_file:
            for line in open(args.cids_file, encoding="utf-8"):
                line = line.strip()
                if line == "cid":
                    continue
                if re.match(r"da\d+/C\d+", line):
                    cids.append(line)
        else:
            assert args.start is not None and args.end is not None
            cids = [f"{args.cid_prefix}{i:06d}" for i in range(args.start, args.end + 1, args.step)]
        out = args.out or f"{args.parent}.pdf"
        total = download_volume_manual(args.parent, cids, out)
        print(f"完成: {out}  页数={total}")
        return

    # ---- 自动模式 ----
    assert args.target, "需提供 target 或 --parent"
    vid, kind = resolve(args.target)
    os.makedirs(args.outdir, exist_ok=True)
    if kind == "file":
        pname = get_parent_label(vid)
        children = get_children(vid)
        folder = os.path.join(args.outdir, pname)
        os.makedirs(folder, exist_ok=True)
        vols = children if children else [vid]
        print(f"容器 {vid} 《{pname}》: {len(vols)} 册 -> {folder}")
        for v in vols:
            label, _ = manifest_label_cids(v)
            out = os.path.join(folder, f"{label}.pdf")
            if os.path.exists(out):
                print(f"跳过已存在: {out}"); continue
            total = download_volume(v, label, out)
            print(f"  -> {out}  ({total} 页)")
            time.sleep(2)
    else:  # img / bare -> 单册
        label, _ = manifest_label_cids(vid)
        out = os.path.join(args.outdir, f"{label}.pdf")
        total = download_volume(vid, label, out)
        print(f"完成: {out}  ({total} 页)")

def download_volume_manual(pid, cids, out_path):
    """手动 cid 列表: 直接分块下载(路径ID=pid)。"""
    from pypdf import PdfReader, PdfWriter
    chunks = [cids[i:i + CHUNK] for i in range(0, len(cids), CHUNK)]
    print(f"共 {len(cids)} 个 cid, 分 {len(chunks)} 块")
    parts = []
    for ci, ch in enumerate(chunks, 1):
        data = None
        for att in range(1, MAX_ATTEMPTS + 1):
            data = post_chunk(pid, ch)
            if data[:4] != b"%PDF":
                print(f"  [挑战页] 块{ci} 尝试{att}: magic={data[:4]} -> 退避")
                time.sleep(min(8 * att, 60) + random.uniform(0, 3)); continue
            try:
                tp = f"/tmp/_man_{ci}.pdf"; open(tp, "wb").write(data)
                np = len(PdfReader(tp).pages)
                if np == len(ch):
                    parts.append(tp); print(f"  [ok] 块{ci}: {np} 页"); break
                print(f"  [截断] 块{ci} 尝试{att}: {np}/{len(ch)} -> 重试")
            except Exception as e:
                print(f"  [坏PDF] 块{ci} 尝试{att}: {e} -> 重试")
            time.sleep(min(8 * att, 60) + random.uniform(0, 3))
        else:
            print(f"[FAIL] 块{ci} 失败"); return 0
    w = PdfWriter(); total = 0
    for p in parts:
        rd = PdfReader(p)
        for pg in rd.pages: w.add_page(pg)
        total += len(rd.pages)
        os.remove(p)
    with open(out_path, "wb") as f: w.write(f)
    return total

if __name__ == "__main__":
    import json
    main()
