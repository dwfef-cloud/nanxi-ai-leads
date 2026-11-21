"""执行历史页（/history）—— 给「到点到底跑没跑」一个可以直接看的地方。

为什么单独做一页
----------------
运行日志抽屉是**单个任务**的视角：打开某个任务才看得到它跑过什么。
但用户真正要回答的问题是：「昨晚 9 点排期触发了吗？跑了几轮？命中几条？
是谁启动的（排期还是我点的）？为什么停了？」——这些跨越多个任务与触发源，
必须有一条统一时间线。

数据来源（全部是落库的真实记录，无一条前端构造）
------------------------------------------------
runtime_runs    运行时实例：谁、何时、触发方式、引擎、耗时、统计、结束原因
runtime_cycles  实例内的每一轮：读数 / 命中 / 浏览屏数
schedule_runs   排期到点的触发记录：哪个排期窗口、动作、结果、重试次数
/ipc/health     排期线程、引擎可用性、安全模式
"""
from __future__ import annotations

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>执行历史 · 阿里山AI获客</title>
<style>
  :root{
    --bg:#0b0e14; --panel:#121722; --panel2:#161c29; --line:#222e46;
    --text:#e6ecf7; --muted:#8794ad; --dim:#5f6b83;
    --ok:#35c08a; --warn:#e0a33e; --err:#e2606b; --blue:#6f8dff;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:13px/1.6 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
  header{position:sticky;top:0;z-index:9;background:rgba(11,14,20,.94);
    backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:14px 20px}
  h1{margin:0 0 10px;font-size:16px;font-weight:600;letter-spacing:.4px}
  .chips{display:flex;flex-wrap:wrap;gap:8px}
  .chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;
    background:var(--panel2);border:1px solid var(--line);color:var(--muted);font-size:12px}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--dim)}
  .dot.on{background:var(--ok);box-shadow:0 0 8px rgba(53,192,138,.7)}
  .dot.off{background:var(--err)}
  .dot.warn{background:var(--warn)}
  main{padding:16px 20px 60px;max-width:1400px;margin:0 auto}
  .bar{display:flex;gap:10px;align-items:center;margin:6px 0 14px;flex-wrap:wrap}
  .tab{padding:5px 12px;border-radius:8px;border:1px solid var(--line);
    background:var(--panel);color:var(--muted);cursor:pointer;font-size:12px}
  .tab.active{background:#1b2540;border-color:#33436b;color:var(--text)}
  .spacer{flex:1}
  .btn{padding:5px 12px;border-radius:8px;border:1px solid var(--line);
    background:var(--panel);color:var(--muted);cursor:pointer;font-size:12px}
  .btn:hover{color:var(--text);border-color:#33436b}
  h2{font-size:13px;color:var(--muted);font-weight:600;margin:22px 0 8px;
    letter-spacing:.5px;text-transform:none}
  table{width:100%;border-collapse:collapse;background:var(--panel);
    border:1px solid var(--line);border-radius:10px;overflow:hidden}
  th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #1b2334;vertical-align:top}
  th{background:var(--panel2);color:var(--muted);font-weight:500;font-size:12px;white-space:nowrap}
  tr:last-child td{border-bottom:0}
  tr.row{cursor:default}
  tr.row:hover td{background:#151b28}
  .name{color:var(--text)}
  .sub{color:var(--dim);font-size:11.5px}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px}
  .tag{display:inline-block;padding:1px 7px;border-radius:6px;font-size:11px;
    border:1px solid transparent;white-space:nowrap}
  .t-completed{color:var(--ok);background:rgba(53,192,138,.12);border-color:rgba(53,192,138,.3)}
  .t-running{color:var(--blue);background:rgba(111,141,255,.12);border-color:rgba(111,141,255,.3)}
  .t-failed{color:var(--err);background:rgba(226,96,107,.12);border-color:rgba(226,96,107,.3)}
  .t-stopped,.t-skipped{color:var(--warn);background:rgba(224,163,62,.12);border-color:rgba(224,163,62,.3)}
  .t-fired{color:var(--ok);background:rgba(53,192,138,.12);border-color:rgba(53,192,138,.3)}
  .empty{padding:26px;text-align:center;color:var(--dim);background:var(--panel);
    border:1px dashed var(--line);border-radius:10px}
  .cycles{display:none}
  .cycles.open{display:block}
  .cycles table{border:0;background:#0f141d}
  .err{color:var(--err)}
  footer{padding:14px 20px;color:var(--dim);font-size:11.5px;border-top:1px solid var(--line)}
  .kpi{display:flex;gap:22px;flex-wrap:wrap;margin:8px 0 4px}
  .kpi div{color:var(--muted);font-size:12px}
  .kpi b{display:block;color:var(--text);font-size:18px;font-weight:600}
</style>
</head>
<body>
<header>
  <h1>执行历史</h1>
  <div class="chips" id="chips"><span class="chip">正在读取状态…</span></div>
</header>
<main>
  <div class="kpi" id="kpi"></div>
  <div class="bar">
    <button class="tab active" data-kind="">全部</button>
    <button class="tab" data-kind="leadgen">评论获客</button>
    <button class="tab" data-kind="entity">线索采集</button>
    <button class="tab" data-kind="monitor">监控</button>
    <button class="tab" data-kind="warmup">养号</button>
    <span class="spacer"></span>
    <button class="btn" id="refresh">刷新</button>
    <button class="btn" id="auto">自动刷新：开</button>
  </div>

  <h2>运行记录（点一行展开每轮明细）</h2>
  <div id="runs"></div>

  <h2>排期触发记录（窗口幂等：同一窗口同一动作只触发一次）</h2>
  <div id="triggered"></div>
</main>
<footer id="foot">数据来自本机 runtime_runs / runtime_cycles / schedule_runs，刷新页面不会丢。</footer>

<script>
(function(){
  var kind = "";
  var auto = true;
  var timer = null;
  var cache = [];

  function esc(s){
    return String(s == null ? "" : s).replace(/[&<>"]/g, function(c){
      return ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"})[c];
    });
  }
  function ts(v){
    if (!v) return "—";
    return String(v).replace("T"," ").slice(0,19);
  }
  function dur(sec){
    if (sec == null) return "—";
    if (sec < 60) return sec + "s";
    var m = Math.floor(sec/60), s = Math.round(sec%60);
    return m + "m" + (s ? s + "s" : "");
  }
  function tag(status){
    var s = String(status||"");
    return '<span class="tag t-'+esc(s)+'">'+esc(s||"—")+'</span>';
  }
  function statsText(st){
    st = st || {};
    var parts = [];
    if (st.videos) parts.push("视频 "+st.videos);
    if (st.comments) parts.push("评论 "+st.comments);
    if (st.read) parts.push("读到 "+st.read);
    if (st.published) parts.push("已发表 "+st.published);
    if (st.interacted) parts.push("已互动 "+st.interacted);
    if (st.reached) parts.push("已触达 "+st.reached);
    if (st.already) parts.push("已评过 "+st.already);
    if (st.leads) parts.push("线索 "+st.leads);
    if (st.new) parts.push("新增 "+st.new);
    if (st.matched) parts.push("命中 "+st.matched);
    if (st.hit) parts.push("意向命中 "+st.hit);
    if (st.watched) parts.push("浏览 "+st.watched+" 屏");
    if (st.liked) parts.push("点赞 "+st.liked);
    if (st.cycles) parts.push("轮次 "+st.cycles);
    if (st.duplicated) parts.push("重复 "+st.duplicated);
    if (st.failed) parts.push("失败 "+st.failed);
    if (st.reason === "no-targets") parts.push("无可执行目标");
    return parts.join(" · ") || "—";
  }
  var KIND_LABEL = {entity:"线索采集", monitor:"监控", warmup:"养号", leadgen:"评论获客"};
  var TRIGGER_LABEL = {manual:"手动启动", schedule:"定时排期", startup:"开机恢复"};

  function invoke(channel, payload){
    return fetch("/ipc/"+encodeURIComponent(channel), {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({args:[payload||{}]})
    }).then(function(r){ return r.json(); }).then(function(p){
      if (!p || p.success !== true) throw new Error((p&&p.error)||"请求失败");
      return p.data;
    });
  }
  function get(url){
    return fetch(url).then(function(r){ return r.json(); });
  }

  function chips(health, hist){
    var sched = (health&&health.scheduler)||{};
    var eng = (hist&&hist.engine)||{};
    var out = [];
    out.push('<span class="chip"><i class="dot '+(sched.running?"on":"off")+'"></i>排期执行器'
      + (sched.running?"运行中":"未运行")+'</span>');
    out.push('<span class="chip"><i class="dot '+(sched.safeMode?"warn":"")+'"></i>合规安全模式'
      + (sched.safeMode?"已开启（全部停）":"关闭")+'</span>');
    var en = eng.engine || "none";
    var label = en === "shell" ? "桌面壳（已登录浏览器）" : (en === "playwright" ? "独立引擎" : "无可用引擎");
    out.push('<span class="chip"><i class="dot '+(en==="none"?"off":"on")+'"></i>执行引擎：'+esc(label)+'</span>');
    // 排期为什么没跑：执行器离线时整体暂停。不显示这一条的话，用户只能
    // 从「一张空表」猜，很容易误判成系统坏了。
    var ex = sched.executor || null;
    if (ex && ex.paused){
      out.push('<span class="chip" title="'+esc(ex.notice||"")+'"><i class="dot warn"></i>'
        +'排期已暂停：执行器离线 '+Math.max(1, Math.round((ex.offlineSec||0)/60))
        +' 分钟（恢复后自动继续）</span>');
    } else if (ex && !ex.online){
      out.push('<span class="chip" title="还差 '+esc(String(ex.graceSec||0))
        +' 秒的宽限期才会整体暂停"><i class="dot warn"></i>执行器离线 '
        +esc(String(ex.offlineSec||0))+'s</span>');
    }
    if (hist && hist.active && hist.active.length){
      out.push('<span class="chip"><i class="dot on"></i>在跑 '+hist.active.length+' 个任务</span>');
    }
    if (hist && hist.diagnostics){
      out.push('<span class="chip">证据：运行 '+hist.diagnostics.runs
        +' · 轮次 '+hist.diagnostics.cycles+' · 已读评论 '+hist.diagnostics.seen+'</span>');
    }
    out.push('<span class="chip">更新于 '+esc(ts(hist&&hist.generatedAt))+'</span>');
    document.getElementById("chips").innerHTML = out.join("");
  }

  function kpi(hist){
    var runs = (hist&&hist.runs)||[];
    var today = new Date().toISOString().slice(0,10);
    var t = runs.filter(function(r){ return String(r.startedAt||"").indexOf(today)===0; });
    var done = t.filter(function(r){ return r.status==="completed"; }).length;
    var fail = t.filter(function(r){ return r.status==="failed"; }).length;
    var leads = 0, matched = 0;
    t.forEach(function(r){
      var s = r.stats||{};
      leads += Number(s.leads||0); matched += Number(s.matched||0);
    });
    document.getElementById("kpi").innerHTML =
      '<div><b>'+t.length+'</b>今日执行</div>'+
      '<div><b>'+done+'</b>完成</div>'+
      '<div><b class="'+(fail?'err':'')+'">'+fail+'</b>失败</div>'+
      '<div><b>'+leads+'</b>新增线索</div>'+
      '<div><b>'+matched+'</b>监控命中</div>';
  }

  function renderRuns(hist){
    var box = document.getElementById("runs");
    var runs = (hist&&hist.runs)||[];
    if (!runs.length){
      box.innerHTML = '<div class="empty">还没有执行记录。点任务上的「开始」或等到排期时间，'
        + '这里会出现每一次真实的运行（含每轮统计与结束原因）。</div>';
      return;
    }
    var rows = runs.map(function(r, i){
      return '<tr class="row" data-i="'+i+'">'
        + '<td><div class="name">'+esc(r.task_name||r.task_id)+'</div>'
        + '<div class="sub mono">'+esc(r.run_id)+'</div></td>'
        + '<td>'+esc(KIND_LABEL[r.kind]||r.kind)+'</td>'
        + '<td>'+esc(TRIGGER_LABEL[r.trigger]||r.trigger||"—")+'</td>'
        + '<td>'+tag(r.status)+'</td>'
        + '<td class="mono">'+esc(ts(r.startedAt))+'<div class="sub">'
          + esc(r.endedAt?ts(r.endedAt):"进行中")+'</div></td>'
        + '<td>'+esc(dur(r.durationSec))+'</td>'
        + '<td>'+esc(statsText(r.stats))+'</td>'
        + '<td class="sub">'+esc(r.message||"—")+'</td>'
        + '</tr>'
        + '<tr class="cycles" id="cy-'+i+'"><td colspan="8"><div class="sub">读取每轮明细…</div></td></tr>';
    }).join("");
    box.innerHTML = '<table><thead><tr><th>任务</th><th>类型</th><th>触发</th><th>状态</th>'
      + '<th>开始 / 结束</th><th>耗时</th><th>统计</th><th>结束说明</th></tr></thead><tbody>'
      + rows + '</tbody></table>';

    Array.prototype.forEach.call(box.querySelectorAll("tr.row"), function(tr){
      tr.addEventListener("click", function(){
        var i = tr.getAttribute("data-i");
        var detail = document.getElementById("cy-"+i);
        var open = detail.classList.toggle("open");
        if (!open) return;
        var run = runs[Number(i)];
        invoke("get-runtime-cycles", {runId: run.run_id, limit: 100}).then(function(d){
          var items = (d&&d.cycles)||[];
          if (!items.length){
            detail.innerHTML = '<td colspan="8"><div class="sub">这次执行没有分轮记录'
              + '（线索采集按目标分轮，若一个目标都没执行则无轮次）。</div></td>';
            return;
          }
          var rows2 = items.slice().reverse().map(function(c){
            return '<tr><td class="mono">#'+esc(c.cycle_no)+'</td><td>'+tag(c.status)+'</td>'
              + '<td class="mono">'+esc(c.startedAt)+'</td><td>'+esc(dur(c.ended_ms&&c.started_ms?Math.round((c.ended_ms-c.started_ms)/1000):null))+'</td>'
              + '<td>'+esc(statsText(c.stats))+'</td><td class="sub">'+esc(c.detail||"—")+'</td></tr>';
          }).join("");
          detail.innerHTML = '<td colspan="8"><table><thead><tr><th>轮次</th><th>状态</th>'
            + '<th>开始</th><th>耗时</th><th>统计</th><th>说明</th></tr></thead><tbody>'
            + rows2 + '</tbody></table></td>';
        }).catch(function(e){
          detail.innerHTML = '<td colspan="8"><div class="err">读取轮次失败：'+esc(e.message)+'</div></td>';
        });
      });
    });
  }

  function renderTriggered(hist){
    var box = document.getElementById("triggered");
    var rows = (hist&&hist.triggered)||[];
    if (!rows.length){
      box.innerHTML = '<div class="empty">还没有排期到点的触发记录。'
        + '给任务设一个每日时段后，到点这里会出现一条 fired / skipped（含原因）。</div>';
      return;
    }
    box.innerHTML = '<table><thead><tr><th>时间</th><th>类型</th><th>任务</th>'
      + '<th>窗口</th><th>动作</th><th>结果</th><th>说明</th></tr></thead><tbody>'
      + rows.map(function(r){
        return '<tr><td class="mono">'+esc(r.fired_at||"—")+'</td>'
          + '<td>'+esc(KIND_LABEL[r.kind]||r.kind)+'</td>'
          + '<td class="mono">'+esc(r.task_id)+'</td>'
          + '<td class="mono">'+esc(r.window_key||"—")+'</td>'
          + '<td>'+esc(r.action==="start"?"启动":"停止")+'</td>'
          + '<td>'+tag(r.status)+'</td>'
          + '<td class="sub">'+esc(r.detail||"—")
          + (Number(r.attempt)>1?' <span class="sub">（第 '+esc(r.attempt)+' 次尝试）</span>':'')
          + '</td></tr>';
      }).join("") + '</tbody></table>';
  }

  function load(){
    Promise.all([
      get("/ipc/health").catch(function(){ return {}; }),
      invoke("get-execution-history", {kind: kind, limit: 80}).catch(function(e){ return {error:e.message}; })
    ]).then(function(res){
      var health = res[0], hist = res[1];
      if (hist && hist.error){
        document.getElementById("runs").innerHTML =
          '<div class="empty err">读取执行历史失败：'+esc(hist.error)+'</div>';
        return;
      }
      chips(health, hist);
      kpi(hist);
      renderRuns(hist);
      renderTriggered(hist);
      document.getElementById("foot").textContent =
        "数据来自本机 runtime_runs / runtime_cycles / schedule_runs；"
        + (hist.note||"") + " 最后更新 " + ts(hist.generatedAt);
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll(".tab"), function(btn){
    btn.addEventListener("click", function(){
      Array.prototype.forEach.call(document.querySelectorAll(".tab"), function(b){
        b.classList.remove("active");
      });
      btn.classList.add("active");
      kind = btn.getAttribute("data-kind")||"";
      load();
    });
  });
  document.getElementById("refresh").addEventListener("click", load);
  document.getElementById("auto").addEventListener("click", function(){
    auto = !auto;
    this.textContent = "自动刷新：" + (auto?"开":"关");
    if (auto) { tick(); } else if (timer) { clearInterval(timer); timer = null; }
  });
  function tick(){
    if (timer) clearInterval(timer);
    timer = setInterval(function(){ if (auto) load(); }, 5000);
  }
  load();
  tick();
})();
</script>
</body>
</html>
"""
