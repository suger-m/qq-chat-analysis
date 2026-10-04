# -*- coding: utf-8 -*-
"""web.py — QQ 聊天记录可视化 (Flask, 无外部静态依赖)

  python web.py [--port 8765]   或   python cli.py web
"""
import os
import sys
import time
import socket
import hashlib
import ipaddress
import threading
import mimetypes
import urllib.request
import urllib.parse

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request, Response, send_file, abort

import qqlib
# Media paths are resolved for the active store account.

app = Flask(__name__)
STORE = None
_lock = threading.Lock()


def store():
    global STORE
    if STORE is None:
        STORE = qqlib.QQStore()
    return STORE


def fmt_time(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")


# ── API ──────────────────────────────────────────────────────────────────────

@app.get("/api/account")
def api_account():
    s = store()
    return jsonify({"account": s.account, "db": s.nt_path})


@app.get("/api/media")
def api_media():
    """提供 nt_data 下的媒体文件 (严格限制在 NT_DATA_DIR 内)"""
    rel = request.args.get("p", "").replace("\\", "/")
    base = os.path.realpath(store().media_dir)
    full = os.path.realpath(os.path.join(base, rel))
    if not (full == base or full.startswith(base + os.sep)):
        abort(403)
    if not os.path.isfile(full):
        abort(404)
    mt = mimetypes.guess_type(full)[0] or "application/octet-stream"
    if full.lower().endswith(".amr"):
        mt = "audio/amr"          # SILK 语音, 浏览器不支持时提供下载
    resp = send_file(full, mimetype=mt, conditional=True)
    resp.headers["Cache-Control"] = "max-age=604800"
    return resp


_MF_HOSTS = ("https://i.gtimg.cn", "https://gxh.vip.qq.com")


def _fetch_mf(md5, size):
    """从 QQ 表情 CDN 拉取商城表情图片 (见 mirai#1258 的 URL 规则)。"""
    for host in _MF_HOSTS:
        url = "%s/club/item/parcel/item/%s/%s/%sx%s.png" % (
            host, md5[:2], md5, size, size)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                if getattr(r, "status", 200) != 200:
                    continue
                data = r.read(4 * 1024 * 1024)
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                return data
        except Exception:
            continue
    return None


@app.get("/api/mf")
def api_mf():
    """商城表情(超级表情)图片: 本地无图时按 80903(md5) 从 CDN 取并落盘缓存。

    经本地代理返回, 规避浏览器直连 CDN 的 TLS/跨域问题; 拉取失败返回 404,
    由前端回退为文字气泡。
    """
    h = (request.args.get("h") or "").lower()
    if len(h) != 32 or any(c not in "0123456789abcdef" for c in h):
        abort(400)
    size = request.args.get("sz", "300")
    if size not in ("200", "300"):
        size = "300"
    cache_dir = os.path.join(qqlib.CACHE_DIR, "mf")
    fn = os.path.join(cache_dir, "%s_%s.png" % (h, size))
    if not (os.path.isfile(fn) and os.path.getsize(fn) > 64):
        data = _fetch_mf(h, size)
        if not data:
            abort(404)
        os.makedirs(cache_dir, exist_ok=True)
        tmp = fn + ".part"
        with open(tmp, "wb") as fw:
            fw.write(data)
        os.replace(tmp, fn)
    resp = send_file(fn, mimetype="image/png", conditional=True)
    resp.headers["Cache-Control"] = "max-age=604800"
    return resp


_IMG_DIR = os.path.join(qqlib.CACHE_DIR, "img")
_IMG_LOCK = threading.Lock()


# 本机代理(Clash 等)常开 fake-ip 模式, 把任意域名都解析到 198.18.0.0/15,
# 会被 ipaddress 判成 is_private —— 必须把这段排除, 否则所有图片都拉不到。
_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")


def _bad_ip(ip):
    if ip in _FAKE_IP:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _url_ok(u):
    """只放行 http(s) 且不指向内网的 URL (本地工具也要防 SSRF)"""
    try:
        p = urllib.parse.urlparse(u)
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname.lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        return False
    try:                                   # 字面量 IP: 直接判定
        return not _bad_ip(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:                                   # 域名: 逐个解析结果判定
        for ai in socket.getaddrinfo(host, None):
            if _bad_ip(ipaddress.ip_address(ai[4][0].split("%")[0])):
                return False
    except Exception:
        return False
    return True


def _img_paths(u):
    h = hashlib.sha1(u.encode("utf-8")).hexdigest()
    return os.path.join(_IMG_DIR, h), os.path.join(_IMG_DIR, h + ".ct")


def _fetch_img(u):
    req = urllib.request.Request(u, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"),
        "Referer": "https://qq.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = r.read(8 * 1024 * 1024)
    if not ct.startswith("image/") or len(data) < 32:
        return None, None
    return data, ct


@app.get("/api/img")
def api_img():
    """卡片缩略图/图标代理: 服务端取回后落盘缓存, 规避浏览器直连 CDN 的
    TLS/跨域/防盗链问题。同一 URL 只拉一次, 失败返回 404 由前端隐藏图。"""
    u = request.args.get("u", "")
    if not u or len(u) > 2000 or not _url_ok(u):
        abort(400)
    fn, ct_file = _img_paths(u)
    if not (os.path.isfile(fn) and os.path.getsize(fn) > 32 and os.path.isfile(ct_file)):
        with _IMG_LOCK:
            if not (os.path.isfile(fn) and os.path.getsize(fn) > 32):
                try:
                    data, ct = _fetch_img(u)
                except Exception:
                    data, ct = None, None
                if not data:
                    abort(404)
                os.makedirs(_IMG_DIR, exist_ok=True)
                with open(fn + ".part", "wb") as fw:
                    fw.write(data)
                os.replace(fn + ".part", fn)
                with open(ct_file, "w", encoding="utf-8") as fw:
                    fw.write(ct)
    try:
        with open(ct_file, encoding="utf-8") as fr:
            ct = fr.read().strip() or "image/png"
    except OSError:
        ct = "image/png"
    resp = send_file(fn, mimetype=ct, conditional=True)
    resp.headers["Cache-Control"] = "max-age=604800"
    return resp


@app.get("/api/groups")
def api_groups():
    with _lock:
        gs = store().groups_overview()
    return jsonify(gs)


@app.get("/api/messages")
def api_messages():
    s = store()
    gid = request.args.get("gid", "")
    limit = min(int(request.args.get("limit", 30)), 200)
    offset = int(request.args.get("offset", 0))
    after = request.args.get("after", type=int)
    before = request.args.get("before", type=int)
    msgs = s.read_group(gid, limit=limit, before_ts=before,
                        after_ts=after, offset=offset)
    for m in msgs:
        m["nick"] = s.nick_of(m["uin"], m["nick"])
        m["time"] = fmt_time(m["ts"])
    return jsonify(msgs)


@app.get("/api/c2c")
def api_c2c():
    s = store()
    peer = request.args.get("peer", "")
    limit = min(int(request.args.get("limit", 30)), 200)
    offset = int(request.args.get("offset", 0))
    msgs = s.read_c2c(peer, limit=limit, offset=offset)
    for m in msgs:
        m["time"] = fmt_time(m["ts"])
    return jsonify(msgs)


@app.get("/api/search")
def api_search():
    s = store()
    kw = request.args.get("kw", "").strip()
    if not kw:
        return jsonify([])
    gid = request.args.get("gid") or None
    limit = min(int(request.args.get("limit", 30)), 100)
    results = s.search(kw, gid=gid, limit=limit)
    for r in results:
        r["time"] = fmt_time(r["ts"])
    return jsonify(results)


@app.get("/api/stats")
def api_stats():
    s = store()
    gid = request.args.get("gid") or None
    with _lock:
        daily = s.stats_daily(days=30, gid=gid)
        hours = s.stats_hours(gid=gid)
        top = s.stats_top(gid, 20) if gid else []
    return jsonify({"daily": daily, "hours": hours, "top": top})


# ── 前端 ─────────────────────────────────────────────────────────────────────

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>QQ 聊天记录</title>
<style>
:root{--bg:#14161a;--panel:#1c1f26;--panel2:#232730;--bd:#2e3340;--fg:#d7dce4;--dim:#8a92a3;--acc:#4f8cff;--acc2:#3ad29f}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.6 "Segoe UI",system-ui,"Microsoft YaHei",sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{padding:10px 18px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:14px;background:var(--panel)}
header h1{font-size:16px;font-weight:600}
header .acct{color:var(--dim);font-size:12px}
#wrap{flex:1;display:flex;overflow:hidden}
#side{width:270px;border-right:1px solid var(--bd);display:flex;flex-direction:column;background:var(--panel)}
#side .search{padding:10px}
#side input{width:100%;padding:6px 10px;border-radius:6px;border:1px solid var(--bd);background:var(--panel2);color:var(--fg);outline:none}
#glist{flex:1;overflow-y:auto;padding:4px}
.gitem{padding:7px 10px;border-radius:6px;cursor:pointer;display:flex;justify-content:space-between;gap:6px}
.gitem:hover{background:var(--panel2)}
.gitem.on{background:#2a3550}
.gitem .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gitem .ct{color:var(--dim);font-size:12px;flex-shrink:0}
#main{flex:1;display:flex;flex-direction:column;overflow:hidden}
#tabs{display:flex;border-bottom:1px solid var(--bd);background:var(--panel)}
#tabs button{background:none;border:none;color:var(--dim);padding:10px 22px;cursor:pointer;font-size:14px;border-bottom:2px solid transparent}
#tabs button.on{color:var(--fg);border-bottom-color:var(--acc)}
#pane{flex:1;overflow-y:auto;padding:16px 22px}
.msg{padding:5px 8px;border-radius:6px;margin-bottom:2px}
.msg:hover{background:var(--panel2)}
.msg .hd{font-size:12px;color:var(--dim)}
.msg .hd b{color:var(--acc2);font-weight:600;margin-right:6px}
.msg .tx{white-space:pre-wrap;word-break:break-word}
#loadmore{margin:12px auto;display:block;padding:7px 26px;border-radius:6px;border:1px solid var(--bd);background:var(--panel2);color:var(--fg);cursor:pointer}
#toolbar{padding:8px 22px;border-bottom:1px solid var(--bd);display:flex;gap:10px;align-items:center;background:var(--panel)}
#toolbar select,#toolbar input{padding:5px 10px;border-radius:6px;border:1px solid var(--bd);background:var(--panel2);color:var(--fg);outline:none}
.bar{display:inline-block;height:18px;background:linear-gradient(90deg,var(--acc),#7aa8ff);border-radius:3px;min-width:1px}
.statrow{display:flex;align-items:center;gap:10px;margin:3px 0;font-size:13px}
.statrow .lb{width:90px;color:var(--dim);flex-shrink:0;text-align:right}
.statrow .v{width:70px;flex-shrink:0}
table.top{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
table.top td,table.top th{padding:5px 8px;border-bottom:1px solid var(--bd);text-align:left}
h3.sec{margin:18px 0 8px;color:var(--fg);font-size:14px}
.dim{color:var(--dim);font-size:12px}
#searchbox{display:flex;gap:8px;margin-bottom:12px}
#searchbox input{flex:1;padding:8px 12px;border-radius:6px;border:1px solid var(--bd);background:var(--panel2);color:var(--fg);outline:none}
#searchbox button{padding:8px 20px;border-radius:6px;border:none;background:var(--acc);color:#fff;cursor:pointer}
mark{background:#5a4a1f;color:#ffd76a;padding:0 2px;border-radius:2px}
.empty{color:var(--dim);text-align:center;padding:40px 0}
img.emo{display:block;margin:4px 0;cursor:zoom-in;border-radius:6px;background:#0000}
img.emo.face{max-width:96px;max-height:96px}
img.emo.mf{max-width:160px;max-height:160px}
.chip{display:inline-block;padding:3px 10px;margin:3px 0;border-radius:999px;
  background:linear-gradient(135deg,#2a3550,#3a2a50);border:1px solid var(--bd);
  font-size:13px;color:var(--fg);vertical-align:middle}
.chip .ic{margin-right:4px}
.quote{border-left:3px solid var(--acc);background:var(--panel2);border-radius:4px;
  padding:3px 9px;margin:3px 0;font-size:12.5px;color:var(--dim);
  white-space:pre-wrap;word-break:break-word}
.sticker-empty{font-size:12px;color:var(--dim);background:var(--panel2);
  border:1px dashed var(--bd);border-radius:6px;padding:4px 9px;display:inline-block;margin:3px 0}
.msg.grp{margin-top:-2px}
.msg.grp .hd{display:none}
.msg .hd .tm{color:#5f6675;margin-left:6px}
.msg .acts{float:right;opacity:0;transition:opacity .12s}
.msg:hover .acts{opacity:1}
.msg .acts button{background:var(--panel2);border:1px solid var(--bd);color:var(--dim);
  border-radius:5px;font-size:11px;padding:1px 7px;cursor:pointer;font-family:inherit}
.msg .acts button:hover{color:var(--fg);border-color:var(--acc)}
img.picb{max-width:min(340px,62vw);max-height:260px;border-radius:8px;cursor:zoom-in;
  display:block;margin:4px 0;background:#0d0f13}
.imeta{font-size:11px;color:var(--dim);margin:2px 0 4px}
.filecard{display:inline-flex;align-items:center;gap:8px;padding:7px 12px;margin:3px 0;
  border-radius:8px;background:var(--panel2);border:1px solid var(--bd);
  text-decoration:none;color:var(--fg);max-width:100%}
.filecard:hover{border-color:var(--acc)}
.filecard .fn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:360px}
.filecard .fs{color:var(--dim);font-size:11px;flex-shrink:0}
/* ── 卡片消息 (小程序 / 图文分享 / 音乐 / 名片 / 位置 / 群公告 / XML) ── */
.card{display:flex;gap:10px;align-items:flex-start;max-width:min(440px,74vw);
  background:var(--panel2);border:1px solid var(--bd);border-radius:10px;padding:10px;
  margin:6px 0;text-decoration:none;color:inherit;box-sizing:border-box;
  transition:border-color .15s,background .15s}
a.card:hover{border-color:var(--acc);background:#28303d}
.card .cth{width:54px;height:54px;border-radius:8px;object-fit:cover;flex:0 0 54px;
  background:var(--panel);display:block}
.card .cth.big{width:74px;height:74px;flex:0 0 74px;border-radius:8px}
.card .cth.noimg{display:flex;align-items:center;justify-content:center;font-size:24px;opacity:.75}
.card .cbd{min-width:0;flex:1 1 auto}
.card .cti{font-weight:600;font-size:13.5px;line-height:1.35;overflow:hidden;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.card .cde{font-size:12px;color:var(--dim);margin-top:4px;line-height:1.45;overflow:hidden;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.card .cfg{font-size:11px;color:var(--dim);margin-top:6px;display:flex;align-items:center;gap:5px;
  overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.card .cfg img{width:14px;height:14px;border-radius:3px;flex:0 0 14px;object-fit:cover}
.card .cfg .go{color:var(--acc);flex-shrink:0}
/* ── 机器人 Markdown (45002=14) ── */
.mdbody{max-width:min(660px,82vw);font-size:13px;line-height:1.62;word-break:break-word}
.mdbody h2,.mdbody h3,.mdbody h4{margin:9px 0 4px;font-size:14px;font-weight:700;
  border-bottom:1px solid var(--bd);padding-bottom:3px}
.mdbody h5{margin:6px 0 3px;font-size:13px;font-weight:700}
.mdbody img{max-width:min(300px,62vw);border-radius:6px;display:block;margin:4px 0;cursor:zoom-in}
.mdbody ul{margin:2px 0;padding-left:19px}
.mdbody li{margin:1px 0}
.mdbody code{background:var(--panel2);border:1px solid var(--bd);border-radius:4px;
  padding:0 4px;font-family:ui-monospace,Consolas,monospace;font-size:12px}
.mdbody pre{background:var(--panel2);border:1px solid var(--bd);border-radius:6px;
  padding:8px 10px;overflow-x:auto;margin:5px 0;font-size:12px}
.mdbody pre code{border:0;background:none;padding:0}
.mdbody blockquote{border-left:3px solid var(--bd);margin:5px 0;padding:1px 0 1px 9px;color:var(--dim)}
.mdbody hr{border:0;border-top:1px solid var(--bd);margin:9px 0}
.mdbody a{color:var(--acc);text-decoration:none}
.mdbody a:hover{text-decoration:underline}
/* ── 机器人按钮组 (45002=17) ── */
.btnrow{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 2px;max-width:min(660px,82vw)}
.btnrow .bt{font-size:12px;padding:5px 13px;border-radius:7px;background:var(--panel2);
  border:1px solid var(--bd);color:var(--fg);text-decoration:none;cursor:pointer;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:240px}
.btnrow a.bt:hover{border-color:var(--acc);color:#fff}
.btnrow span.bt{color:var(--dim);cursor:default}
.tip{font-size:12px;color:var(--dim);background:var(--panel2);border:1px solid var(--bd);
  border-radius:999px;padding:3px 11px;display:inline-block;margin:3px 0}
.tx a{color:var(--acc);text-decoration:none;border-bottom:1px dotted var(--acc);word-break:break-all}
.tx a:hover{border-bottom-style:solid}
#main{position:relative}
#jump{position:absolute;right:26px;bottom:22px;padding:7px 16px;border-radius:999px;
  border:1px solid var(--bd);background:var(--panel2);color:var(--fg);cursor:pointer;
  display:none;box-shadow:0 4px 14px #0007;z-index:6;font-size:13px}
#jump.on{display:block}
#lb{position:fixed;inset:0;background:rgba(8,10,14,.93);display:none;
  align-items:center;justify-content:center;z-index:99;cursor:zoom-out}
#lb.on{display:flex}
#lb img{max-width:94vw;max-height:88vh;border-radius:6px}
#lb .cap{position:absolute;bottom:16px;left:0;right:0;text-align:center;color:#9aa3b2;font-size:12px}
</style></head><body>
<header><h1>QQ 聊天记录</h1><span class="acct" id="acct"></span><span class="dim">本地解密库 · 纯离线</span></header>
<div id="wrap">
  <div id="side">
    <div class="search"><input id="gfilter" placeholder="筛选群…"></div>
    <div id="glist"><div class="empty">加载中…(首次建缓存约 1 分钟)</div></div>
  </div>
  <div id="main">
    <div id="tabs">
      <button data-t="msg" class="on">消息</button>
      <button data-t="search">搜索</button>
      <button data-t="stats">统计</button>
    </div>
    <div id="toolbar"></div>
    <div id="pane"><div class="empty">← 从左侧选择一个群</div></div>
    <button id="jump">↓ 回到最新</button>
  </div>
</div>
<div id="lb"><img alt=""><div class="cap"></div></div>
<script>
let GROUPS=[],CUR=null,TAB='msg',MSGS=[],OFFSET=0,RANGE=0;
const $=s=>document.querySelector(s);
window.onerror=(m,s,l,c)=>{const el=$('#pane');if(el)el.insertAdjacentHTML('afterbegin','<div class="empty" style="color:#f66">JS错误: '+esc(String(m))+' @'+l+':'+c+'</div>')};
function esc(s){s=(s==null?'':String(s));return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function mfFail(el){const nm=el.dataset.nm||'表情';el.outerHTML='<span class="chip"><span class="ic">😀</span>'+esc(nm)+'</span>'}
const URLRE=new RegExp("https?://[^\\s<]+","g");
function linkify(e){return e.replace(URLRE,'<a href="$1" target="_blank" rel="noopener">$1</a>')}
function fmtSize(n){if(!n)return '';return n>=1048576?(n/1048576).toFixed(1)+' MB':n>=1024?(n/1024).toFixed(0)+' KB':n+' B'}
function toast(s){const d=document.createElement('div');d.textContent=s;
  d.style.cssText='position:fixed;left:50%;bottom:64px;transform:translateX(-50%);background:#2a3550;color:#d7dce4;padding:7px 16px;border-radius:999px;font-size:12px;z-index:200;border:1px solid #2e3340';
  document.body.appendChild(d);setTimeout(()=>d.remove(),1300)}
const LB=document.getElementById('lb');
function lg(u,cap){LB.querySelector('img').src=u;LB.querySelector('.cap').textContent=cap||'';LB.classList.add('on')}
LB.onclick=()=>{LB.classList.remove('on');LB.querySelector('img').removeAttribute('src')};
document.addEventListener('keydown',e=>{if(e.key==='Escape')LB.classList.remove('on')});
function copyMsg(i){const m=MSGS[i];if(!m)return;navigator.clipboard.writeText(m.text||'').then(()=>toast('已复制消息文本'),()=>toast('复制失败'))}
function hilight(t,kw){if(!kw)return esc(t);const e=esc(t),ke=esc(kw);return e.replace(new RegExp(ke.replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&'),'g'),m=>'<mark>'+m+'</mark>')}
// 带重试的 fetch: 服务重启/缓存构建期间自动等待重试
async function fetchJSON(url,retries=60,delay=2000){
  for(let i=0;i<retries;i++){
    try{
      const r=await fetch(url);
      if(r.ok)return await r.json();
    }catch(e){}
    if(i===0)$('#glist').innerHTML='<div class="empty">连接中/缓存构建中…<br><span class="dim">自动重试 ('+(i+1)+')</span></div>';
    await new Promise(res=>setTimeout(res,delay));
  }
  $('#glist').innerHTML='<div class="empty">连接失败, 请刷新页面</div>';
  return null;
}
(async()=>{
  const acct=await fetchJSON('/api/account');
  if(acct)$('#acct').textContent='账号 '+acct.account;
  const gs=await fetchJSON('/api/groups');
  if(!gs)return;
  GROUPS=gs;renderGroups();
  if(gs.length){selectGroup(gs[0].id)}else{$('#glist').innerHTML='<div class="empty">没有群消息数据</div>'}
})();
function renderGroups(){
  const f=$('#gfilter').value.trim();
  const list=GROUPS.filter(g=>!f||(g.name||'').includes(f)||g.id.includes(f));
  $('#glist').innerHTML=list.map(g=>`<div class="gitem${g.id===CUR?' on':''}" onclick="selectGroup('${g.id}')">
    <span class="nm">${esc(g.name||'未命名群 '+g.id)}</span><span class="ct">${(g.count/10000).toFixed(1)}万</span></div>`).join('')||'<div class="empty">无匹配</div>';
}
$('#gfilter').oninput=renderGroups;

// 轻量重试: 网络错误/非200时快速重试3次 (掩盖偶发连接抖动)
async function J(url){
  let lastErr=null;
  for(let i=0;i<4;i++){
    try{
      const r=await fetch(url);
      if(r.ok)return await r.json();
      lastErr=new Error('HTTP '+r.status);
    }catch(e){lastErr=e}
    if(i<3)await new Promise(res=>setTimeout(res,1200));
  }
  throw lastErr||new Error('请求失败');
}
function selectGroup(id){
  CUR=id;TAB='msg';OFFSET=0;MSGS=[];
  document.querySelectorAll('.gitem').forEach(e=>e.classList.remove('on'));
  renderGroups();
  gname=(GROUPS.find(g=>g.id===id)||{}).name||id;
  $('#toolbar').innerHTML=`<b>${esc(gname)}</b><span class="dim">${id}</span>
    <select onchange="loadMsgs(true)">
      <option value="0">全部</option><option value="86400000">今天…</option>
      <option value="604800000">近 7 天</option><option value="2592000000">近 30 天</option>
    </select>`;
  setTab('msg');  // 内部会调用 loadMsgs(true), 避免双重加载
}
function rangeTs(){const v=parseInt($('#toolbar select')?.value||0);return v?Date.now()-v:0}
function loadMsgs(reset){
  if(reset){OFFSET=0;MSGS=[];$('#pane').innerHTML='<div class="empty">加载中…</div>'}
  const after=Math.floor(rangeTs()/1000);
  J(`/api/messages?gid=${CUR}&limit=50&offset=${OFFSET}&after=${after||''}`)
  .then(ms=>{
    if(reset)MSGS=ms;else{const seen=new Set(MSGS.map(m=>m.rowid));MSGS=MSGS.concat(ms.filter(m=>!seen.has(m.rowid)))}
    OFFSET+=ms.length;renderMsgs(ms.length>=50);
  }).catch(e=>{$('#pane').innerHTML='<div class="empty">加载失败: '+esc(String(e&&e.message||e)).slice(0,200)+'<br><span class="dim">点左侧群重试; 若持续失败请把此错误信息发给助手</span></div>'});
}
// ── 卡片 / 机器人 Markdown / 按钮组 渲染 ─────────────────────────────────────
const CARD_ICON={miniapp:'🔗',news:'📰',music:'🎵',contact:'👤',location:'📍',
  announce:'📢',forum:'💬',record:'📋',xml:'🔗'};
function pimg(u){return '/api/img?u='+encodeURIComponent(u)}
function imgFallback(el,ic){
  const c=el.classList.contains('big'),d=document.createElement('div');
  d.className='cth noimg'+(c?' big':'');d.textContent=ic||'🔗';
  el.parentNode.replaceChild(d,el);
}
function cardHtml(x){
  const th=x.thumb?pimg(x.thumb):'',ic=x.icon?pimg(x.icon):'';
  const link=/^https?:/.test(x.url||'');
  const big=(x.card==='news'||x.card==='music'||x.card==='record');
  const ic0=CARD_ICON[x.card]||'🔗';
  const inner=(th?`<img class="cth${big?' big':''}" loading="lazy" src="${th}" onerror="imgFallback(this,'${ic0}')">`
                 :`<div class="cth noimg${big?' big':''}">${ic0}</div>`)
    +`<div class="cbd"><div class="cti">${esc(x.title||'(无标题)')}</div>`
    +(x.desc?`<div class="cde">${esc(x.desc)}</div>`:'')
    +`<div class="cfg">${ic?`<img loading="lazy" src="${ic}" onerror="this.remove()">`:''}`
    +`<span>${esc(x.tag||'卡片')}</span>${link?'<span class="go">· 点击打开</span>':''}</div></div>`;
  return link
    ?`<a class="card" href="${esc(x.url)}" target="_blank" rel="noopener">${inner}</a>`
    :`<div class="card">${inner}</div>`;
}
// 极简 Markdown: 图片/链接/粗体/行内码/标题/引用/列表/围栏代码/分隔线
function mdInline(t){
  const tk=[];
  t=t.replace(/!\\[[^\\]]*\\]\\(([^)\\s]+)\\)/g,(m,u)=>{tk.push(`<img loading="lazy" src="${pimg(u)}" onclick="lg(this.src,'')">`);return '@@T'+(tk.length-1)+'@@'});
  t=t.replace(/\\[([^\\]]+)\\]\\(([^)\\s]+)\\)/g,(m,tx,u)=>{tk.push(`<a href="${esc(u)}" target="_blank" rel="noopener">${esc(tx)}</a>`);return '@@T'+(tk.length-1)+'@@'});
  t=esc(t);
  t=t.replace(/\\*\\*([^*]+)\\*\\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>');
  t=t.replace(/https?:\\/\\/[^\\s<]+/g,m=>`<a href="${m}" target="_blank" rel="noopener">${m}</a>`);
  return t.replace(/@@T(\\d+)@@/g,(m,i)=>tk[+i]);
}
function mdHtml(src){
  const s=String(src||'').replace(/^\\[\\]\\(%7B[^)]*\\)\\s*/,'').replace(/\\r/g,'');
  const out=[];const code=[];let inList=false,inCode=false;
  for(const raw of s.split('\\n')){
    const ln=raw.replace(/\\s+$/,'');
    if(/^\\s*```/.test(ln)){
      if(inCode){out.push('<pre>'+esc(code.join('\\n'))+'</pre>');code.length=0;inCode=false}
      else{inCode=true}
      continue;
    }
    if(inCode){code.push(raw);continue}
    if(/^\\s*(?:-{3,}|_{3,}|\\*{3,})\\s*$/.test(ln)){if(inList){out.push('</ul>');inList=false}out.push('<hr>');continue}
    let m;
    if((m=ln.match(/^(#{1,6})\\s+(.*)$/))){if(inList){out.push('</ul>');inList=false}
      const lv=Math.min(m[1].length+1,5);out.push('<h'+lv+'>'+mdInline(m[2])+'</h'+lv+'>');continue}
    if((m=ln.match(/^>\\s?(.*)$/))){if(inList){out.push('</ul>');inList=false}
      out.push('<blockquote>'+mdInline(m[1])+'</blockquote>');continue}
    if((m=ln.match(/^\\s*[-*+]\\s+(.*)$/))){if(!inList){out.push('<ul>');inList=true}
      out.push('<li>'+mdInline(m[1])+'</li>');continue}
    if(!ln.trim()){if(inList){out.push('</ul>');inList=false}continue}
    if(inList){out.push('</ul>');inList=false}
    out.push('<div>'+mdInline(ln)+'</div>');
  }
  if(inList)out.push('</ul>');
  if(inCode&&code.length)out.push('<pre>'+esc(code.join('\\n'))+'</pre>');
  return '<div class="mdbody">'+out.join('')+'</div>';
}
function btnHtml(x){
  return (x.btns||[]).map(r=>'<div class="btnrow">'+r.map(b=>{
    const u=b.u||'';
    return /^https?:/.test(u)
      ?`<a class="bt" href="${esc(u)}" target="_blank" rel="noopener">${esc(b.t)}</a>`
      :`<span class="bt" title="需在 QQ 客户端内使用">${esc(b.t)}</span>`;
  }).join('')+'</div>').join('');
}
function renderMsgs(more){
  const pn=$('#pane');
  if(!MSGS.length){pn.innerHTML='<div class="empty">该范围内没有消息</div>';return}
  const prevH=pn.scrollHeight,prevT=pn.scrollTop;
  function mediaHtml(m){
    if(!m.media||!m.media.length)return '';
    return m.media.map(x=>{
      const u=x.web?'/api/media?p='+encodeURIComponent(x.web):null;
      const tu=x.thumb?'/api/media?p='+encodeURIComponent(x.thumb):null;
      const nm=esc(x.name||'');
      if(x.kind==='pic'){
        if(!u)return `<div class="dim">🖼 [图片] ${nm} (原图已过期)</div>`;
        const meta=[x.w&&x.h?x.w+'×'+x.h:'',fmtSize(x.size)].filter(Boolean).join(' · ');
        return `<img class="picb" loading="lazy" src="${u}" onclick="lg('${u}','${nm}')" title="点击查看大图">`
             + (meta?`<div class="imeta">${esc(meta)}</div>`:'');
      }
      if(x.kind==='video'){
        if(u)return `<video controls preload="metadata" src="${u}" style="max-width:min(380px,72vw);border-radius:8px;display:block;margin:4px 0"></video>`;
        if(tu)return `<img class="picb" loading="lazy" src="${tu}" onclick="lg('${tu}','${nm}')" title="点击查看大图"><div class="imeta">🎬 视频原片未缓存，仅缩略图</div>`;
        return `<div class="dim">🎬 [视频] ${nm} (已过期)</div>`;
      }
      if(x.kind==='ptt')
        return `<div class="filecard">🎤 语音消息<span class="fs">SILK</span><a class="fs" href="${u}" download="${nm}">下载</a></div>`;
      // 引用消息
      if(x.kind==='quote')return `<div class="quote">↩ ${esc(x.name)}</div>`;
      // 表情: face=经典小黄脸真图, marketface=商城表情(本地图/CDN 代理), emoji=无图(名字兜底)
      if(x.kind==='marketface'){
        if(u)return `<img class="emo mf" loading="eager" src="${u}" onclick="lg('${u}','${nm}')">`;
        if(x.md5)return `<img class="emo mf" loading="lazy" data-nm="${esc(x.name||'表情')}" src="/api/mf?h=${x.md5}" onerror="mfFail(this)" onclick="lg(this.src,'${nm}')">`;
        const emf=FACE[x.name];
        if(emf)return `<span style="font-size:34px;line-height:1;vertical-align:middle">${emf}</span>`;
        return `<span class="chip" title="marketface id=${esc(String(x.id))}"><span class="ic">😀</span>${esc(x.name||'表情')}</span>`;
      }
      if(x.kind==='face'){
        if(u)return `<img class="emo face" loading="eager" src="${u}" onclick="lg('${u}','${nm}')">`;
        const em=FACE[x.name];
        if(em)return `<span style="font-size:34px;line-height:1;vertical-align:middle">${em}</span>`;
        return `<span class="chip" title="${esc(x.kind)} id=${esc(String(x.id))}">${esc(x.name||'表情')}</span>`;
      }
      if(x.kind==='emoji'){
        if(u)return `<img class="emo" loading="lazy" src="${u}" onclick="lg('${u}','${nm}')">`;
        const em=FACE[x.name];
        if(em)return `<span style="font-size:34px;line-height:1;vertical-align:middle">${em}</span>`;
        if(x.name)return `<span class="chip"><span class="ic">😊</span>${esc(x.name)}</span>`;
        return `<span class="sticker-empty">😊 表情 (本地无此表情资源)</span>`;
      }
      // 卡片(小程序/分享/音乐/名片/位置/公告/聊天记录) —— 标题+摘要+缩略图, 可点击
      if(x.kind==='card')return cardHtml(x);
      // 机器人 Markdown 正文
      if(x.kind==='md')return mdHtml(x.md);
      // 机器人按钮组
      if(x.kind==='btns')return btnHtml(x);
      // 通话等纯提示
      if(x.kind==='tip')return `<div class="tip">${esc(x.name)}</div>`;
      // 文件/其它
      if(u)return `<a class="filecard" href="${u}" download="${nm}">📎 <span class="fn">${nm}</span><span class="fs">${esc(fmtSize(x.size))}</span></a>`;
      return `<div class="dim">📎 [文件] ${nm} (未缓存到本地)</div>`;
    }).join('');
  }
  // QQ 经典小黄脸文本码 → Unicode emoji (仅替换已知的, 未知 [xxx] 原样保留)
  const FACE={'微笑':'😊','撇嘴':'😖','色':'😍','发呆':'😬','得意':'😎','流泪':'😢','害羞':'😳','闭嘴':'🤐','睡':'😴','大哭':'😭','尴尬':'😅','发怒':'😡','调皮':'😜','呲牙':'😁','惊讶':'😲','难过':'😞','囧':'😵','抓狂':'🤯','吐':'🤮','偷笑':'🤭','可爱':'🥰','白眼':'🙄','傲慢':'😤','饥饿':'😋','困':'😪','惊恐':'😱','流汗':'😅','憨笑':'🤣','大兵':'🪖','奋斗':'💪','咒骂':'🤬','疑问':'❓','嘘':'🤫','晕':'😵‍💫','衰':'😵','骷髅':'💀','敲打':'👊','再见':'👋','擦汗':'🥵','抠鼻':'👌','鼓掌':'👏','坏笑':'😏','鄙视':'👎','委屈':'🥺','快哭了':'😣','阴险':'😈','亲亲':'😘','吓':'😱','可怜':'🥹','菜刀':'🔪','西瓜':'🍉','啤酒':'🍺','篮球':'🏀','乒乓':'🏓','咖啡':'☕','饭':'🍚','猪头':'🐷','玫瑰':'🌹','凋谢':'🥀','示爱':'💘','爱心':'❤️','心碎':'💔','蛋糕':'🎂','闪电':'⚡','炸弹':'💣','刀':'🔪','足球':'⚽','瓢虫':'🐞','月亮':'🌙','太阳':'☀️','礼物':'🎁','拥抱':'🤗','强':'👍','弱':'🤏','握手':'🤝','胜利':'✌️','抱拳':'🫱','勾引':'👉','拳头':'✊','OK':'🆗','爱你':'❤️','飞吻':'💋','转圈':'🌀','发抖':'🥶','跳跳':'🕺','猪':'🐷','微笑弧':'🙂','哈欠':'🥱','捂脸':'🤦','奸笑':'😆','喵喵':'🐱','怄火':'🔥','晚安':'🌙','红包':'🧧','干杯':'🍻','doge':'🐕','菜狗':'🐶','吃瓜':'🍉','汪汪':'🐶','芒狗':'🥭','好兄弟':'🫂','超级赞':'👍','抛出':'🤾','奶茶':'🧋','骰子':'🎲','包剪锤':'✊','干饭':'🍚','膜拜':'🙏','头铁':'🤕','摸鱼':'🐟','搬砖':'🧱','躺平':'🛌','裂开':'🤡','泪目':'🥲','无语':'😑','佩服':'🙇','吐血':'🩸','晕倒':'💫','机械':'🤖','外星':'👽','幽灵':'👻','小纠结':'🤔','我最美':'💅','where':'🧭','What':'❓','汗':'💦','打脸':'🙆','石化':'🗿','乐园':'🎢','面包':'🍞','糖':'🍬','棒棒糖':'🍭','烟花':'🎆','闪电赞':'⚡','睡觉':'😴','起床':'⏰','加班':'💻','下班':'🏃','红包':'🧧','捡到钱':'💰','穷':'🪙','暴富':'💎','抽卡':'🎴','锦鲤':'🐟','许愿':'🌠'};
  function fmt(t){
    let e=esc(t);
    e=e.replace(/\\[([^\\[\\]<>]{1,8})\\]/g,(m,c)=>FACE.hasOwnProperty(c)?FACE[c]:m);
    return linkify(e);
  }
  const TAGONLY=/^\\[(图片|视频|语音)\\]( \\[(图片|视频|语音)\\])*$/;
  const arr=[...MSGS].reverse();
  let last=null;
  const html=arr.map((m,i)=>{
    const idx=MSGS.length-1-i;                    // 回到 MSGS 的下标(复制用)
    const same=!!last&&last.uin===m.uin&&m.ts-last.ts>=0&&m.ts-last.ts<300;
    last=m;
    const body=mediaHtml(m);
    const t=(m.text||'').trim();
    // 图片/视频/语音已经渲染出实体时, 不再重复显示 "[图片]" 这类占位文本
    const tx=(t&&!(body&&TAGONLY.test(t)))
      ? `<div class="tx">${fmt(m.text)}</div>` : '';
    const hd=same?'':`<div class="hd"><b>${esc(m.nick)}</b>${esc(m.uin)}<span class="tm">${m.time}</span></div>`;
    return `<div class="msg${same?' grp':''}">`
      +`<div class="acts"><button onclick="copyMsg(${idx})">复制</button></div>`
      +hd+body+tx+`</div>`;
  }).join('');
  // 聊天式布局: 旧的在上新的在下; 「加载更早」按钮在顶部, 到底提示在底部
  pn.innerHTML=(more?'<button id="loadmore" onclick="loadMsgs(false)">加载更早消息</button>':'')
    +html+'<div class="empty" style="padding:14px">— 到底了 —</div>';
  // 「回到最新」浮动按钮
  const jb=$('#jump');
  jb.onclick=()=>pn.scrollTo({top:pn.scrollHeight,behavior:'smooth'});
  pn.onscroll=()=>jb.classList.toggle('on',pn.scrollHeight-pn.scrollTop-pn.clientHeight>280);
  if(more){pn.scrollTop=prevT+(pn.scrollHeight-prevH)}  // 锚定视窗: 新内容在上方插入, 保持原消息不动
  else{pn.scrollTop=pn.scrollHeight}  // 首次加载滚到最新
  jb.classList.remove('on');
}

// tabs
document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>setTab(b.dataset.t));
function setTab(t){
  TAB=t;
  document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
  if(t==='msg'){$('#toolbar').style.display='flex';if(CUR)loadMsgs(true)}
  else{$('#toolbar').style.display='none';$('#pane').innerHTML=''}
  if(t==='search')renderSearch();
  if(t==='stats')renderStats();
}

function renderSearch(){
  $('#pane').innerHTML=`<div id="searchbox"><input id="kw" placeholder="搜索全部群消息…(回车)">
    <label class="dim"><input type="checkbox" id="onlyg" ${CUR?'':'disabled'}> 仅当前群</label>
    <button onclick="doSearch()">搜索</button></div><div id="sres" class="dim">全量扫描 1GB 文本约需 5~20 秒</div>`;
  $('#kw').onkeydown=e=>{if(e.key==='Enter')doSearch()};
}
function doSearch(){
  const kw=$('#kw').value.trim();if(!kw)return;
  const gid=$('#onlyg').checked?CUR:'';
  $('#sres').innerHTML='<div class="empty">搜索中…</div>';
  J(`/api/search?kw=${encodeURIComponent(kw)}&gid=${gid}&limit=50`)
  .then(rs=>{
    $('#sres').innerHTML=rs.length?rs.map(r=>`<div class="msg"><div class="hd"><b>${esc(r.gname||r.gid)}</b>${r.time}</div><div class="tx">${hilight(r.text,kw)}</div></div>`).join('')
      :'<div class="empty">没有找到</div>';
  }).catch(()=>{$('#sres').innerHTML='<div class="empty">搜索失败, 请重试</div>'});
}

function renderStats(){
  const gid=TAB==='stats'?(CUR||''):'';
  $('#pane').innerHTML='<div class="empty">统计构建中…(首次约 1 分钟, 已缓存)</div>';
  J('/api/stats?gid='+gid).then(d=>{
    const gname=CUR?(GROUPS.find(g=>g.id===CUR)||{}).name||CUR:'全部群';
    let h=`<h3 class="sec">${esc(gname)} · 近 30 天每日消息量</h3>`;
    h+=barchart(d.daily.map(x=>[x.d.slice(5),x.n]));
    h+=`<h3 class="sec">24 小时时段分布</h3>`;
    h+=barchart(d.hours.map((n,i)=>[i+'时',n]));
    if(d.top&&d.top.length){h+=`<h3 class="sec">发言 Top 20</h3><table class="top"><tr><th>#</th><th>昵称</th><th>QQ</th><th>条数</th></tr>`;
      d.top.forEach((s,i)=>h+=`<tr><td>${i+1}</td><td>${esc(s.nick)}</td><td>${esc(s.uin)}</td><td>${s.n}</td></tr>`);
      h+='</table>'}
    $('#pane').innerHTML=h;
  }).catch(()=>{$('#pane').innerHTML='<div class="empty">统计加载失败, 重试请点上方「统计」</div>'});
}
function barchart(data){
  const mx=Math.max(...data.map(d=>d[1]),1);
  let h='<div style="margin:10px 0">';
  for(const[lb,v]of data){
    h+=`<div class="statrow"><span class="lb">${esc(lb)}</span><span class="bar" style="width:${Math.max(v/mx*380,1)}px"></span><span class="v">${v.toLocaleString()}</span></div>`;
  }
  return h+'</div>';
}
</script></body></html>"""


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


def run(port=8765, account=None):
    global STORE
    if account:
        STORE = qqlib.QQStore(account)

    def warmup():
        try:
            s = store()
            s.groups_overview()
            s.stats_daily(days=30)
            s.stats_hours()
            print("[warmup] 缓存构建完成")
        except Exception as e:
            print(f"[warmup] 失败: {e}")

    threading.Thread(target=warmup, daemon=True).start()
    print(f"Web UI: http://127.0.0.1:{port}")
    try:
        from waitress import serve
        # 生产级服务器: 无 Werkzeug keep-alive 偶发断连问题
        serve(app, host="127.0.0.1", port=port, threads=8,
              channel_timeout=120, connection_limit=200)
    except ImportError:
        app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--account")
    a = ap.parse_args()
    run(port=a.port, account=a.account)
