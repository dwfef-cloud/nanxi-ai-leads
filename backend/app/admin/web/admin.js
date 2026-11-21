/* 运营后台前端逻辑（无外部依赖，CSP 下只用同源脚本）。
   鉴权：令牌存 sessionStorage，关闭标签页即失效；401 自动回登录页。 */
(function () {
  'use strict';

  // ---------- 主题：与主界面共用 localStorage key，切一处两边一致 ----------
  var THEME_KEY = 'ui_theme';
  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    document.documentElement.style.colorScheme = t;
  }
  applyTheme(localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark');

  // ---------- 工具 ----------
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function num(n) {
    n = Number(n || 0);
    return n.toLocaleString('zh-CN');
  }
  function fmtTime(iso) {
    if (!iso) return '—';
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return String(iso);
      var p = function (x) { return x < 10 ? '0' + x : '' + x; };
      return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
             ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
    } catch (e) { return String(iso); }
  }
  function ago(iso) {
    if (!iso) return '从未';
    var t = new Date(iso).getTime();
    if (isNaN(t)) return String(iso);
    var s = Math.floor((Date.now() - t) / 1000);
    if (s < 60) return s + ' 秒前';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
    if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
    return Math.floor(s / 86400) + ' 天前';
  }
  var toastTimer = null;
  function toast(msg, isErr) {
    var el = $('toast');
    el.textContent = msg;
    el.className = 'toast' + (isErr ? ' err' : '');
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 2600);
  }
  function copyText(text) {
    // 传统降级：隐藏 textarea + execCommand，兼容 iframe / 旧内核 /
    // 剪贴板权限被拒（预览面板等非独立窗口环境）的场景
    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;top:-999px;opacity:0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { /* 忽略 */ }
      document.body.removeChild(ta);
      return ok;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).catch(function () {
        if (!fallback()) throw new Error('浏览器拒绝了剪贴板访问');
      });
    }
    return Promise.resolve().then(function () {
      if (!fallback()) throw new Error('浏览器拒绝了剪贴板访问');
    });
  }

  // ---------- API ----------
  var TOKEN_KEY = 'admin_token';
  function token() { return sessionStorage.getItem(TOKEN_KEY) || ''; }

  function api(path, options) {
    options = options || {};
    var headers = { 'Content-Type': 'application/json' };
    if (token()) headers['X-Admin-Token'] = token();
    return fetch('/admin/api' + path, {
      method: options.method || 'GET',
      headers: headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
      credentials: 'same-origin'
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (res.status === 401) {
          sessionStorage.removeItem(TOKEN_KEY);
          showLogin('会话已过期，请重新登录');
          throw new Error('unauthorized');
        }
        if (!res.ok) throw new Error(data.detail || ('请求失败（' + res.status + '）'));
        return data;
      });
    });
  }

  // ---------- 登录 ----------
  function showLogin(msg) {
    $('appView').hidden = true;
    $('loginView').hidden = false;
    if (msg) { $('loginError').textContent = msg; $('loginError').hidden = false; }
    setTimeout(function () { $('password').focus(); }, 50);
  }
  function showApp() {
    $('loginView').hidden = true;
    $('appView').hidden = false;
    switchView('overview');
  }

  $('loginBtn').addEventListener('click', doLogin);
  $('password').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') doLogin();
  });

  function doLogin() {
    var pw = $('password').value;
    if (!pw) { $('loginError').textContent = '请输入口令'; $('loginError').hidden = false; return; }
    $('loginBtn').disabled = true;
    api('/login', { method: 'POST', body: { password: pw } })
      .then(function (d) {
        sessionStorage.setItem(TOKEN_KEY, d.token);
        $('password').value = '';
        showApp();
      })
      .catch(function (e) {
        if (e.message === 'unauthorized') {
          $('loginError').textContent = '口令不正确';
          $('loginError').hidden = false;
        } else {
          $('loginError').textContent = e.message;
          $('loginError').hidden = false;
        }
      })
      .finally(function () { $('loginBtn').disabled = false; });
  }

  $('logoutBtn').addEventListener('click', function () {
    api('/logout', { method: 'POST' }).catch(function () {});
    sessionStorage.removeItem(TOKEN_KEY);
    showLogin('已退出登录');
  });

  $('themeBtn').addEventListener('click', function () {
    var next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    applyTheme(next);
    localStorage.setItem(THEME_KEY, next);
    redrawCharts();
  });

  // ---------- 视图切换 ----------
  var TITLES = {
    overview: ['概览', '密钥签发与多用户使用情况'],
    licenses: ['密钥管理', '生成、延期、暂停、吊销授权密钥'],
    instances: ['用户监控', '客户机在线状态与使用量'],
    usage: ['用量分析', '按天 / 按密钥的使用量统计'],
    audit: ['操作日志', '后台所有变更留痕'],
    guide: ['接入说明', '客户机接入的三个接口'],
    settings: ['设置', '后台口令与运行信息']
  };
  var current = 'overview';
  var chartData = { trend: null, usage: null };

  function switchView(name) {
    current = name;
    Array.prototype.forEach.call(document.querySelectorAll('.nav-item'), function (b) {
      b.classList.toggle('active', b.getAttribute('data-view') === name);
    });
    Array.prototype.forEach.call(document.querySelectorAll('.view'), function (v) {
      v.hidden = v.id !== 'view-' + name;
    });
    $('pageTitle').textContent = TITLES[name][0];
    $('pageSub').textContent = TITLES[name][1];
    if (name === 'overview') loadOverview();
    if (name === 'licenses') loadLicenses();
    if (name === 'instances') loadInstances();
    if (name === 'usage') loadUsage();
    if (name === 'audit') loadAudit();
    if (name === 'guide') {
      var base = location.origin + '/admin/api';
      $('guideBase').textContent = base;
      $('guideBase2').textContent = base;
      $('guideBase3').textContent = base;
    }
    if (name === 'settings') $('infoUrl').textContent = location.origin + '/admin/';
  }
  $('nav').addEventListener('click', function (e) {
    var btn = e.target.closest('.nav-item');
    if (btn) switchView(btn.getAttribute('data-view'));
  });
  $('refreshBtn').addEventListener('click', function () { switchView(current); });

  // ---------- 概览 ----------
  function loadOverview() {
    api('/overview?days=14').then(function (d) {
      var s = d.licenses, i = d.instances, t = d.today;
      $('kpis').innerHTML = [
        kpi('密钥总数', num(s.total), '未激活 ' + num(s.unused) + ' · 已激活 ' + num(s.active), ''),
        kpi('在线实例', num(i.online), '累计 ' + num(i.total) + ' 台，离线 ' + num(i.offline), 'k-green'),
        kpi('今日动作', num(t.actions), '采集 ' + num(t.leads) + ' · 评论 ' + num(t.comments) + ' · 私信 ' + num(t.dms), 'k-purple'),
        kpi('今日登录', num(t.logins), '活跃实例 ' + num(t.activeInstances) + ' 台', 'k-amber')
      ].join('');

      chartData.trend = d.series;
      renderChart($('trendChart'), d.series, [
        { key: 'leads', color: 'primary' },
        { key: 'comments', color: 'green' },
        { key: 'dms', color: 'purple' }
      ]);

      var top = d.topLicenses || [];
      $('topLicenses').innerHTML = top.length ? tableHtml(
        ['客户 / 密钥', '采集', '评论', '私信', '动作'],
        top.map(function (r) {
          return [
            '<span>' + esc(r.customer_name || '未命名客户') + '</span><br>' +
            '<span class="mono muted">' + esc(r.license_code) + '</span>',
            '<span class="num">' + num(r.leads) + '</span>',
            '<span class="num">' + num(r.comments) + '</span>',
            '<span class="num">' + num(r.dms) + '</span>',
            '<span class="num">' + num(r.actions) + '</span>'
          ];
        })
      ) : emptyBlock('暂无用量数据', '客户机接入并上报后，这里会出现按客户的用量排行');

      var au = d.recentAudit || [];
      $('recentAudit').innerHTML = au.length ? au.map(function (r) {
        return '<div class="tl-item"><i class="tl-dot"></i><div class="tl-body">' +
               '<b>' + esc(r.action) + '</b> ' + (r.target ? '<span>· ' + esc(r.target) + '</span>' : '') +
               '<br><span>' + esc(fmtTime(r.at)) + (r.detail ? ' · ' + esc(r.detail) : '') + '</span></div></div>';
      }).join('') : '<div class="empty">暂无操作记录</div>';
    }).catch(function (e) { if (e.message !== 'unauthorized') toast(e.message, true); });
  }

  function kpi(label, value, foot, cls) {
    return '<div class="kpi ' + (cls || '') + '">' +
      '<div class="k-label">' + esc(label) + '</div>' +
      '<div class="k-value">' + esc(value) + '</div>' +
      '<div class="k-foot">' + esc(foot) + '</div></div>';
  }

  function emptyBlock(title, sub) {
    return '<div class="empty"><b>' + esc(title) + '</b>' + esc(sub) + '</div>';
  }

  function tableHtml(headers, rows, rightFrom) {
    rightFrom = rightFrom == null ? headers.length : rightFrom;
    var thead = '<tr>' + headers.map(function (h, i) {
      return '<th' + (i >= rightFrom ? ' class="num"' : '') + '>' + esc(h) + '</th>';
    }).join('') + '</tr>';
    var tbody = rows.map(function (cells) {
      return '<tr>' + cells.map(function (c, i) {
        var cls = i >= rightFrom ? ' class="num"' : '';
        return '<td' + cls + '>' + c + '</td>';
      }).join('') + '</tr>';
    }).join('');
    return '<table><thead>' + thead + '</thead><tbody>' + tbody + '</tbody></table>';
  }

  // ---------- SVG 折线图 ----------
  var PALETTE = { primary: '#3b82f6', green: '#10b981', purple: '#a78bfa', amber: '#f59e0b' };

  function renderChart(container, series, keys) {
    if (!container) return;
    if (!series || !series.length) {
      container.innerHTML = emptyBlock('暂无趋势数据', '有客户机上报后自动生成');
      return;
    }
    var W = container.clientWidth || 620, H = container.clientHeight || 200;
    var padL = 38, padR = 10, padT = 12, padB = 22;
    var iw = Math.max(10, W - padL - padR), ih = Math.max(10, H - padT - padB);
    var max = 1;
    series.forEach(function (p) {
      keys.forEach(function (k) { max = Math.max(max, Number(p[k.key] || 0)); });
    });
    max = niceMax(max);
    var n = series.length;
    var stepX = n > 1 ? iw / (n - 1) : 0;
    function X(i) { return padL + (n > 1 ? i * stepX : iw / 2); }
    function Y(v) { return padT + ih - (Number(v || 0) / max) * ih; }

    var parts = [];
    // 网格 + y 轴刻度
    for (var g = 0; g <= 4; g++) {
      var y = padT + (ih / 4) * g;
      var val = Math.round(max - (max / 4) * g);
      parts.push('<line x1="' + padL + '" y1="' + y + '" x2="' + (W - padR) + '" y2="' + y +
        '" stroke="currentColor" stroke-opacity="0.08" stroke-width="1"/>');
      parts.push('<text x="' + (padL - 7) + '" y="' + (y + 4) + '" text-anchor="end" ' +
        'font-size="10" fill="currentColor" fill-opacity="0.45">' + val + '</text>');
    }
    // x 轴标签（最多 7 个）
    var labelEvery = Math.max(1, Math.ceil(n / 7));
    series.forEach(function (p, i) {
      if (i % labelEvery !== 0 && i !== n - 1) return;
      parts.push('<text x="' + X(i) + '" y="' + (H - 6) + '" text-anchor="middle" ' +
        'font-size="10" fill="currentColor" fill-opacity="0.45">' + esc(String(p.day).slice(5)) + '</text>');
    });
    // 折线 + 面积
    keys.forEach(function (k, ki) {
      var d = series.map(function (p, i) {
        return (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(p[k.key]).toFixed(1);
      }).join(' ');
      if (ki === 0) {
        var area = d + ' L' + X(n - 1).toFixed(1) + ' ' + (padT + ih) +
                   ' L' + X(0).toFixed(1) + ' ' + (padT + ih) + ' Z';
        parts.push('<path d="' + area + '" fill="' + PALETTE[k.color] + '" fill-opacity="0.10"/>');
      }
      parts.push('<path d="' + d + '" fill="none" stroke="' + PALETTE[k.color] +
        '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>');
      series.forEach(function (p, i) {
        parts.push('<circle cx="' + X(i).toFixed(1) + '" cy="' + Y(p[k.key]).toFixed(1) +
          '" r="2.2" fill="' + PALETTE[k.color] + '"><title>' +
          esc(p.day + ' ' + k.key + '：' + p[k.key]) + '</title></circle>');
      });
    });
    container.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" ' +
      'style="color:currentColor">' + parts.join('') + '</svg>';
  }

  function niceMax(v) {
    if (v <= 5) return 5;
    var mag = Math.pow(10, Math.floor(Math.log10(v)));
    var norm = v / mag;
    var step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
    return step * mag;
  }

  function redrawCharts() {
    if (chartData.trend && current === 'overview') {
      renderChart($('trendChart'), chartData.trend, [
        { key: 'leads', color: 'primary' },
        { key: 'comments', color: 'green' },
        { key: 'dms', color: 'purple' }
      ]);
    }
    if (chartData.usage && current === 'usage') {
      renderChart($('usageChart'), chartData.usage, [
        { key: 'leads', color: 'primary' },
        { key: 'comments', color: 'green' },
        { key: 'dms', color: 'purple' },
        { key: 'logins', color: 'amber' }
      ]);
    }
  }
  window.addEventListener('resize', function () {
    clearTimeout(window.__rz);
    window.__rz = setTimeout(redrawCharts, 180);
  });

  // ---------- 密钥管理 ----------
  var licState = { page: 1, size: 20, total: 0, status: '', plan: '', keyword: '' };

  function loadLicenses() {
    var qs = '?page=' + licState.page + '&size=' + licState.size +
      '&status=' + encodeURIComponent(licState.status) +
      '&plan=' + encodeURIComponent(licState.plan) +
      '&keyword=' + encodeURIComponent(licState.keyword);
    api('/licenses' + qs).then(function (d) {
      licState.total = d.total;
      var rows = d.items.map(function (r) {
        var statusCls = 'b-' + r.status;
        var actions = [
          '<button class="btn btn-ghost btn-sm" data-act="extend" data-code="' + esc(r.code) + '">延期</button>',
          r.status === 'revoked'
            ? '<button class="btn btn-ghost btn-sm" data-act="restore" data-code="' + esc(r.code) + '">恢复</button>'
            : '<button class="btn btn-ghost btn-sm" data-act="suspend" data-code="' + esc(r.code) + '">暂停</button>',
          r.status !== 'revoked'
            ? '<button class="btn btn-danger btn-sm" data-act="revoke" data-code="' + esc(r.code) + '">吊销</button>'
            : '',
          '<button class="btn btn-ghost btn-sm" data-act="edit" data-code="' + esc(r.code) + '">编辑</button>'
        ].filter(Boolean).join('');
        var exp = r.expiresAt
          ? '<span>' + esc(fmtTime(r.expiresAt).slice(0, 10)) + '</span><br><span class="muted">' +
            (r.daysLeft == null ? '' : (r.daysLeft < 0 ? '已过期' : '剩 ' + r.daysLeft + ' 天')) + '</span>'
          : '<span class="muted">永久</span>';
        return [
          '<span class="mono">' + esc(r.code) + '</span>' +
            '<button class="copy-btn" data-copy="' + esc(r.code) + '">复制</button>',
          '<span class="badge b-plan">' + esc(r.plan) + '</span>',
          '<span>' + esc(r.customerName || '—') + '</span>' +
            (r.customerContact ? '<br><span class="muted">' + esc(r.customerContact) + '</span>' : ''),
          '<span class="badge ' + statusCls + '">' + esc(statusText(r.status)) + '</span>',
          exp,
          r.boundMachine ? '<span class="mono muted">' + esc(r.boundMachine.slice(0, 12)) + '…</span>'
                         : '<span class="muted">未绑定</span>',
          '<div class="row-actions">' + actions + '</div>'
        ];
      });
      $('licTable').innerHTML = rows.length
        ? tableHtml(['密钥', '套餐', '客户', '状态', '到期', '绑定机器', '操作'], rows, 6)
        : emptyBlock('还没有密钥', '点右上角「生成密钥」创建第一把');
      $('licTotal').textContent = '共 ' + num(d.total) + ' 把';
      var pages = Math.max(1, Math.ceil(d.total / licState.size));
      $('licPage').textContent = licState.page + ' / ' + pages;
      $('licPrev').disabled = licState.page <= 1;
      $('licNext').disabled = licState.page >= pages;
    }).catch(function (e) { if (e.message !== 'unauthorized') toast(e.message, true); });
  }

  function statusText(s) {
    return { unused: '未激活', active: '已激活', suspended: '已暂停',
             revoked: '已吊销', expired: '已过期' }[s] || s;
  }

  $('licQueryBtn').addEventListener('click', function () {
    licState.page = 1;
    licState.keyword = $('licSearch').value.trim();
    licState.status = $('licStatus').value;
    licState.plan = $('licPlan').value;
    loadLicenses();
  });
  $('licSearch').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') $('licQueryBtn').click();
  });
  $('licPrev').addEventListener('click', function () {
    if (licState.page > 1) { licState.page--; loadLicenses(); }
  });
  $('licNext').addEventListener('click', function () {
    licState.page++; loadLicenses();
  });
  $('licExportBtn').addEventListener('click', function () {
    // 走同源下载；fetch 带令牌再落盘，避免 <a> 无法带自定义头
    fetch('/admin/api/export/licenses.csv', { headers: { 'X-Admin-Token': token() } })
      .then(function (r) {
        if (!r.ok) throw new Error('导出失败');
        return r.blob();
      })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = 'licenses.csv';
        a.click();
        setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
        toast('已导出 CSV');
      })
      .catch(function (e) { toast(e.message, true); });
  });

  // 表格内操作（事件委托）
  $('licTable').addEventListener('click', function (e) {
    var cp = e.target.closest('[data-copy]');
    if (cp) {
      copyText(cp.getAttribute('data-copy'))
        .then(function () { toast('已复制密钥'); })
        .catch(function (e) { toast(e.message || '复制失败，请手动选中复制', true); });
      return;
    }
    var btn = e.target.closest('[data-act]');
    if (!btn) return;
    var act = btn.getAttribute('data-act');
    var code = btn.getAttribute('data-code');
    if (act === 'extend') return openExtend(code);
    if (act === 'edit') return openEdit(code);
    if (act === 'suspend') return quickPatch(code, { status: 'suspended' }, '已暂停该密钥');
    if (act === 'restore') return quickPatch(code, { status: 'active' }, '已恢复该密钥');
    if (act === 'revoke') {
      if (!window.confirm('吊销后客户机下次心跳即停止工作，确认吊销？\n' + code)) return;
      return quickPatch(code, { status: 'revoked' }, '已吊销该密钥');
    }
  });

  function quickPatch(code, body, okMsg) {
    api('/licenses/' + encodeURIComponent(code), { method: 'PATCH', body: body })
      .then(function () { toast(okMsg); loadLicenses(); })
      .catch(function (e) { toast(e.message, true); });
  }

  // ---------- 弹窗 ----------
  function openModal(title, bodyHtml, footHtml, onMount) {
    $('modalTitle').textContent = title;
    $('modalBody').innerHTML = bodyHtml;
    $('modalFoot').innerHTML = footHtml;
    $('modalMask').hidden = false;
    if (onMount) onMount();
  }
  function closeModal() { $('modalMask').hidden = true; }
  $('modalClose').addEventListener('click', closeModal);
  $('modalMask').addEventListener('click', function (e) {
    if (e.target === $('modalMask')) closeModal();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !$('modalMask').hidden) closeModal();
  });

  $('licNewBtn').addEventListener('click', function () {
    openModal('生成密钥',
      '<div class="field"><span>套餐</span><select id="gPlan" class="input">' +
        '<option value="basic">基础版</option>' +
        '<option value="standard" selected>标准版</option>' +
        '<option value="pro">专业版</option>' +
        '<option value="ultimate">旗舰版</option></select></div>' +
      '<div class="grid-2-inner">' +
        '<div class="field"><span>数量（1-200）</span><input id="gCount" type="number" value="1" min="1" max="200" /></div>' +
        '<div class="field"><span>有效天数（空=永久）</span><input id="gDays" type="number" value="365" min="1" placeholder="留空表示永久" /></div>' +
      '</div>' +
      '<div class="grid-2-inner">' +
        '<div class="field"><span>席位数</span><input id="gSeats" type="number" value="1" min="1" /></div>' +
        '<div class="field"><span>日动作配额（0=不限）</span><input id="gQuota" type="number" value="0" min="0" /></div>' +
      '</div>' +
      '<div class="field"><span>客户名称</span><input id="gName" placeholder="例如：杭州某某电商" /></div>' +
      '<div class="field"><span>联系方式</span><input id="gContact" placeholder="手机 / 微信" /></div>' +
      '<div class="field"><span>备注</span><input id="gNote" placeholder="可留空" /></div>' +
      '<div id="gResult"></div>',
      '<button class="btn btn-ghost" id="gCancel" type="button">关闭</button>' +
      '<button class="btn btn-primary" id="gSubmit" type="button">生成</button>',
      function () {
        $('gCancel').addEventListener('click', closeModal);
        $('gSubmit').addEventListener('click', function () {
          var body = {
            plan: $('gPlan').value,
            count: Number($('gCount').value || 1),
            validDays: $('gDays').value === '' ? null : Number($('gDays').value),
            seats: Number($('gSeats').value || 1),
            dailyQuota: Number($('gQuota').value || 0),
            customerName: $('gName').value.trim(),
            customerContact: $('gContact').value.trim(),
            note: $('gNote').value.trim()
          };
          $('gSubmit').disabled = true;
          api('/licenses', { method: 'POST', body: body })
            .then(function (d) {
              $('gResult').innerHTML = '<div class="code-result" id="gCodes">' +
                d.codes.map(function (c) { return esc(c); }).join('<br>') + '</div>' +
                '<p class="hint">已生成 ' + d.codes.length + ' 把，' +
                (d.expiresAt ? '到期 ' + esc(fmtTime(d.expiresAt).slice(0, 10)) : '永久有效') +
                '。点下面按钮可整批复制。</p>';
              $('modalFoot').innerHTML =
                '<button class="btn btn-ghost" id="gCopy" type="button">复制全部</button>' +
                '<button class="btn btn-primary" id="gDone" type="button">完成</button>';
              $('gCopy').addEventListener('click', function () {
                copyText(d.codes.join('\n'))
                  .then(function () { toast('已复制 ' + d.codes.length + ' 把密钥'); })
                  .catch(function (e) { toast(e.message || '复制失败，请在输入框全选复制', true); });
              });
              $('gDone').addEventListener('click', function () { closeModal(); loadLicenses(); });
              toast('生成成功');
            })
            .catch(function (e) { toast(e.message, true); })
            .finally(function () { $('gSubmit').disabled = false; });
        });
      });
  });

  function openExtend(code) {
    openModal('延期 · ' + code,
      '<div class="field"><span>延长天数（在当前到期日基础上叠加，正数）</span>' +
      '<input id="eDays" type="number" value="365" min="1" /></div>' +
      '<p class="hint">永久授权的密钥延期后同样变为有到期日。</p>',
      '<button class="btn btn-ghost" id="eCancel" type="button">取消</button>' +
      '<button class="btn btn-primary" id="eOk" type="button">确认延期</button>',
      function () {
        $('eCancel').addEventListener('click', closeModal);
        $('eOk').addEventListener('click', function () {
          api('/licenses/' + encodeURIComponent(code), {
            method: 'PATCH', body: { extendDays: Number($('eDays').value || 0) }
          }).then(function () { toast('已延期'); closeModal(); loadLicenses(); })
            .catch(function (e) { toast(e.message, true); });
        });
      });
  }

  function openEdit(code) {
    api('/licenses?keyword=' + encodeURIComponent(code) + '&size=1').then(function (d) {
      var r = (d.items || [])[0];
      if (!r) { toast('密钥不存在', true); return; }
      openModal('编辑 · ' + code,
        '<div class="field"><span>客户名称</span><input id="edName" value="' + esc(r.customerName) + '" /></div>' +
        '<div class="field"><span>联系方式</span><input id="edContact" value="' + esc(r.customerContact) + '" /></div>' +
        '<div class="grid-2-inner">' +
          '<div class="field"><span>席位数</span><input id="edSeats" type="number" value="' + esc(r.seats) + '" min="1" /></div>' +
          '<div class="field"><span>日配额（0=不限）</span><input id="edQuota" type="number" value="' + esc(r.dailyQuota) + '" min="0" /></div>' +
        '</div>' +
        '<div class="field"><span>备注</span><input id="edNote" value="' + esc(r.note) + '" /></div>' +
        (r.boundMachine ? '<div class="field"><span>绑定机器</span><input value="' + esc(r.boundMachine) + '" disabled /></div>' +
          '<label class="checkbox-row"><input type="checkbox" id="edUnbind" /> 解除机器绑定（换机时勾选）</label>' : '') +
        '<p class="hint">删除密钥请在列表里用「吊销」，删除会丢掉台账记录。</p>',
        '<button class="btn btn-ghost" id="edCancel" type="button">取消</button>' +
        '<button class="btn btn-primary" id="edOk" type="button">保存</button>',
        function () {
          $('edCancel').addEventListener('click', closeModal);
          $('edOk').addEventListener('click', function () {
            var body = {
              customerName: $('edName').value.trim(),
              customerContact: $('edContact').value.trim(),
              seats: Number($('edSeats').value || 1),
              dailyQuota: Number($('edQuota').value || 0),
              note: $('edNote').value.trim()
            };
            var ub = $('edUnbind');
            if (ub && ub.checked) body.unbind = true;
            api('/licenses/' + encodeURIComponent(code), { method: 'PATCH', body: body })
              .then(function () { toast('已保存'); closeModal(); loadLicenses(); })
              .catch(function (e) { toast(e.message, true); });
          });
        });
    }).catch(function (e) { toast(e.message, true); });
  }

  // ---------- 用户监控 ----------
  function loadInstances() {
    var kw = $('instSearch').value.trim();
    api('/instances?keyword=' + encodeURIComponent(kw)).then(function (d) {
      $('instKpis').innerHTML = [
        kpi('在线', num(d.online), '10 分钟内有心跳', 'k-green'),
        kpi('总计', num(d.total), '历史接入过的机器', ''),
        kpi('离线', num(d.total - d.online), '超过 10 分钟无心跳', 'k-amber'),
        kpi('在线率', d.total ? Math.round(d.online / d.total * 100) + '%' : '—', '在线 / 总计', 'k-purple')
      ].join('');
      var rows = d.items.map(function (r) {
        return [
          '<span class="mono">' + esc(r.machineId.slice(0, 16)) + '…</span>',
          '<span>' + esc(r.customerName || '—') + '</span><br>' +
            '<span class="mono muted">' + esc(r.licenseCode) + '</span>',
          '<span class="badge ' + (r.online ? 'b-online' : 'b-offline') + '">' +
            (r.online ? '在线' : '离线') + '</span>',
          '<span>' + esc(ago(r.lastSeen)) + '</span><br><span class="muted">' +
            esc(fmtTime(r.lastSeen)) + '</span>',
          esc(r.hostname || '—'),
          esc(r.appVersion || '—'),
          '<span class="num">' + num(r.actionsTotal) + '</span>'
        ];
      });
      $('instTable').innerHTML = rows.length
        ? tableHtml(['机器码', '客户 / 密钥', '状态', '最后心跳', '主机名', '版本', '累计动作'], rows, 6)
        : emptyBlock('还没有客户机接入', '客户机调用 /admin/api/activate 后会出现在这里');
    }).catch(function (e) { if (e.message !== 'unauthorized') toast(e.message, true); });
  }
  $('instQueryBtn').addEventListener('click', loadInstances);
  $('instSearch').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') loadInstances();
  });

  // ---------- 用量 ----------
  function loadUsage() {
    api('/usage?days=14').then(function (d) {
      chartData.usage = d.series;
      renderChart($('usageChart'), d.series, [
        { key: 'leads', color: 'primary' },
        { key: 'comments', color: 'green' },
        { key: 'dms', color: 'purple' },
        { key: 'logins', color: 'amber' }
      ]);
      var rows = (d.byLicense || []).map(function (r) {
        return [
          '<span>' + esc(r.customer_name || '未命名客户') + '</span><br>' +
            '<span class="mono muted">' + esc(r.license_code) + '</span>',
          '<span class="num">' + num(r.leads) + '</span>',
          '<span class="num">' + num(r.comments) + '</span>',
          '<span class="num">' + num(r.dms) + '</span>',
          '<span class="num">' + num(r.logins) + '</span>',
          '<span class="num">' + num(r.actions) + '</span>'
        ];
      });
      $('usageTable').innerHTML = rows.length
        ? tableHtml(['客户 / 密钥', '采集', '评论', '私信', '登录', '动作'], rows, 1)
        : emptyBlock('暂无用量记录', '客户机上报后按密钥汇总');
    }).catch(function (e) { if (e.message !== 'unauthorized') toast(e.message, true); });
  }

  // ---------- 审计 ----------
  function loadAudit() {
    var kw = $('auditSearch').value.trim();
    api('/audit?limit=200&keyword=' + encodeURIComponent(kw)).then(function (d) {
      var rows = d.items.map(function (r) {
        return [
          '<span class="muted">' + esc(fmtTime(r.at)) + '</span>',
          esc(r.action),
          '<span class="mono">' + esc(r.target || '—') + '</span>',
          '<span class="muted">' + esc(r.detail || '') + '</span>'
        ];
      });
      $('auditTable').innerHTML = rows.length
        ? tableHtml(['时间', '动作', '对象', '明细'], rows)
        : emptyBlock('暂无操作记录', '生成密钥、修改状态等动作都会留痕');
    }).catch(function (e) { if (e.message !== 'unauthorized') toast(e.message, true); });
  }
  $('auditQueryBtn').addEventListener('click', loadAudit);
  $('auditSearch').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') loadAudit();
  });

  // ---------- 改密 ----------
  $('pwBtn').addEventListener('click', function () {
    var oldPw = $('pwOld').value, n1 = $('pwNew').value, n2 = $('pwNew2').value;
    var msg = $('pwMsg');
    msg.hidden = false;
    if (n1 !== n2) { msg.textContent = '两次输入的新口令不一致'; return; }
    if (n1.length < 8) { msg.textContent = '新口令至少 8 位'; return; }
    api('/password', { method: 'POST', body: { oldPassword: oldPw, newPassword: n1 } })
      .then(function (d) {
        msg.textContent = d.message || '已更新';
        $('pwOld').value = $('pwNew').value = $('pwNew2').value = '';
        setTimeout(function () {
          sessionStorage.removeItem(TOKEN_KEY);
          showLogin('口令已更新，请重新登录');
        }, 1200);
      })
      .catch(function (e) { msg.textContent = e.message; });
  });

  // ---------- 时钟 ----------
  function tick() {
    var d = new Date();
    var p = function (x) { return x < 10 ? '0' + x : '' + x; };
    $('clock').textContent = d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }
  tick();
  setInterval(tick, 1000);

  // ---------- 启动 ----------
  if (token()) {
    api('/me').then(showApp).catch(function () { showLogin(); });
  } else {
    showLogin();
  }
})();
