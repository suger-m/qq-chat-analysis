# -*- coding: utf-8 -*-
"""qreport.py — 把 Agent 的结构化分析渲染成「单文件 HTML 报告」。

设计参考 astrbot_plugin_qq_group_daily_analysis / chatlog / wechat-daily-report:

  * **分析由 Agent (LLM) 读懂聊天内容后产出 JSON** —— 不做词频统计;
  * **渲染交给固定 HTML 模板** —— 每次输出风格一致, 单文件内联 CSS,
    无外部依赖, 可直接发群 / 截图 / 离线打开。

视觉取向: 柔和淡蓝底 + 白卡片 + 蓝色柔光阴影; Hero 用浅色渐变卡,
统计卡带彩色图标方块, 板块标题带彩色图标徽章, 关键人物使用 **QQ 官方头像**。

数据分析与渲染彻底解耦: Agent 只负责「内容判断」(议题/问答/决策/金句/人物),
本模块只负责「长什么样」。

用法:
  python qreport.py analysis.json -o report.html [--theme light|dark]
  python qreport.py analysis.json -o r.html --no-avatar     # 不联网取头像
  python qreport.py --example            # 打印输入 JSON 示例
  python qreport.py --schema             # 打印字段说明

输入 JSON 契约见 --schema; 所有字段除 group 外均可缺省, 缺失的板块整块跳过。
"""
import os
import re
import sys
import json
import html
import datetime
import argparse
import qprivacy
_PRIVATE_IDS = set()
_AVATAR_CLASSES = {}

sys.stdout.reconfigure(encoding="utf-8")

# ── 颜色 / 主题 ──────────────────────────────────────────────────────────────
# light 为默认: 淡蓝底 (#eef4fc) + 白卡片 + 蓝色柔光, astrbot BA 主题同源的观感。
THEMES = {
    "light": """
  --bg:#eef4fc; --panel:#ffffff; --panel2:#f5f9ff; --panel3:#e9f2fe;
  --bd:#dde8f8; --bd2:#c6d7ef;
  --fg:#22345c; --fg2:#54678a; --dim:#8194b3; --dim2:#a6b6d0;
  --acc:#3593f3; --acc2:#31c48d; --pink:#f06292; --orange:#ff9f43;
  --purple:#7c6ff7; --gold:#f7b731;
  --acc-soft:rgba(53,147,243,.10); --acc2-soft:rgba(49,196,141,.13);
  --acc-bd:rgba(53,147,243,.28);
  --warn:#cf8413; --warn-soft:rgba(207,132,19,.13);
  --bad:#e2574c; --bad-soft:rgba(226,87,76,.11);
  --quote-bg:#f6faff;
  --sh:0 2px 8px -3px rgba(37,99,190,.13);
  --sh2:0 6px 18px -8px rgba(37,99,190,.19);
  --sh3:0 16px 32px -14px rgba(37,99,190,.29);
  --hero-bg:linear-gradient(135deg,#ffffff 0%,#edf4ff 48%,#dbe9ff 100%);
  --hero-bd:#d5e5fb; --hero-sh:0 14px 34px -18px rgba(37,99,190,.42);
  --hero-fg:#1b2c50; --hero-fg2:#3d5478; --hero-dim:#7c8fae;
  --glow1:rgba(53,147,243,.22); --glow2:rgba(49,196,141,.16);
  --av-ring:#ffffff;
  --stat-bg:rgba(255,255,255,.80); --stat-bd:#e4eefb;
""",
    "dark": """
  --bg:#0b0f18; --panel:#131a28; --panel2:#18202f; --panel3:#1e2739;
  --bd:rgba(255,255,255,.08); --bd2:rgba(255,255,255,.16);
  --fg:#e9edf6; --fg2:#a9b6cd; --dim:#7a879e; --dim2:#5b6779;
  --acc:#5aa4f7; --acc2:#3ecf9e; --pink:#f06292; --orange:#ffb066;
  --purple:#9b90f9; --gold:#f7c95e;
  --acc-soft:rgba(90,164,247,.14); --acc2-soft:rgba(62,207,158,.13);
  --acc-bd:rgba(90,164,247,.36);
  --warn:#e0a95e; --warn-soft:rgba(224,169,94,.13);
  --bad:#ff7d7d; --bad-soft:rgba(255,125,125,.12);
  --quote-bg:rgba(255,255,255,.035);
  --sh:0 1px 2px rgba(0,0,0,.28);
  --sh2:0 2px 10px -4px rgba(0,0,0,.45);
  --sh3:0 16px 34px -16px rgba(0,0,0,.65);
  --hero-bg:linear-gradient(135deg,#0c1424 0%,#141f3a 52%,#1e2e52 100%);
  --hero-bd:rgba(255,255,255,.08); --hero-sh:0 18px 40px -20px rgba(0,0,0,.75);
  --hero-fg:#ffffff; --hero-fg2:rgba(255,255,255,.9); --hero-dim:rgba(255,255,255,.6);
  --glow1:rgba(96,150,255,.40); --glow2:rgba(58,214,168,.18);
  --av-ring:#141c2e;
  --stat-bg:rgba(255,255,255,.07); --stat-bd:rgba(255,255,255,.11);
""",
}

CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{/*__VARS__*/
  --r:18px; --rs:14px; --rxs:10px;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif;
  --mono:"JetBrains Mono","Cascadia Code",Consolas,"SF Mono",ui-monospace,monospace;
}
html{-webkit-text-size-adjust:100%}
body{background:var(--bg);color:var(--fg);font:15px/1.7 var(--sans);
  padding:40px 20px 76px;-webkit-font-smoothing:antialiased}
.wrap{max-width:880px;margin:0 auto}
::selection{background:var(--acc);color:#fff}

/* ── 头像 ────────────────────────────────────────────── */
.av{display:inline-block;flex-shrink:0;border-radius:50%;background-size:cover;
  background-position:center;background-color:var(--panel3);
  box-shadow:inset 0 0 0 1px rgba(16,24,40,.06)}
.av-ltr{display:flex;align-items:center;justify-content:center;color:#fff;
  font-weight:700;letter-spacing:.02em}

/* ── Hero ────────────────────────────────────────────── */
.hero{position:relative;overflow:hidden;isolation:isolate;
  background:var(--hero-bg);border:1px solid var(--hero-bd);border-radius:22px;
  box-shadow:var(--hero-sh);margin-bottom:28px;color:var(--hero-fg)}
.hero::before{content:"";position:absolute;left:0;right:0;top:0;height:3px;z-index:3;
  background:linear-gradient(90deg,var(--acc),var(--acc2) 55%,var(--pink))}
.hero-glow{position:absolute;border-radius:50%;z-index:0;pointer-events:none;
  filter:blur(6px)}
.hero-glow.g1{width:440px;height:440px;top:-250px;right:-140px;
  background:radial-gradient(circle,var(--glow1),transparent 68%)}
.hero-glow.g2{width:360px;height:360px;bottom:-220px;left:-110px;
  background:radial-gradient(circle,var(--glow2),transparent 70%)}
.hero-in{position:relative;z-index:2;padding:30px 32px 28px}
.hero-top{display:flex;align-items:flex-start;justify-content:space-between;
  gap:26px;flex-wrap:wrap}
.hero-l{min-width:0;flex:1}
.avstack{display:flex;flex-shrink:0;padding-top:4px}
.avstack .av{width:36px;height:36px;margin-left:-10px;
  box-shadow:0 0 0 2.5px var(--av-ring),0 3px 10px rgba(20,40,90,.18)}
.avstack .av:first-child{margin-left:0}
@media (max-width:560px){.avstack{display:none}}
.kicker{display:inline-flex;align-items:center;gap:7px;margin-bottom:13px;
  font-size:11px;font-weight:700;letter-spacing:.12em;color:var(--acc);
  background:var(--panel);border:1px solid var(--acc-bd);
  padding:5px 12px;border-radius:999px;box-shadow:0 1px 4px rgba(37,99,190,.12)}
.kicker .dot{width:5px;height:5px;border-radius:50%;background:var(--acc2);
  box-shadow:0 0 0 3px rgba(49,196,141,.24);
  animation:pulse 2.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
h1{font-size:31px;line-height:1.26;font-weight:800;letter-spacing:-.02em;
  color:var(--hero-fg);word-break:break-word}
.period{margin-top:10px;color:var(--hero-dim);font-size:13px;font-weight:500;
  display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.period .sep{width:3px;height:3px;border-radius:50%;background:currentColor;opacity:.65}
.headline{margin-top:18px;padding-left:15px;border-left:3px solid var(--acc);
  font-size:16px;line-height:1.8;color:var(--hero-fg2);font-weight:450}

/* 统计卡 (图标方块 + 数字 + 标签) */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(126px,1fr));
  gap:11px;margin-top:22px}
.stat{display:flex;align-items:center;gap:11px;background:var(--stat-bg);
  border:1px solid var(--stat-bd);border-radius:15px;padding:11px 13px;
  box-shadow:0 2px 10px -5px rgba(37,99,190,.28)}
.stat .si{width:34px;height:34px;border-radius:11px;flex-shrink:0;color:#fff;
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 3px 8px -3px rgba(20,40,90,.35)}
.stat .si svg{display:block}
.stat .sv{font-family:var(--mono);font-size:19px;font-weight:700;line-height:1.15;
  color:var(--hero-fg);letter-spacing:-.02em}
.stat .sl{font-size:11.5px;color:var(--hero-dim);margin-top:1px;white-space:nowrap}

/* ── 板块 ────────────────────────────────────────────── */
.sec{margin-bottom:30px}
.sec>h2{display:flex;align-items:center;gap:10px;margin-bottom:14px;
  font-size:15.5px;font-weight:700;letter-spacing:-.005em;color:var(--fg)}
.ico{width:26px;height:26px;border-radius:9px;flex-shrink:0;
  display:inline-flex;align-items:center;justify-content:center}
.ico svg{display:block}
.ico.c-blue{background:rgba(53,147,243,.13);color:#3593f3}
.ico.c-green{background:rgba(49,196,141,.15);color:#22a877}
.ico.c-pink{background:rgba(240,98,146,.14);color:#e04f83}
.ico.c-orange{background:rgba(255,159,67,.16);color:#e0821f}
.ico.c-purple{background:rgba(124,111,247,.14);color:#6a5cf0}
.ico.c-gold{background:rgba(247,183,49,.18);color:#c9900f}
.cnt{font-size:11.5px;color:var(--dim);font-weight:600;background:var(--panel);
  border:1px solid var(--bd);padding:1px 8px;border-radius:999px;
  font-family:var(--mono);margin-left:2px;flex-shrink:0}

/* 通用徽章配色 */
.bdg{font-size:10.5px;font-weight:650;padding:2px 9px;border-radius:999px;
  letter-spacing:.01em;white-space:nowrap}
.bdg.b-blue{color:#2b7fd4;background:var(--acc-soft)}
.bdg.b-green{color:#1f9e73;background:var(--acc2-soft)}
.bdg.b-pink{color:#d9487c;background:rgba(240,98,146,.14)}
.bdg.b-orange{color:#d97c17;background:rgba(255,159,67,.17)}
.bdg.b-purple{color:#6255e8;background:rgba(124,111,247,.15)}
.bdg.b-gold{color:#b8860b;background:rgba(247,183,49,.20)}

/* ── 摘要 ────────────────────────────────────────────── */
.summary{position:relative;overflow:hidden;background:var(--panel);
  border:1px solid var(--bd);border-radius:var(--r);padding:22px 26px;
  box-shadow:var(--sh2)}
.summary::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
  background:linear-gradient(180deg,var(--acc),var(--acc2))}
.summary p{color:var(--fg2);font-size:15px}
.summary p+p{margin-top:10px}

/* ── 活跃柱状图 ──────────────────────────────────────── */
.chart{display:flex;align-items:stretch;gap:5px;height:152px;padding:18px 20px 10px;
  background:var(--panel);border:1px solid var(--bd);border-radius:var(--r);
  box-shadow:var(--sh2)}
.act{flex:1;min-width:0;display:flex;flex-direction:column;gap:7px}
.ab{flex:1;display:flex;align-items:flex-end;justify-content:center}
.ab i{display:block;width:100%;max-width:26px;border-radius:5px;
  background:linear-gradient(180deg,var(--acc),#a6cdfc);
  box-shadow:0 2px 7px -3px rgba(53,147,243,.6)}
.act.peak .ab i{background:linear-gradient(180deg,var(--pink),#f9a7c2);
  box-shadow:0 3px 9px -3px rgba(240,98,146,.6)}
.ab i.zero{height:2px;background:var(--bd2);box-shadow:none;border-radius:2px}
.act span{font-size:9.5px;color:var(--dim2);font-family:var(--mono);
  text-align:center;height:12px;line-height:12px;white-space:nowrap}

/* ── 议题 ────────────────────────────────────────────── */
.topics{display:flex;flex-direction:column;gap:13px}
.topic{position:relative;background:var(--panel);border:1px solid var(--bd);
  border-radius:var(--r);padding:20px 24px 20px 26px;box-shadow:var(--sh2);
  overflow:hidden;transition:transform .18s ease,box-shadow .18s ease}
.topic:hover{transform:translateY(-2px);box-shadow:var(--sh3)}
.topic::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
  background:linear-gradient(180deg,var(--acc),#8fc4fb)}
.topic.h2::before{background:linear-gradient(180deg,var(--orange),#ffd6a5)}
.topic.h3::before{background:var(--bd2)}
.topic.h0::before{background:var(--bd)}
.t-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.tno{font-family:var(--mono);font-size:11.5px;font-weight:700;flex-shrink:0;
  color:var(--acc);background:var(--acc-soft);padding:2px 8px;border-radius:8px}
.t-head h3{font-size:16.5px;font-weight:700;line-height:1.42;flex:1;min-width:180px;
  letter-spacing:-.01em}
.heat{font-size:11px;font-weight:650;padding:2px 9px;border-radius:999px;
  white-space:nowrap}
.heat.h1{color:var(--bad);background:var(--bad-soft)}
.heat.h2{color:var(--warn);background:var(--warn-soft)}
.heat.h3{color:var(--acc);background:var(--acc-soft)}
.heat.h0{display:none}
.t-tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.tag{font-size:11.5px;color:var(--acc);background:var(--acc-soft);
  padding:2px 9px;border-radius:999px;font-weight:550}
.t-sum{margin-top:11px;color:var(--fg2);line-height:1.78;font-size:14.5px}
.t-points{list-style:none;margin-top:11px;display:flex;flex-direction:column;gap:6px}
.t-points li{position:relative;padding-left:17px;color:var(--fg2);font-size:14px;
  line-height:1.72}
.t-points li::before{content:"";position:absolute;left:3px;top:9px;width:4px;height:4px;
  border-radius:50%;background:var(--acc);opacity:.75}
/* 议题参与者头像堆叠 (astrbot 的做法: 让 LLM 直接给 QQ号, 头像才准) */
.t-contrib{display:flex;flex-shrink:0;align-items:center}
.t-contrib .av{width:25px;height:25px;margin-left:-8px;
  box-shadow:0 0 0 2px var(--panel)}
.t-contrib .av:first-child{margin-left:0}

/* ── 维度占比 (聊天构成锐评) ──────────────────────────── */
.dims{background:var(--panel);border:1px solid var(--bd);border-radius:var(--r);
  padding:22px 24px;box-shadow:var(--sh2)}
.dim+.dim{margin-top:16px}
.dim .d-h{display:flex;align-items:baseline;justify-content:space-between;
  gap:12px;margin-bottom:7px}
.dim .d-n{font-size:14.5px;font-weight:680;color:var(--fg);letter-spacing:-.005em}
.dim .d-p{font-family:var(--mono);font-size:13px;font-weight:650;color:var(--dim);
  flex-shrink:0}
.dim .d-bar{height:8px;border-radius:5px;background:var(--panel3);
  overflow:hidden;box-shadow:inset 0 1px 2px rgba(37,99,190,.08)}
.dim .d-bar i{display:block;height:100%;border-radius:5px;
  transition:width .3s ease}
.dim .d-c{font-size:13px;color:var(--dim);line-height:1.68;margin-top:8px}
.dim-sum{margin-top:18px;padding-top:15px;border-top:1px solid var(--bd);
  font-size:14.5px;color:var(--fg2);line-height:1.75}
.dim-head{margin-bottom:3px}
.dim-head .dt{font-size:17px;font-weight:700;letter-spacing:-.01em}
.dim-head .ds{font-size:13px;color:var(--dim);margin-top:4px}

/* 引用 */
.quotes{margin-top:13px;display:flex;flex-direction:column;gap:7px}
blockquote{background:var(--quote-bg);border-radius:var(--rs);
  padding:10px 14px;display:flex;gap:10px;align-items:flex-start;
  border:1px solid var(--bd)}
blockquote .av{width:26px;height:26px;margin-top:1px}
blockquote .qb{flex:1;min-width:0}
blockquote .qh{display:flex;align-items:baseline;gap:8px;margin-bottom:2px}
blockquote .q-who{font-size:12.5px;font-weight:650;color:var(--fg2)}
blockquote .q-tm{font-size:11px;color:var(--dim2);font-family:var(--mono)}
blockquote .q-tx{font-size:13.8px;color:var(--fg2);line-height:1.7;
  word-break:break-word;white-space:pre-wrap}

/* ── 问答 ────────────────────────────────────────────── */
.qas{display:flex;flex-direction:column;gap:12px}
.qa{background:var(--panel);border:1px solid var(--bd);border-radius:var(--r);
  padding:16px 20px;box-shadow:var(--sh2)}
.qa .row{display:flex;gap:11px;align-items:flex-start}
.qa .row+.row{margin-top:13px;padding-top:13px;border-top:1px solid var(--bd)}
.qa .av{width:30px;height:30px;margin-top:1px}
.qa .body{flex:1;min-width:0}
.qa .who{font-size:12px;color:var(--dim);margin-bottom:3px;display:flex;
  align-items:baseline;gap:8px;flex-wrap:wrap}
.qa .who b{color:var(--fg2);font-weight:650;font-size:12.5px}
.qa .who .wt{color:var(--dim2);font-family:var(--mono);font-size:11px}
.qa .tx{color:var(--fg2);line-height:1.75;font-size:14.5px;
  word-break:break-word;white-space:pre-wrap}

/* ── 待办 ────────────────────────────────────────────── */
.todos{display:flex;flex-direction:column;gap:8px}
.todo{display:flex;gap:12px;align-items:flex-start;background:var(--panel);
  border:1px solid var(--bd);border-radius:var(--rs);padding:13px 17px;
  box-shadow:var(--sh)}
.todo .box{flex-shrink:0;width:17px;height:17px;border-radius:6px;
  border:1.5px solid var(--orange);margin-top:4px;background:rgba(255,159,67,.14)}
.todo .tx{flex:1;color:var(--fg2);line-height:1.7;font-size:14.5px}
.todo .owner{font-size:11.5px;padding:2px 9px;border-radius:999px;
  white-space:nowrap;flex-shrink:0;font-weight:600;color:#d97c17;
  background:rgba(255,159,67,.17)}
.todo .note{display:block;font-size:12.5px;color:var(--dim);margin-top:2px}

/* ── 人物 ────────────────────────────────────────────── */
.people{display:grid;grid-template-columns:repeat(auto-fill,minmax(285px,1fr));gap:12px}
.person{background:var(--panel);border:1px solid var(--bd);
  border-radius:var(--r);padding:18px;box-shadow:var(--sh2);
  transition:transform .18s ease,box-shadow .18s ease}
.person:hover{transform:translateY(-2px);box-shadow:var(--sh3)}
.p-top{display:flex;align-items:center;gap:12px}
.person .av{width:46px;height:46px;
  box-shadow:0 0 0 2px var(--panel),0 0 0 3.5px rgba(53,147,243,.28)}
.p-name{font-size:15px;font-weight:700;display:flex;align-items:center;
  gap:8px;flex-wrap:wrap;line-height:1.35}
.p-stat{font-size:11.5px;color:var(--dim2);font-family:var(--mono);margin-top:4px;
  letter-spacing:-.01em}
.p-desc{font-size:13.5px;color:var(--fg2);line-height:1.7;margin-top:11px;
  padding-top:11px;border-top:1px solid var(--bd)}
.p-quote{font-size:12.5px;color:var(--dim);line-height:1.65;margin-top:9px;
  padding-left:11px;border-left:2px solid var(--bd2);word-break:break-word}

/* ── 金句 ────────────────────────────────────────────── */
.quotes-wall{display:grid;grid-template-columns:repeat(auto-fill,minmax(276px,1fr));
  gap:12px}
.quote{position:relative;background:var(--panel);border:1px solid var(--bd);
  border-radius:var(--r);padding:20px 22px 16px;box-shadow:var(--sh2);
  overflow:hidden;display:flex;flex-direction:column}
.quote::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
  background:linear-gradient(180deg,var(--gold),#ffe0a0)}
.quote:nth-child(3n+2)::before{background:linear-gradient(180deg,var(--pink),#ffc4d8)}
.quote:nth-child(3n+3)::before{background:linear-gradient(180deg,var(--purple),#d6d1ff)}
.quote .mark{position:absolute;top:4px;right:16px;font-size:54px;line-height:1;
  color:var(--acc);opacity:.08;font-family:Georgia,serif;font-weight:700}
.quote .tx{font-size:15px;line-height:1.78;color:var(--fg);font-weight:500;
  word-break:break-word;white-space:pre-wrap;position:relative;flex:1}
.quote .ft{display:flex;align-items:center;gap:9px;margin-top:14px;
  padding-top:12px;border-top:1px solid var(--bd)}
.quote .ft .av{width:28px;height:28px}
.quote .ft .who{font-size:12.5px;font-weight:650;color:var(--fg2);
  white-space:nowrap}
.quote .ft .tm{font-size:11px;color:var(--dim2);font-family:var(--mono);
  white-space:nowrap}
.quote .note{font-size:11.5px;color:var(--dim);line-height:1.6;margin-top:7px}

/* ── 洞察 / 风险 ─────────────────────────────────────── */
.insights{display:flex;flex-direction:column;gap:8px}
.insight{display:flex;gap:12px;align-items:flex-start;background:var(--panel);
  border:1px solid var(--bd);border-radius:var(--rs);padding:13px 17px;
  box-shadow:var(--sh);position:relative;overflow:hidden}
.insight::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
  background:linear-gradient(180deg,var(--purple),#c6c0ff)}
.insight .n{flex-shrink:0;width:21px;height:21px;border-radius:50%;
  background:linear-gradient(135deg,var(--purple),#9d94ff);color:#fff;
  font-size:11px;font-weight:700;display:flex;align-items:center;
  justify-content:center;font-family:var(--mono);margin-top:2px}
.insight .tx{flex:1;color:var(--fg2);line-height:1.72;font-size:14.5px}
.risks{display:flex;flex-direction:column;gap:8px}
.risk{display:flex;gap:11px;align-items:flex-start;border-radius:var(--rs);
  padding:13px 17px;border:1px solid var(--bd)}
.risk.lv-warn{background:var(--warn-soft);border-color:transparent}
.risk.lv-bad{background:var(--bad-soft);border-color:transparent}
.risk.lv-info{background:var(--panel);box-shadow:var(--sh)}
.risk .ic{flex-shrink:0;font-size:13px;line-height:1.55;font-weight:800}
.risk .tx{flex:1;color:var(--fg2);line-height:1.72;font-size:14.5px}
.risk.lv-warn .ic{color:var(--warn)}
.risk.lv-bad .ic{color:var(--bad)}
.risk.lv-info .ic{color:var(--acc)}

/* ── 时间线 ──────────────────────────────────────────── */
.tl{position:relative;padding-left:24px}
.tl::before{content:"";position:absolute;left:5px;top:7px;bottom:7px;width:1px;
  background:var(--bd2)}
.tl-item{position:relative;padding-bottom:16px}
.tl-item:last-child{padding-bottom:0}
.tl-item::before{content:"";position:absolute;left:-23px;top:6px;width:9px;height:9px;
  border-radius:50%;background:var(--panel);border:2px solid var(--acc);
  box-shadow:0 0 0 3px var(--acc-soft)}
.tl-t{font-family:var(--mono);font-size:11.5px;color:var(--acc);font-weight:600}
.tl-h{font-size:14.5px;font-weight:650;margin-top:2px;line-height:1.5}
.tl-d{font-size:13.5px;color:var(--dim);line-height:1.68;margin-top:3px}

/* ── 附录 ────────────────────────────────────────────── */
details.appendix{background:var(--panel);border:1px solid var(--bd);
  border-radius:var(--r);overflow:hidden;box-shadow:var(--sh)}
details.appendix>summary{padding:14px 20px;cursor:pointer;font-size:13.5px;
  font-weight:650;color:var(--fg2);list-style:none;display:flex;
  align-items:center;gap:10px;user-select:none}
details.appendix>summary::-webkit-details-marker{display:none}
details.appendix>summary::before{content:"";order:9;margin-left:auto;
  width:7px;height:7px;border-right:1.8px solid var(--dim2);
  border-bottom:1.8px solid var(--dim2);transform:rotate(45deg);
  transition:transform .18s;display:inline-block}
details.appendix[open]>summary::before{transform:rotate(-135deg)}
details.appendix[open]>summary{border-bottom:1px solid var(--bd)}
.raw{padding:14px 20px;font-size:12.5px;line-height:1.85;max-height:540px;
  overflow:auto;font-family:var(--mono)}
.raw .ln{display:flex;gap:10px;padding:1.5px 0}
.raw .tm{color:var(--dim2);flex-shrink:0}
.raw .wh{color:#22a877;flex-shrink:0;max-width:108px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.raw .tx{color:var(--fg2);word-break:break-word;white-space:pre-wrap;min-width:0}

/* ── 页脚 ────────────────────────────────────────────── */
.foot{margin-top:36px;padding-top:18px;border-top:1px solid var(--bd);
  display:flex;align-items:center;justify-content:center;gap:9px;
  color:var(--dim2);font-size:11.5px;letter-spacing:.01em;flex-wrap:wrap}
.foot .fdot{width:3px;height:3px;border-radius:50%;background:currentColor;
  opacity:.55}
.empty{padding:32px;text-align:center;color:var(--dim);font-size:13.5px;
  border:1px dashed var(--bd2);border-radius:var(--r)}

@media (max-width:640px){
  body{padding:18px 12px 46px;font-size:14.5px}
  .hero{border-radius:18px}
  .hero-in{padding:24px 20px 22px}
  h1{font-size:23px}
  .headline{font-size:15px;margin-top:16px}
  .stats{grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:9px}
  .topic,.qa{padding-left:17px;padding-right:17px}
  .people,.quotes-wall{grid-template-columns:1fr}
}
@media print{
  body{background:#fff;padding:0}
  .hero,.card,.topic,.qa,.todo,.person,.quote,.insight,.risk,.summary,.chart{
    box-shadow:none;break-inside:avoid}
  details.appendix{border:none}
}
"""


# ── 小工具 ───────────────────────────────────────────────────────────────────

def esc(s):
    return html.escape(qprivacy.redact_text(s, _PRIVATE_IDS), quote=True)


def num(v):
    """数字千分位 (字符串原样返回)。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v:,}"
    return str(v if v is not None else "")


def av_colors(name):
    """昵称 → 稳定的双色渐变 (字母头像降级用)。"""
    h = 0
    for ch in str(name):
        h = (h * 131 + ord(ch)) & 0xFFFFFF
    h1 = h % 360
    h2 = (h1 + 40) % 360
    return (f"hsl({h1},58%,{48 + (h >> 8) % 10}%)",
            f"hsl({h2},56%,{36 + (h >> 16) % 9}%)")


def _heat_cls(h):
    """热度 → CSS 类 + 文案。数字 1-5 (5 最热) 或 high/mid/low。"""
    if isinstance(h, str):
        s = h.strip().lower()
        m = {"high": 1, "hot": 1, "热议": 1, "高": 1, "爆": 1,
             "mid": 2, "medium": 2, "中": 2, "warm": 2,
             "low": 3, "low2": 3, "低": 3, "冷": 3}
        v = m.get(s)
        if v is not None:
            return f"h{v}", {1: "热议", 2: "活跃", 3: "提及"}.get(v, "")
        try:
            v = int(re.sub(r"\D", "", s) or 0)
        except ValueError:
            v = 0
    else:
        try:
            v = int(h)
        except (TypeError, ValueError):
            v = 0
    if v >= 5:
        return "h1", "热议"
    if v >= 3:
        return "h2", "活跃"
    if v in (1, 2):
        return "h3", "提及"
    return "h0", ""


def _who_time(q):
    who = q.get("who") or q.get("name") or q.get("uin") or ""
    tm = q.get("time") or q.get("ts_str") or ""
    return who, tm


def _li_list(items, cls):
    items = [esc(x) for x in items if str(x).strip()]
    if not items:
        return ""
    return (f'<ul class="{cls}">'
            + "".join(f"<li>{x}</li>" for x in items) + "</ul>")


# 板块图标 (Material 24x24 path) —— 让标题有识别度。
_ICONS = {
    "doc": "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z",
    "chat": "M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H6l-2 2V4h16v12z",
    "qa": "M11 18h2v-2h-2v2zm1-16C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm0-14c-2.21 0-4 1.79-4 4h2c0-1.1.9-2 2-2s2 .9 2 2c0 2-3 1.75-3 5h2c0-2.25 3-2.5 3-5 0-2.21-1.79-4-4-4z",
    "check": "M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-9 14l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z",
    "people": "M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z",
    "quote": "M6 17h3l2-4V7H5v6h3zm8 0h3l2-4V7h-6v6h3z",
    "bulb": "M9 21c0 .55.45 1 1 1h4c.55 0 1-.45 1-1v-1H9v1zm3-19C8.14 2 5 5.14 5 9c0 2.38 1.19 4.47 3 5.74V17c0 .55.45 1 1 1h6c.55 0 1-.45 1-1v-2.26c1.81-1.27 3-3.36 3-5.74 0-3.86-3.14-7-7-7z",
    "warn": "M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z",
    "clock": "M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67z",
    "tune": "M3 17v2h6v-2H3zM3 5v2h10V5H3zm10 16v-2h8v-2h-8v-2h-2v6h2zM7 9v2H3v2h4v2h2V9H7zm14 4v-2H11v2h10zm-6-4h2V7h4V5h-4V3h-2v6z",
    "notes": "M3 18h12v-2H3v2zM3 6v2h18V6H3zm0 7h18v-2H3v2z",
    "fire": "M13.5.67s.74 2.65.74 4.8c0 2.06-1.35 3.73-3.41 3.73-2.07 0-3.63-1.67-3.63-3.73l.03-.36C5.21 7.51 4 10.62 4 14c0 4.42 3.58 8 8 8s8-3.58 8-8C20 8.61 17.41 3.8 13.5.67zM11.71 19c-1.78 0-3.22-1.4-3.22-3.14 0-1.62 1.05-2.76 2.81-3.12 1.77-.36 3.6-1.21 4.62-2.58.39 1.29.59 2.65.59 4.04 0 2.65-2.15 4.8-4.8 4.8z",
    "group": "M12 12.75c1.63 0 3.07.39 4.24.9 1.08.48 1.76 1.56 1.76 2.73V18H6v-1.61c0-1.18.68-2.26 1.76-2.73 1.17-.52 2.61-.91 4.24-.91zM4 13c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm1.13 1.1c-.37-.06-.74-.1-1.13-.1-.99 0-1.93.21-2.78.58A2.01 2.01 0 0 0 0 16.43V18h4.5v-1.61c0-.83.23-1.61.63-2.29zM20 13c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm4 3.43c0-.81-.48-1.53-1.22-1.85A6.95 6.95 0 0 0 20 14c-.39 0-.76.04-1.13.1.4.68.63 1.46.63 2.29V18H24v-1.57zM12 6c1.66 0 3 1.34 3 3s-1.34 3-3 3-3-1.34-3-3 1.34-3 3-3z",
}

# 统计卡图标方块 / 人物角色徽章 / 维度条 的配色循环
_HUES = ("blue", "green", "pink", "orange", "purple", "gold")
_SOLID = {"blue": "linear-gradient(135deg,#4aa3f7,#2a7fe0)",
          "green": "linear-gradient(135deg,#40d29b,#22a877)",
          "pink": "linear-gradient(135deg,#f4799f,#e04f83)",
          "orange": "linear-gradient(135deg,#ffb066,#ef8a1e)",
          "purple": "linear-gradient(135deg,#9b90f9,#6a5cf0)",
          "gold": "linear-gradient(135deg,#fbc94f,#e0a60c)"}
_DIM_BAR = ("linear-gradient(90deg,#4aa3f7,#8fc4fb)",
            "linear-gradient(90deg,#40d29b,#8ee8c4)",
            "linear-gradient(90deg,#f4799f,#fbb0c8)",
            "linear-gradient(90deg,#ffb066,#ffd6a5)",
            "linear-gradient(90deg,#9b90f9,#cdc7ff)",
            "linear-gradient(90deg,#fbc94f,#ffe3a3)")


def _ico(name, color="blue"):
    p = _ICONS.get(name)
    if not p:
        return ""
    return (f'<span class="ico c-{color}">'
            '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor" '
            f'aria-hidden="true"><path d="{p}"/></svg></span>')


def _section(title, body, count=None, icon=None, color="blue"):
    if not body:
        return ""
    c = f'<span class="cnt">{esc(count)}</span>' if count else ""
    head = _ico(icon, color) if icon else ""
    return (f'<section class="sec"><h2>{head}{esc(title)}{c}</h2>'
            f'{body}</section>')


# ── 头像 ─────────────────────────────────────────────────────────────────────
# 渲染期收集「谁出现在报告里」→ 反查 QQ 号 → 拉官方头像 → 每个 QQ 号只内联
# 一份 data URI (挂在 CSS 类上), 页面里用 <i class="av av-<uin>"> 引用。

AVATARS = {}        # {uin: data_uri}  本次渲染实际用到的
_RESOLVER = None    # 昵称 → QQ号 反查表 (按群构建, 见 _resolver)


def reset_resolver(gid=None):
    """按「这份报告对应的群」重建昵称反查表, 并清掉上一份的缓存。

    必须每次渲染都重置: 反表是从解密库建的, 进程内长期复用会让**跨群渲染**
    (web.py 连续出多个群的报告) 拿着第一个群的表去解析后面的群 —— 群名片是
    分群的, 同一个人在别的群叫别的名字, 结果就是批量配错头像。

    `gid` 交给 `qqlib.nick_index` 做**群内**解析, 且重名不猜 (返回空 → 降级
    首字头像)。不传 gid 时退回全库唯一名。
    """
    global _RESOLVER, AVATARS
    _RESOLVER = {}
    AVATARS = {}
    try:
        import qqlib
        _RESOLVER = qqlib.QQStore().nick_index(gid)
    except Exception:
        pass
    return _RESOLVER


def _resolver():
    """昵称 → QQ号。未初始化时按「全库唯一名」兜底。"""
    if _RESOLVER is None:
        reset_resolver(None)
    return _RESOLVER


# 显式 QQ号字段 (Agent 从「成员对照表」抄过来填的) / 人名字段
UIN_KEYS = ("uin", "qq", "user_id", "uin_str", "from_uin",
            "asker_uin", "answerer_uin", "who_uin", "owner_uin", "sender_uin",
            "who_q_uin", "who_a_uin")
NAME_KEYS = ("who", "name", "asker", "answerer", "owner", "sender",
             "nick", "user", "who_q", "who_a")


def _uin_of(entry, name):
    """给一条记录找 QQ 号: 显式字段优先, 否则按昵称反查群成员表。"""
    if isinstance(entry, dict):
        for k in ("uin", "qq", "uin_str", "from_uin", "avatar"):
            v = entry.get(k)
            if v and str(v).strip().isdigit():
                return str(v).strip()
    nm = str(name or "").strip()
    if nm.isdigit():
        return nm
    return (_resolver().get(nm) or "") if nm else ""


def _uin_for(entry, name, *keys):
    """按指定字段名取 QQ号 (qa 里 asker/answerer 各有自己的 uin)。"""
    if isinstance(entry, dict):
        for k in keys:
            v = entry.get(k)
            if v and str(v).strip().isdigit():
                return str(v).strip()
    return _uin_of(entry, name)


def _avatar_class(uin):
    if uin not in _AVATAR_CLASSES:
        _AVATAR_CLASSES[uin] = "p%d" % (len(_AVATAR_CLASSES) + 1)
    return _AVATAR_CLASSES[uin]


def _avatar(name, uin=None, cls="", size=100):
    """头像元素。有真实头像→CSS 类引用; 否则→首字渐变圆。"""
    u = str(uin or "").strip() if uin else ""
    if u and u.isdigit() and AVATARS.get(u):
        return f'<i class="av {cls} av-{_avatar_class(u)}"></i>'
    nm = str(name or "?").strip() or "?"
    if nm.isdigit():
        nm = "匿名成员"
    c1, c2 = av_colors(nm)
    first = next((ch for ch in nm if not ch.isspace()), "?")
    fs = max(10, int(size * 0.42))
    return (f'<i class="av {cls} av-ltr" '
            f'style="background-image:linear-gradient(135deg,{c1},{c2});'
            f'font-size:{fs}px">{esc(first)}</i>')


def _avatar_style():
    """把用到的头像编成 CSS 类 (每个 QQ 号只内联一次 data URI)。"""
    if not AVATARS:
        return ""
    rules = "".join(f'.av-{_avatar_class(u)}{{background-image:url("{uri}")}}'
                    for u, uri in AVATARS.items())
    return f"<style>{rules}</style>"


def _walk_ids(o, uins, names):
    """递归扫整个 JSON, 收集所有 QQ号与出现过的昵称。"""
    if isinstance(o, dict):
        for k, v in o.items():
            if (k in UIN_KEYS and isinstance(v, (str, int))
                    and str(v).strip().isdigit()):
                uins.append(str(v).strip())
            elif (k in NAME_KEYS and isinstance(v, str) and v.strip()):
                names.append(v.strip())
            else:
                _walk_ids(v, uins, names)
    elif isinstance(o, list):
        for it in o:
            if (isinstance(it, str) and it.strip().isdigit()
                    and len(it.strip()) >= 5):     # contributors: ["123", ...]
                uins.append(it.strip())
            else:
                _walk_ids(it, uins, names)


def collect_avatars(d, size=100):
    """扫描报告里出现的所有人 → 拉头像。失败静默跳过 (降级字母头像)。

    两类来源: ① JSON 里显式写的 QQ号 (最准); ② 昵称反查群成员表 (兜底)。
    """
    uins, names = [], []
    _walk_ids(d, uins, names)
    for nm in names:
        if isinstance(nm, str) and nm.isdigit():
            uins.append(nm)
        else:
            u = _resolver().get(nm)
            if u:
                uins.append(u)
    seen, ordered = set(), []
    for u in uins:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    try:
        import qqlib
        for u in ordered:
            uri = qqlib.avatar_data_uri(u, size)
            if uri:
                AVATARS[u] = uri
            if len(AVATARS) >= 40:      # 上限, 防报告体积失控
                break
    except Exception:
        pass
    return ordered


# ── 各板块渲染 ───────────────────────────────────────────────────────────────

def _hero(d, stack=None):
    stats = d.get("stats") or []
    if isinstance(stats, dict):
        stats = [{"label": k, "value": v} for k, v in stats.items()]
    cards = []
    for i, s in enumerate(stats):
        if not isinstance(s, dict):
            continue
        nm = s.get("icon") or ("chat", "people", "clock", "fire",
                               "group", "doc")[i % 6]
        path = _ICONS.get(nm) or _ICONS["doc"]
        hue = _HUES[i % len(_HUES)]
        cards.append(
            f'<div class="stat"><span class="si" style="background:{_SOLID[hue]}">'
            '<svg viewBox="0 0 24 24" width="17" height="17" fill="currentColor" '
            f'aria-hidden="true"><path d="{path}"/></svg></span>'
            f'<div><div class="sv">{esc(num(s.get("value")))}</div>'
            f'<div class="sl">{esc(s.get("label", ""))}</div></div></div>')
    stats_html = f'<div class="stats">{"".join(cards)}</div>' if cards else ""

    avs = ""
    if stack:
        items = "".join(_avatar("", u, size=100) for u in stack[:7])
        if items:
            avs = f'<div class="avstack">{items}</div>'

    meta = [esc(d.get("period") or "")]
    # Group identifiers remain private in the source JSON.
    meta = [m for m in meta if m]
    sep = '<span class="sep"></span>'
    hl = (f'<p class="headline">{esc(d["headline"])}</p>'
          if d.get("headline") else "")
    return f"""<header class="hero">
  <div class="hero-glow g1"></div><div class="hero-glow g2"></div>
  <div class="hero-in">
    <div class="hero-top">
      <div class="hero-l">
        <span class="kicker"><span class="dot"></span>{esc(d.get('kicker') or 'QQ 群聊日报')}</span>
        <h1>{esc(d.get('group') or '未命名群聊')}</h1>
        <div class="period">{sep.join(meta)}</div>
      </div>
      {avs}
    </div>
    {hl}
    {stats_html}
  </div>
</header>"""


def _summary(d):
    s = d.get("summary")
    if not s:
        return ""
    if isinstance(s, list):
        ps = "".join(f'<p>{esc(x)}</p>' for x in s)
    else:
        ps = "".join(f'<p>{esc(p)}</p>' for p in str(s).split("\n") if p.strip())
    return f'<section class="sec"><div class="summary">{ps}</div></section>'


def _activity(d):
    """可选: d["activity"] = [24 个数字] 或 [{"hour","count"}] → 渐变柱状图。"""
    raw = d.get("activity")
    if not raw or not isinstance(raw, list):
        return ""
    vals = []
    if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in raw):
        hours = len(raw) == 24
        vals = [(f"{i:02d}" if hours else str(i), float(x))
                for i, x in enumerate(raw)]
    else:
        for i, x in enumerate(raw):
            if isinstance(x, dict):
                lab = x.get("hour", x.get("label", x.get("h", i)))
                cnt = x.get("count", x.get("value", x.get("c", 0)))
            else:
                lab, cnt = i, x
            try:
                vals.append((str(lab), float(cnt or 0)))
            except (TypeError, ValueError):
                vals.append((str(lab), 0.0))
    if not vals or max(v for _, v in vals) <= 0:
        return ""
    # 有效时段很集中时裁掉两头的大片空白, 柱子才看得出形状
    idx = [i for i, (_, v) in enumerate(vals) if v > 0]
    if idx and (idx[-1] - idx[0] + 1) <= 16:
        vals = vals[max(0, idx[0] - 1):min(len(vals), idx[-1] + 1)]
    mx = max(v for _, v in vals)
    step = max(1, len(vals) // 8)
    bars = []
    for i, (lab, v) in enumerate(vals):
        peak = " peak" if v >= mx else ""
        if v > 0:
            bar = f'<i style="height:{max(6.0, v / mx * 100.0):.1f}%"></i>'
        else:
            bar = '<i class="zero"></i>'
        show = "" if (i % step and v < mx) else esc(lab)
        bars.append(f'<div class="act{peak}"><div class="ab">{bar}</div>'
                    f'<span>{show}</span></div>')
    body = f'<div class="chart">{"".join(bars)}</div>'
    return _section(d.get("title_activity") or "活跃时段",
                    body, icon="clock", color="green")


def _quality(d):
    """聊天构成: 3-6 个「高层抽象维度 + 占比 + 锐评」。"""
    q = d.get("quality")
    if not isinstance(q, dict):
        return ""
    dims = [x for x in (q.get("dimensions") or []) if isinstance(x, dict)]
    if not dims:
        return ""
    # 先解析成数字再统一处理, 不要边渲染边算 —— 需要先知道总和才能等比缩放。
    # Agent 经常给出「合计 120%」这种数 (三个维度凭感觉各写 40%), 直接画出来的
    # 条形图会互相矛盾 (加起来超出整条轨道)。这里按 astrbot 的口径归一:
    # 各自 clamp 到 [0,100], 总和超 100 时整体乘 100/total。
    vals = []
    for x in dims:
        raw = str(x.get("percentage", x.get("pct", 0)) or 0)
        try:
            pct = float(re.sub(r"[^\d.]", "", raw) or 0)
        except ValueError:
            pct = 0.0
        vals.append(max(0.0, min(100.0, pct)))
    total = sum(vals)
    factor = 100.0 / total if total > 100 else 1.0
    rows = []
    for i, (x, v) in enumerate(zip(dims, vals)):
        pct = round(v * factor, 1)
        col = _DIM_BAR[i % len(_DIM_BAR)]
        cm = x.get("comment") or x.get("desc") or ""
        rows.append(
            '<div class="dim"><div class="d-h">'
            f'<span class="d-n">{esc(x.get("name", ""))}</span>'
            f'<span class="d-p">{pct:g}%</span></div>'
            f'<div class="d-bar"><i style="width:{pct:.1f}%;background:{col}"></i></div>'
            + (f'<div class="d-c">{esc(cm)}</div>' if cm else "")
            + "</div>")
    t, st = q.get("title") or "", q.get("subtitle") or ""
    head = ""
    if t or st:
        head = (f'<div class="dim-head"><div class="dt">{esc(t)}</div>'
                + (f'<div class="ds">{esc(st)}</div>' if st else "") + "</div>")
    sm = q.get("summary") or ""
    sumh = f'<div class="dim-sum">{esc(sm)}</div>' if sm else ""
    return _section(d.get("title_quality") or "聊天构成",
                    f'<div class="dims">{head}{"".join(rows)}{sumh}</div>',
                    icon="tune", color="purple")


def _blockquote(q):
    who, tm = _who_time(q)
    tx = q.get("text") or q.get("tx") or ""
    av = _avatar(who, _uin_of(q, who), size=100)
    head = []
    if who:
        head.append(f'<span class="q-who">{esc(who)}</span>')
    if tm:
        head.append(f'<span class="q-tm">{esc(tm)}</span>')
    h = f'<div class="qh">{"".join(head)}</div>' if head else ""
    return (f'<blockquote>{av}<div class="qb">{h}'
            f'<div class="q-tx">{esc(tx)}</div></div></blockquote>')


def _contrib_html(items):
    """议题参与者头像堆叠。条目可以是 QQ号字符串, 也可以是 {name,uin}。"""
    seen, avs = set(), []
    for it in (items or [])[:6]:
        if isinstance(it, dict):
            nm = it.get("name") or it.get("nick") or ""
            u = _uin_of(it, nm)
        else:
            nm, u = str(it), _uin_of({}, str(it))
        key = u or nm
        if not key or key in seen:
            continue
        seen.add(key)
        avs.append(_avatar(nm, u))
    return f'<div class="t-contrib">{"".join(avs)}</div>' if avs else ""


def _topics(d):
    items = d.get("topics") or []
    if not items:
        return ""
    cards = []
    for i, t in enumerate(items, 1):
        hcls, htxt = _heat_cls(t.get("heat"))
        heat = f'<span class="heat {hcls}">{esc(htxt)}</span>' if htxt else ""
        tags = "".join(f'<span class="tag">{esc(x)}</span>'
                       for x in (t.get("tags") or []))
        pts = _li_list(t.get("points") or [], "t-points")
        qs = "".join(_blockquote(q) for q in (t.get("quotes") or [])
                     if isinstance(q, dict))
        qhtml = f'<div class="quotes">{qs}</div>' if qs else ""
        taghtml = f'<div class="t-tags">{tags}</div>' if tags else ""
        contrib = _contrib_html(t.get("contributors") or t.get("people"))
        cards.append(f"""<article class="topic {hcls}">
  <div class="t-head"><span class="tno">{i:02d}</span>
    <h3>{esc(t.get('title', ''))}</h3>{heat}{contrib}</div>
  {taghtml}
  <p class="t-sum">{esc(t.get('summary', ''))}</p>
  {pts}{qhtml}
</article>""")
    return _section(d.get("title_topics") or "今日议题",
                    f'<div class="topics">{"".join(cards)}</div>',
                    f"{len(items)} 个", icon="chat", color="blue")


def _qa(d):
    items = d.get("qa") or []
    if not items:
        return ""
    rows = []
    for x in items:
        if not isinstance(x, dict):
            continue
        parts = []
        q = x.get("q") or ""
        if q:
            asker = x.get("asker") or x.get("who_q") or ""
            tm = x.get("time") or ""
            who = (f'<b>{esc(asker)}</b>' if asker else "")
            wt = f'<span class="wt">{esc(tm)}</span>' if tm else ""
            parts.append(
                f'<div class="row">'
                f'{_avatar(asker, _uin_for(x, asker, "asker_uin", "who_q_uin"))}'
                f'<div class="body"><div class="who">{who}{wt}</div>'
                f'<div class="tx">{esc(q)}</div></div></div>')
        a = x.get("answer") or x.get("a") or ""
        if a:
            ans_er = x.get("answerer") or x.get("who_a") or ""
            tm = x.get("time_a") or ""
            who = (f'<b>{esc(ans_er)}</b>' if ans_er else "")
            wt = f'<span class="wt">{esc(tm)}</span>' if tm else ""
            parts.append(
                f'<div class="row">'
                f'{_avatar(ans_er, _uin_for(x, ans_er, "answerer_uin", "who_a_uin"))}'
                f'<div class="body"><div class="who">{who}{wt}</div>'
                f'<div class="tx">{esc(a)}</div></div></div>')
        if parts:
            rows.append(f'<div class="qa">{"".join(parts)}</div>')
    return _section(d.get("title_qa") or "关键问答",
                    f'<div class="qas">{"".join(rows)}</div>', f"{len(rows)} 组",
                    icon="qa", color="green")


def _todos(d):
    items = d.get("todos") or []
    if not items:
        return ""
    rows = []
    for t in items:
        if isinstance(t, str):
            t = {"text": t}
        owner = (f'<span class="owner">{esc(t["owner"])}</span>'
                 if t.get("owner") else "")
        note = (f'<span class="note">{esc(t["note"])}</span>'
                if t.get("note") else "")
        rows.append(f'<div class="todo"><div class="box"></div>'
                    f'<div class="tx">{esc(t.get("text", ""))}{note}</div>{owner}</div>')
    return _section(d.get("title_todos") or "待办 / 决策",
                    f'<div class="todos">{"".join(rows)}</div>', f"{len(rows)} 条",
                    icon="check", color="orange")


def _people(d):
    items = d.get("people") or []
    if not items:
        return ""
    cards = []
    for i, p in enumerate(items):
        if not isinstance(p, dict):
            p = {"name": str(p)}
        name = p.get("name") or p.get("uin") or "?"
        uin = _uin_of(p, name)
        hue = _HUES[i % len(_HUES)]
        role = (f'<span class="bdg b-{hue}">{esc(p["role"])}</span>'
                if p.get("role") else "")
        stat = (f'<div class="p-stat">{esc(p["stat"])}</div>'
                if p.get("stat") else "")
        desc = (f'<div class="p-desc">{esc(p["desc"])}</div>'
                if p.get("desc") else "")
        q = p.get("quote") or (p.get("quotes") or [None])[0]
        qh = (f'<div class="p-quote">{esc(q)}</div>' if q else "")
        cards.append(f"""<div class="person">
  <div class="p-top">{_avatar(name, uin, size=100)}
    <div><div class="p-name">{esc(name)}{role}</div>{stat}</div></div>
  {desc}{qh}
</div>""")
    return _section(d.get("title_people") or "关键人物",
                    f'<div class="people">{"".join(cards)}</div>', f"{len(items)} 人",
                    icon="people", color="pink")


def _quotes(d):
    items = d.get("quotes") or []
    if not items:
        return ""
    cards = []
    for q in items:
        if not isinstance(q, dict):
            q = {"text": str(q)}
        who, tm = _who_time(q)
        note = (f'<div class="note">{esc(q["note"])}</div>'
                if q.get("note") else "")
        cards.append(f"""<div class="quote"><div class="mark">&rdquo;</div>
  <div class="tx">{esc(q.get('text', ''))}</div>
  <div class="ft">{_avatar(who, _uin_of(q, who))}
  <span class="who">{esc(who)}</span>
  <span class="tm">{esc(tm)}</span></div>{note}
</div>""")
    return _section(d.get("title_quotes") or "金句 / 名场面",
                    f'<div class="quotes-wall">{"".join(cards)}</div>',
                    f"{len(items)} 条", icon="quote", color="gold")


def _insights(d):
    items = d.get("insights") or []
    if not items:
        return ""
    rows = "".join(f'<div class="insight"><div class="n">{i}</div>'
                   f'<div class="tx">{esc(x)}</div></div>'
                   for i, x in enumerate(items, 1))
    return _section(d.get("title_insights") or "观察与洞察",
                    f'<div class="insights">{rows}</div>',
                    icon="bulb", color="purple")


def _risks(d):
    items = d.get("risks") or []
    if not items:
        return ""
    ic = {"warn": "!", "bad": "×", "info": "i"}
    rows = []
    for r in items:
        if isinstance(r, str):
            r = {"text": r}
        lv = (r.get("level") or "info").lower()
        if lv in ("error", "high", "danger"):
            lv = "bad"
        if lv not in ic:
            lv = "info"
        rows.append(f'<div class="risk lv-{lv}"><div class="ic">{ic[lv]}</div>'
                    f'<div class="tx">{esc(r.get("text", ""))}</div></div>')
    return _section(d.get("title_risks") or "风险 / 提醒",
                    f'<div class="risks">{"".join(rows)}</div>', f"{len(rows)} 条",
                    icon="warn", color="orange")


def _timeline(d):
    items = d.get("timeline") or []
    if not items:
        return ""
    rows = []
    for x in items:
        if not isinstance(x, dict):
            x = {"title": str(x)}
        desc = f'<div class="tl-d">{esc(x["desc"])}</div>' if x.get("desc") else ""
        rows.append(f'<div class="tl-item"><div class="tl-t">{esc(x.get("time", ""))}</div>'
                    f'<div class="tl-h">{esc(x.get("title", ""))}</div>{desc}</div>')
    return _section(d.get("title_timeline") or "时间线",
                    f'<div class="tl">{"".join(rows)}</div>',
                    icon="clock", color="green")


def _appendix(d):
    items = d.get("appendix") or []
    if not items:
        return ""
    lines = []
    for x in items:
        if isinstance(x, str):
            x = {"text": x}
        tm = x.get("time") or x.get("ts_str") or ""
        who = x.get("who") or x.get("name") or ""
        lines.append(f'<div class="ln"><span class="tm">{esc(tm)}</span>'
                     f'<span class="wh">{esc(who)}</span>'
                     f'<span class="tx">{esc(x.get("text", ""))}</span></div>')
    n = len(items)
    return f"""<section class="sec"><details class="appendix">
  <summary>{_ico('notes', 'blue')}原始对话片段<span class="cnt">{n} 条</span></summary>
  <div class="raw">{''.join(lines)}</div>
</details></section>"""


# ── 主渲染 ───────────────────────────────────────────────────────────────────

def render(d, theme="light", avatar=True):
    """把分析 dict 渲染成完整 HTML 字符串。"""
    theme = theme if theme in THEMES else "light"
    AVATARS.clear()
    _AVATAR_CLASSES.clear()
    _PRIVATE_IDS.clear()
    ids, names = [], []
    _walk_ids(d, ids, names)
    _PRIVATE_IDS.update(ids)
    if d.get("gid"):
        _PRIVATE_IDS.add(str(d["gid"]))
    # 先按本报告的群重建昵称反查表 (清掉上一份的), 再收集头像:
    # 顺序不能反 —— collect_avatars 就是靠这张表把昵称转成 QQ号的。
    reset_resolver(d.get("gid"))
    stack = collect_avatars(d) if avatar else []
    body = "".join([
        _hero(d, stack), _summary(d), _activity(d), _quality(d), _topics(d),
        _qa(d), _todos(d), _people(d), _quotes(d), _insights(d), _risks(d),
        _timeline(d), _appendix(d),
    ])
    if not body.strip():
        body = '<div class="empty">报告内容为空 —— JSON 里没有任何可渲染的板块。</div>'
    gen = d.get("generated") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    period = d.get("period") or ""
    bits = [x for x in (esc(period) if period else "", esc(gen)) if x]
    # Group identifiers are omitted from the shareable footer.
    if d.get("group"):
        bits.insert(0, esc(d["group"]))
    dot = '<span class="fdot"></span>'
    foot = f'<div class="foot">{dot.join(bits)}</div>'
    title = d.get("group") or "群聊报告"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>{CSS.replace("/*__VARS__*/", THEMES[theme])}</style>
{_avatar_style()}
</head>
<body>
<div class="wrap">
{body}
{foot}
</div>
</body>
</html>
"""


# ── 输入契约 ─────────────────────────────────────────────────────────────────

SCHEMA = """\
输入 JSON 字段 (除 group 外全部可选; 为空/缺省 = 该板块整块跳过)

  group      str   群名 (标题)
  gid        str   群号
  kicker     str   Hero 徽章文案, 默认 "QQ 群聊日报"
  period     str   覆盖时间范围, 如 "10-03 16:53 → 21:38"
  generated  str   生成时间
  headline   str   一句话总览 (Hero 引言)
  stats      [{"label","value","icon"?}]  Hero 里的统计卡 (带彩色图标方块)。
                                             建议 3-4 个; icon 可选: chat/people/
                                             clock/fire/group/doc
  activity   [24 个数字] 或 [{"hour","count"}]  可选, 渲染成活跃时段柱状图。
             cli.py heatmap --json 可直接取到时序数据。
  summary    [str] 或 str          2-3 段摘要
  quality    {"title","subtitle","dimensions":[{"name","percentage","comment"}],
              "summary"}          聊天构成: 抽象维度 + 占比 + 锐评
  topics     [{"title","heat","tags":[str],"summary","points":[str],
               "contributors":[QQ号或{"name","uin"}],
               "quotes":[{"who","time","text","uin"}]}]   heat: 1-5 或 high/mid/low
  qa         [{"q","asker","time","answer","answerer","time_a"}]
  todos      [{"text","owner","note"}]
  people     [{"name","role","stat","desc","quote","uin"}]
  quotes     [{"who","time","text","note","uin"}]
  insights   [str]
  risks      [{"level","text"}]    level: info|warn|bad
  timeline   [{"time","title","desc"}]
  appendix   [{"time","who","text"}]

**人物头像（重要）**: `cli context` 输出的原料开头有一张「发言成员对照表」,
按多列对齐, 形如 `成员甲⚠=<成员甲_ID>   成员乙=<成员乙_ID>`。写 JSON 时把对应 QQ号
填进 `"uin"` 字段（qa 用 asker/answerer 时也给 `asker_uin`/`answerer_uin`），
渲染器默认尝试加载对应头像，接口失败时使用首字头像。带 `⚠` 的昵称在群里重名（同一昵称多个 QQ号），
**必须**写 QQ号 —— 只写名字会配错人。重名的候选 QQ号列在对照表上面的注释行里。
取不到时降级为昵称首字渐变头像。`--no-avatar` 完全关闭联网取头像。

**写完先自检**: `cli.py validate analysis.json --context ctx.txt` 会检查字段名
拼写、类型、占比合计，并按对照表补齐 `uin`、核对金句是否为原文/是否为本人所说。
`--fix` 输出修复版 JSON；仍需人工处理警告。成员表和 JSON 仅用于本地分析，
不得把真实编号、路径或密钥复制到帮助示例及用户可转发的说明里。HTML 隐藏结构化身份编号；
头像和昵称仍可识别人，不能称为匿名报告。上述 ID 仅为占位符。
"""

EXAMPLE = {
    "group": "示例群聊",
    "gid": "<群ID>",
    "kicker": "QQ 群聊日报",
    "period": "10-03 16:53 → 21:38",
    "generated": "2026-10-03 21:45",
    "headline": "一句话说清这段时间群里发生了什么、结论是什么。",
    "stats": [{"label": "条消息", "value": 320, "icon": "chat"},
              {"label": "人参与", "value": 22, "icon": "people"},
              {"label": "小时跨度", "value": 5.1, "icon": "clock"},
              {"label": "个议题", "value": 4, "icon": "fire"}],
    "activity": [0, 0, 0, 0, 0, 0, 1, 3, 8, 14, 11, 7, 9, 22, 31, 26, 18,
                 24, 35, 41, 29, 16, 6, 2],
    "summary": [
        "第一段: 这段时间群里的主线是什么。",
        "第二段: 值得记下来的判断或结论。",
    ],
    "quality": {
        "title": "群聊主题标题", "subtitle": "副标题一句话",
        "dimensions": [
            {"name": "抽象维度名", "percentage": 42, "comment": "这个维度下的具体锐评。"},
            {"name": "另一个维度", "percentage": 28, "comment": "点评。"},
        ],
        "summary": "全群表现的一句话总结。",
    },
    "topics": [{
        "title": "议题标题", "heat": 5, "tags": ["标签"],
        "summary": "议题概述。",
        "points": ["要点一", "要点二"],
        "contributors": ["<成员甲_ID>", "<成员乙_ID>"],
        "quotes": [{"who": "某人", "uin": "<成员甲_ID>", "time": "20:14",
                    "text": "原话引用。"}],
    }],
    "qa": [{"q": "提问", "asker": "甲", "asker_uin": "<成员甲_ID>", "time": "19:02",
            "answer": "回答", "answerer": "乙", "answerer_uin": "<成员乙_ID>",
            "time_a": "19:03"}],
    "todos": [{"text": "待办事项", "owner": "某人", "note": "补充"}],
    "people": [{"name": "某人", "role": "角色", "stat": "20:14 提出核心判断",
                "desc": "这个人在群里扮演什么角色。", "quote": "代表发言。"}],
    "quotes": [{"who": "某人", "time": "21:30", "text": "值得记录的一句话。",
                "note": "为什么记它"}],
    "insights": ["读完内容才写得出的观察。"],
    "risks": [{"level": "warn", "text": "需要注意的点。"}],
    "timeline": [{"time": "20:14", "title": "事件", "desc": "细节"}],
    "appendix": [{"time": "20:14", "who": "某人", "text": "原始消息"}],
}


def main():
    ap = argparse.ArgumentParser(
        description="把群聊分析 JSON 渲染成单文件 HTML 报告",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", nargs="?", help="分析 JSON 文件")
    ap.add_argument("-o", "--out", help="输出 HTML 路径")
    ap.add_argument("--theme", default="light", choices=["light", "dark"],
                    help="配色主题 (默认 light)")
    ap.add_argument("--no-avatar", action="store_true",
                    help="不联网取 QQ 头像, 全部用首字渐变头像")
    ap.add_argument("--stdout", action="store_true", help="输出到标准输出")
    ap.add_argument("--example", action="store_true", help="打印示例 JSON")
    ap.add_argument("--schema", action="store_true", help="打印字段说明")
    a = ap.parse_args()

    if a.schema:
        print(SCHEMA)
        return
    if a.example:
        print(json.dumps(EXAMPLE, ensure_ascii=False, indent=1))
        return
    if not a.src:
        ap.error("需要指定分析 JSON 文件 (或 --example / --schema)")
    with open(a.src, encoding="utf-8-sig") as f:
        d = json.load(f)
    out = render(d, theme=a.theme, avatar=not a.no_avatar)
    if a.stdout:
        print(out)
        return
    path = a.out or os.path.splitext(a.src)[0] + ".html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    n_av = len(AVATARS)
    print(f"已渲染 {path} ({len(out):,} 字节, 头像 {n_av} 张)")


if __name__ == "__main__":
    main()
