import {liveDashboard, refreshLiveDashboard, stopLiveTask, dismissLiveTask} from "./LiveDashboardRuntime.js";
import {projectLiveCards, liveStatusText} from "./LiveDashboardModel.js";
import {dashboardGrid, dashboardPageZoom} from "./LiveDashboardLayout.js";
import {
  ch as on,
  ci as ls,
  cj as rs,
  ck as as,
  cl as cs,
  cm as us,
  A as G,
  y as oe,
  an as ds,
  aD as S,
  cn as te,
  co as ms,
  o as ue,
  cp as yt,
  cq as de,
  cr as fs,
  cs as Qe,
  x as Ae,
  z as vs,
  ct as Z,
  cu as d,
  cv as v,
  cw as i,
  i as g,
  aG as h,
  cx as f,
  cy as _,
  cz as C,
  at as D,
  cA as pe,
  cB as ne,
  cC as $,
  bt as x,
  cD as E,
  cE as hs,
  cF as wt,
  cG as ps,
  cH as _t,
  T as gs,
  cI as Qt,
  cJ as ys,
  cK as ws,
  cL as _s,
  cM as bs,
  cN as Ss,
  cO as Ts,
  cP as Ks,
  cQ as Be,
} from "./index-1pPBwPwk.js";
import { u as xs } from "./useWorkPublishRunner-C44TwGb-.js";
import { g as en } from "./browserViewHostBounds-B-s7AYXG.js";
import { A as et } from "./activity-Czll_gvU.js";
import { C as Ms } from "./chevron-right-Du46-gLo.js";
import { X as ge } from "./x-Dap4NVBp.js";
import { M as bt, D as Cs, a as Ls } from "./minimize-2-D6AfvBjM.js";
import { L as tn } from "./loader-circle-Cq8NLiNM.js";
import { M as St } from "./monitor-6144Sa4v.js";
/**
 * @license lucide-vue-next v0.395.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */ const As = on("ChevronLeftIcon", [
  ["path", { d: "m15 18-6-6 6-6", key: "1wnfg3" }],
]);
/**
 * @license lucide-vue-next v0.395.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */ const Rs = on("LayoutGridIcon", [
  ["rect", { width: "7", height: "7", x: "3", y: "3", rx: "1", key: "1g98yp" }],
  [
    "rect",
    { width: "7", height: "7", x: "14", y: "3", rx: "1", key: "6d4xhi" },
  ],
  [
    "rect",
    { width: "7", height: "7", x: "14", y: "14", rx: "1", key: "nxv5o0" },
  ],
  [
    "rect",
    { width: "7", height: "7", x: "3", y: "14", rx: "1", key: "1bb6yr" },
  ],
]);
function Is(Tt = {}) {
  return Object.entries(Tt).map(([k, F]) => [k, F.length, F[F.length - 1]]);
}
const ks = { class: "dashboard-view" },
  zs = { class: "progress-monitor-section" },
  Es = { key: 0, class: "progress-account-nav" },
  Ps = { class: "progress-nav-title" },
  Fs = { class: "progress-nav-controls" },
  $s = { class: "progress-nav-index" },
  Bs = { key: 0, class: "no-task-placeholder" },
  Vs = ["onClick"],
  Ns = { class: "acc-header" },
  Ds = { class: "acc-top" },
  Ws = { class: "acc-identity" },
  Os = ["title"],
  qs = ["title"],
  Ys = { class: "acc-side" },
  Gs = { class: "stop-btn-mark" },
  Hs = { class: "close-btn-mark" },
  Us = { class: "acc-metrics" },
  Xs = { key: 0, class: "metric-pill metric-pill--success" },
  js = { key: 1, class: "metric-pill metric-pill--info" },
  Zs = {
    class: "metric-pill metric-pill--success",
    title: "本次养号已确认并入库的新增点赞，已赞和跳过不计数",
  },
  Js = {
    class: "metric-pill metric-pill--success",
    title: "本次养号已确认并入库的新增收藏，已收藏和跳过不计数",
  },
  Qs = {
    class: "metric-pill metric-pill--success",
    title: "本次养号复制视频链接确认成功并入库的次数",
  },
  ei = { key: 3, class: "metric-pill metric-pill--info" },
  ti = { class: "metric-pill metric-pill--info", title: "累计完成的巡检轮次" },
  ni = {
    class: "metric-pill metric-pill--success",
    title: "监控抓取到的新评论数",
  },
  si = {
    class: "metric-pill metric-pill--warn",
    title: "命中关键词/规则的评论数",
  },
  ii = { key: 0, class: "metric-pill metric-pill--info" },
  oi = { key: 1, class: "metric-pill metric-pill--info" },
  li = { class: "metric-pill metric-pill--info" },
  ri = { class: "metric-pill metric-pill--success" },
  ai = { class: "metric-pill metric-pill--warn" },
  ci = ["title"],
  ui = {
    key: 0,
    class: "metric-pill metric-pill--warn",
    title: "识别为重复、未重复入库的数量",
  },
  di = ["title"],
  mi = {
    key: 0,
    class: "metric-pill metric-pill--warn",
    title:
      "任务累计互动计数：成功点赞、原评论回复、主页首作评论和视频主贴评论；关注、私信不计入此上限",
  },
  fi = {
    key: 1,
    class: "metric-pill metric-pill--info",
    title:
      "当前搜索词或当前视频入口的处理进度；切换搜索词/入口后会按新目标重新从 0 计数",
  },
  vi = ["title"],
  hi = { class: "terminal-bubble-header" },
  pi = {
    style: {
      display: "flex",
      "align-items": "center",
      overflow: "hidden",
      flex: "1",
    },
  },
  gi = ["title"],
  yi = { class: "terminal-header-actions" },
  wi = ["disabled", "onClick"],
  _i = { key: 0, class: "empty-terminal" },
  bi = { class: "time" },
  Si = ["title"],
  Ti = { class: "panel-content" },
  Ki = { class: "progress-info" },
  xi = { class: "info-main" },
  Mi = { class: "text-group" },
  Ci = { key: 0, class: "label" },
  Li = { key: 1, class: "label" },
  Ai = { class: "detail" },
  Ri = { class: "lead-name" },
  Ii = { key: 0, class: "action-tag" },
  ki = { class: "stats-group" },
  zi = { class: "mini-stat" },
  Ei = { class: "success" },
  Pi = { class: "mini-stat" },
  Fi = { class: "error" },
  $i = { class: "mini-stat" },
  Bi = { class: "progress-bar-container" },
  Vi = { class: "result-modal-header" },
  Ni = { class: "modal-stats" },
  Di = { class: "main-workspace" },
  Wi = { class: "workspace-layout-row" },
  Oi = { class: "canvas-section" },
  qi = { key: 0, class: "empty-preview" },
  Yi = { key: 1, class: "empty-preview entity-live-placeholder" },
  Gi = ["onMousedown"],
  Hi = ["onMousedown"],
  Ui = { class: "window-controls" },
  Xi = ["title", "onClick"],
  ji = { class: "window-title" },
  Zi = { class: "window-actions" },
  Ji = { class: "stop-btn-mark" },
  Qi = { class: "close-btn-mark" },
  eo = { class: "window-content" },
  to = { key: 0, class: "finished-session-overlay" },
  no = { class: "finished-reason" },
  so = { key: 0, class: "finished-summary" },
  io = ["onClick"],
  oo = { class: "suspended-title" },
  lo = { class: "suspended-desc" },
  ro = { key: 2, class: "loading-overlay" },
  ao = { key: 3, class: "suspended-telemetry-overlay" },
  co = { class: "overlay-content" },
  uo = ["onMousedown"],
  mo = { key: 0, class: "zoom-terminal-sidebar" },
  fo = { class: "sidebar-header" },
  vo = { class: "header-left" },
  ho = { class: "account-name" },
  po = { class: "header-actions" },
  go = { key: 0, class: "empty-terminal" },
  yo = { class: "time" },
  wo = ["title"],
  _o = ["onMousedown"],
  bo = ["onMousedown"],
  So = { class: "window-controls" },
  To = ["onClick"],
  Ko = ["onClick"],
  xo = ["onClick"],
  Mo = { class: "popup-title" },
  Co = { class: "popup-actions" },
  Lo = ["onMousedown"],
  nn = 40,
  sn = "dashboard_live_preview_scope",
  ko = "dashboard_light_live_mode",
  zo = 32,
  Eo = 3,
  Po = {
    __name: "Dashboard",
    setup(Tt) {
      const { on: k, send: F, invoke: ye } = rs(),
        m = as(),
        { stopEntityLeadgenTask: ln } = cs(),
        { stopMonitorTask: rn } = us(),
        { stopWorkPublishTask: an, syncWorkPublishRuntimeTasks: cn } = xs(),
        J = G(null),
        we = m.accountLogs,
        tt = G(null),
        Ve = G(null),
        Ne = Ae({}),
        se = G(null);
      let _e = 0;
      const B = G([]),
        Re = G([]),
        V = G([]),
        W = G([]),
        X = new Map(),
        j = new Map();
      let ie = 0,
        Q = 0;
      const De = new Set(),
        un = [120, 450, 1e3, 2200, 4200];
      function dn() {
        const e = localStorage.getItem(sn);
        return e === "all" || e === "current"
          ? e
          : localStorage.getItem(ko) === "false"
            ? "all"
            : "current";
      }
      const R = G(!1);
      let H = !1;
      const ae = G(dn()),
        mn = [
          { label: "看当前", value: "current" },
          { label: "看全部", value: "all" },
        ];
      let be = 0;
      const Kt = (e) =>
          e ? e.scrollHeight - e.scrollTop - e.clientHeight <= 40 : !1,
        xt = G(null),
        Mt = (e = !1) => {
          S(() => {
            const t = xt.value;
            t && (e || Kt(t)) && (t.scrollTop = t.scrollHeight);
          });
        },
        O = G(null),
        fn = (e) => {
          (O.value === e ? (O.value = null) : ((O.value = e), Mt(!0)),
            S(() => {
              N();
            }));
        },
        Ct = (e) => {
          m.clearAccountLogs(e);
        };
      async function vn(e) {
        var n;
        if ((n = navigator.clipboard) != null && n.writeText) {
          await navigator.clipboard.writeText(e);
          return;
        }
        const t = document.createElement("textarea");
        ((t.value = e),
          t.setAttribute("readonly", ""),
          (t.style.position = "fixed"),
          (t.style.left = "-9999px"),
          document.body.appendChild(t),
          t.select(),
          document.execCommand("copy"),
          document.body.removeChild(t));
      }
      async function Lt(e) {
        const card = b.value.find(card => card.id === e && card.dashboardKind);
        const t = card ? Wt(card) : Array.isArray(we[e]) ? we[e] : [];
        if (!t.length) {
          te.warning("当前窗口暂无可复制日志");
          return;
        }
        const n = nt(e),
          o =
            (n == null ? void 0 : n.nickname) ||
            (n == null ? void 0 : n.accountName) ||
            e ||
            "未知账号",
          a = t.map(
            (c) => `[${c.time || "--:--:--"}] ${String(c.msg || "").trim()}`,
          ),
          u = [
            `账号：${o}`,
            `导出时间：${new Date().toLocaleString("zh-CN", { hour12: !1 })}`,
            `日志条数：${a.length}`,
            "",
            ...a,
          ].join(`
`);
        try {
          (await vn(u), te.success(`已复制 ${a.length} 条运行轨迹`));
        } catch {
          te.error("复制失败，请展开轨迹后手动复制");
        }
      }
      const We = Ae({}),
        hn = (e, t) => {
          e ? (We[t] = e) : delete We[t];
        },
        pn = (e, t) => {
          const n = String(t);
          e ? (Ne[n] = e) : delete Ne[n];
        },
        gn = (e) => {
          ((se.value = e),
            (tt.value = tt.value === e ? null : e),
            S(() => {
              (N(), At(e, !0));
            }));
        },
        At = (e, t = !1) => {
          S(() => {
            const n = We[e];
            n && (t || Kt(n)) && (n.scrollTop = n.scrollHeight);
          });
        },
        yn = () => {
          be ||
            (be = requestAnimationFrame(() => {
              be = 0;
              for (const e in We) {
                const t = b.value.find((n) => n.id === e);
                (t && gt(t)) || At(e, !1);
              }
              O.value && Mt(!1);
            }));
        };
      oe(
        () => Is(m.accountLogs),
        () => {
          yn();
        },
        { immediate: !0 },
      );
      const nt = (e) => b.value.find((t) => t.id === e),
        Y = G(null),
        st = G(10),
        z = [];
      let le = null,
        it = null,
        Se = 0,
        Oe = null,
        qe = null;
      const Ie = G(!1),
        wn = [
          { title: "执行账号", dataIndex: "accountName", key: "accountName" },
          { title: "线索对象", dataIndex: "leadName", key: "leadName" },
          { title: "执行动作", dataIndex: "type", key: "type" },
          { title: "结果", dataIndex: "success", key: "success" },
          { title: "执行时间", dataIndex: "time", key: "time" },
        ],
        Rt = ps,
        re = Ae({}),
        q = Ae([]),
        Ye = {},
        _n = (e, t) => {
          e ? (Ye[t] = e) : delete Ye[t];
        },
        Ge = Ae({}),
        He = {},
        bn = (e, t) => {
          e ? (He[t] = e) : delete He[t];
        },
        Sn = (e) => {
          if (!e) return !0;
          const t = e.toLowerCase();
          return t.includes("dy") || t.includes("douyin") || t.includes("抖音");
        },
        It = (e) => (Sn(e) ? "DY" : String(e || "").toUpperCase());
      function ot(e) {
        const t = String(e || "")
          .trim()
          .toLowerCase();
        return !t || t === "dy" || t.includes("douyin") || t.includes("抖音")
          ? "douyin"
          : t;
      }
      function lt(e, t) {
        const n = ot(e);
        return !t || t === "default" ? n : `${n}_${t}`;
      }
      function ee(e = {}) {
        const t = String(e.viewKey || "").trim();
        if (
          t.startsWith("entity_") ||
          t.startsWith("monitor_") ||
          t.startsWith("publish_")
        )
          return t;
        if (e.taskKind === "work_publish")
          return `publish_${e.id || e.accountId}`;
        if (e.taskKind === "entity_leadgen") {
          const o = e.id || e.accountId;
          return o ? `entity_${o}` : t;
        }
        if (e.taskKind === "video_monitor") {
          const o = e.id || e.accountId;
          return o ? `monitor_${o}` : t;
        }
        const n = e.platform || String(e.viewKey || "").split("_")[0];
        return lt(n, e.id || e.accountId);
      }
      const T = (e) =>
          b.value.find(card => card.id === e && card.dashboardKind)?.displayStats ||
          (m.accountStats && m.accountStats[e]) || {
            videos: 0,
            likes: 0,
            replies: 0,
            follows: 0,
            messages: 0,
          },
        ce = (e) => T(e).monitorStats || {},
        rt = (e) => {
          var t, n;
          return _s(
            e.isSessionFinished &&
              (n = (t = e.progress) == null ? void 0 : t.nurture) != null &&
              n.actions
              ? e.progress.nurture.actions
              : T(e.id).nurtureActions,
          );
        },
        Ue = (e) => T(e).entityStats || {},
        kt = (e) => Number(T(e).entityMaxCollect || 0),
        Tn = (e) => bs(Ue(e), 2),
        Kn = (e) =>
          String(T(e).keyword || "").startsWith("搜索:")
            ? "本词视频"
            : "本入口视频";
      function at(e) {
        const t = String((e == null ? void 0 : e.name) || "").trim();
        if (t) return t;
        const n =
          (e == null ? void 0 : e.id) || (e == null ? void 0 : e.accountId);
        if (!n) return "";
        const o = m.accounts.find((a) => a.id === n);
        return String((o == null ? void 0 : o.name) || "").trim();
      }
      function Xe(e) {
        return (
          at(e) ||
          String((e == null ? void 0 : e.nickname) || "").trim() ||
          "未登录"
        );
      }
      function zt(e) {
        const t = at(e);
        if (!t) return !1;
        const n = String((e == null ? void 0 : e.nickname) || "").trim();
        return !!n && n !== t;
      }
      async function xn() {
        try {
          const e = await ye("get-batch-runtime-state");
          m.applyBatchRuntimeState(e);
        } catch (e) {
          console.warn(
            "[Dashboard] 同步批量任务运行状态失败:",
            (e == null ? void 0 : e.message) || e,
          );
        }
      }
      let appliedLiveFocus = 0;
      function focusDashboardTask() {
        const request = liveDashboard.focus;
        if (!request || request.requestedAt === appliedLiveFocus) return;
        const card = b.value.find(card => card.id === request.accountId && (!request.runId || card.runId === request.runId));
        if (!card) return;
        appliedLiveFocus = request.requestedAt;
        fe.delete(card.id);
        Y.value = card.id;
        se.value = card.id;
        B.value = [...new Set([...B.value, card.viewKey])];
        S(() => { pt("current"); $t(card.id, {behavior:"auto"}); });
      }
      oe(() => [liveDashboard.focus?.requestedAt, liveDashboard.revision], focusDashboardTask);
      ds(async () => {
        await refreshLiveDashboard();
        focusDashboardTask();
        var u, c, p;
        let e = 0;
        z.push(
          k("entity-leadgen-monitor-windows-changed", (l = []) => {
            (e++,
              (Re.value = Array.isArray(l) ? l : []),
              S(() => {
                (U(), L({ force: !0 }));
              }));
          }),
        );
        const t = e;
        (ye("get-entity-leadgen-monitor-view-keys")
          .then((l) => {
            t === e && (Re.value = Array.isArray(l) ? l : []);
          })
          .catch(() => {}),
          b.value.forEach((l, y) => Yt(l.id, y)),
          Ze(),
          S(() => {
            (U(), S(N));
          }),
          z.push(
            k("view-batch-status", ({ viewKey: l, isRunning: y }) => {
              (m.setBatchStatus(l, y),
                S(() => {
                  (U(), S(N));
                }));
            }),
          ),
          z.push(
            k("batch-task-info", ({ accountIds: l }) => {
              const y = new Set(l || []);
              (m.finishedTaskSnapshots
                .filter((M) => M.taskKind === "batch" && y.has(M.id))
                .forEach((M) => m.dismissFinishedTask(M.id)),
                S(() => mt()));
            }),
          ),
          z.push(
            k("batch-task-progress", (l) => {
              ((m.batchProgress.lastLeadName = l.lastLeadName || ""),
                (m.batchProgress.lastAction = l.lastAction || ""));
            }),
          ),
          z.push(
            k("batch-task-complete", () => {
              (te.success("一项批量跟进已完成"), S(() => mt()));
            }),
          ),
          z.push(
            k("batch-task-stopped", () => {
              (S(() => mt()), te.warning("批量任务已手动停止"));
            }),
          ),
          z.push(
            k("batch-account-finished", () => {
              S(() => {
                (U(), S(N));
              });
            }),
          ),
          z.push(
            k("new-log", (l) => {
              m.addLog(l);
            }),
          ),
          z.push(
            k("ui-zoom-changed", () => {
              me(0);
            }),
          ),
          z.push(
            k("automation-layout-refresh", () => {
              (X.clear(), j.forEach((l) => clearTimeout(l)), j.clear(), me());
            }),
          ),
          z.push(
            k("automation-live-preview-auto-closed", (l = {}) => {
              ((H = !1),
                (R.value = !1),
                (B.value = []),
                (V.value = []),
                (W.value = []),
                q.splice(0, q.length),
                X.clear(),
                j.forEach((M) => clearTimeout(M)),
                j.clear(),
                Fe(),
                F("set-views-visible", !1));
              const y = Math.max(1, Math.round(Number(l.delayMs || 3e4) / 1e3));
              te.info(
                `软件在后台超过 ${y} 秒，已自动关闭画面，任务仍在继续运行`,
              );
            }),
          ));
        function n(l = []) {
          const y = (Array.isArray(l) ? l : []).map(String).filter(Boolean);
          y.length &&
            ((B.value = y),
            (H = !0),
            (R.value = !0),
            S(() => {
              try {
                U();
              } catch {}
              requestAnimationFrame(() => {
                (L({ force: !0 }),
                  (H = !1),
                  requestAnimationFrame(() => {
                    R.value && L({ force: !0 });
                  }));
              });
            }));
        }
        function o(l = [], y = V) {
          const M = (Array.isArray(l) ? l : []).map(String).filter(Boolean);
          M.length &&
            ((y.value = M),
            (H = !0),
            (R.value = !0),
            S(() => {
              try {
                U();
              } catch {}
              requestAnimationFrame(() => {
                (L({ force: !0 }),
                  (H = !1),
                  requestAnimationFrame(() => {
                    R.value && L({ force: !0 });
                  }));
              });
            }));
        }
        (z.push(
          k("entity-leadgen-open-live-preview", (l = {}) => {
            n(l.viewKeys);
          }),
        ),
          z.push(
            k("entity-leadgen-close-live-preview", () => {
              ((B.value = []), b.value.length > 0 ? L({ force: !0 }) : $e());
            }),
          ),
          z.push(
            k("entity-leadgen-detach-live-preview", (l = {}) => {
              const y = new Set(l.viewKeys || []);
              ((B.value =
                l.accountIds == null ? [] : B.value.filter((M) => !y.has(M))),
                L({ force: !0 }));
            }),
          ),
          z.push(
            k("video-monitor-open-live-preview", (l = {}) => {
              o(l.viewKeys);
            }),
          ),
          z.push(
            k("video-monitor-close-live-preview", () => {
              ((V.value = []), b.value.length > 0 ? L({ force: !0 }) : $e());
            }),
          ),
          z.push(
            k("video-monitor-detach-live-preview", (l = {}) => {
              const y = new Set(l.viewKeys || []);
              ((V.value =
                l.accountIds == null ? [] : V.value.filter((M) => !y.has(M))),
                L({ force: !0 }));
            }),
          ),
          z.push(
            k("work-publish-open-live-preview", (l = {}) => {
              o(l.viewKeys, W);
            }),
          ));
        const a = (l = {}) => {
          const y = new Set(l.viewKeys || []);
          ((W.value = W.value.filter((M) => !y.has(M))), L({ force: !0 }));
        };
        (z.push(k("work-publish-close-live-preview", a)),
          z.push(k("work-publish-detach-live-preview", a)),
          z.push(
            k("work-publish-task-event", (l = {}) => {
              l.type === "view-ready" &&
                R.value &&
                o([...new Set([...W.value, l.viewKey])], W);
            }),
          ),
          await cn().catch(() => {}));
        try {
          const l = await ye("consume-work-publish-live-preview");
          (u = l == null ? void 0 : l.viewKeys) != null &&
            u.length &&
            o(l.viewKeys, W);
        } catch {}
        try {
          const l = await ye("consume-entity-leadgen-live-preview");
          (c = l == null ? void 0 : l.viewKeys) != null &&
            c.length &&
            n(l.viewKeys);
        } catch {}
        try {
          const l = await ye("get-monitor-live-views");
          ms(m.monitorRunningTasks, l);
        } catch {}
        try {
          const l = await ye("consume-video-monitor-live-preview");
          (p = l == null ? void 0 : l.viewKeys) != null &&
            p.length &&
            o(l.viewKeys);
        } catch {}
        (await xn(),
          Gt(J.value),
          window.addEventListener("resize", Pt),
          window.addEventListener("radar-security-challenge", Et),
          L());
      });
      function Et(e) {
        var o;
        const t =
          (o = e == null ? void 0 : e.detail) == null ? void 0 : o.accountId;
        if (!t) return;
        const n = b.value.find((a) => a.id === t || a.accountId === t);
        !n ||
          n.isSessionFinished ||
          ((Y.value = n.id), R.value ? Ce(n.id) : pt("current"));
      }
      const Pt = () => {
        Pe() && me();
      };
      oe(Ie, (e) => {
        e ? F("set-views-visible", !1) : L({ force: !0 });
      });
      function Te(e) {
        return q.find((t) => t.taskId === e);
      }
      function ct(e) {
        var n, o;
        if (!e) return null;
        const t =
          ((n = e.querySelector) == null
            ? void 0
            : n.call(e, ".window-content")) ||
          ((o = e.querySelector) == null ? void 0 : o.call(e, ".popup-body")) ||
          e;
        return en(t, { minWidth: 10, minHeight: 10 });
      }
      function ut(e) {
        return dashboardPageZoom(e);
      }
      function dt(e, t, { force: n = !1, refresh: o = !1 } = {}) {
        if (!t || t.width < 10 || t.height < 10) return;
        const a = ut(t),
          u = X.get(e);
        if (
          !n &&
          !o &&
          u &&
          Math.abs(u.bounds.x - t.x) <= 1 &&
          Math.abs(u.bounds.y - t.y) <= 1 &&
          Math.abs(u.bounds.width - t.width) <= 1 &&
          Math.abs(u.bounds.height - t.height) <= 1 &&
          Math.abs(u.zoomFactor - a) < 0.001
        )
          return;
        const p = {
            viewKey: e,
            bounds: { ...t },
            zoomFactor: a,
            light: o ? !1 : Ee.value,
            force: n || o,
          },
          l = Date.now(),
          y = (u == null ? void 0 : u.sentAt) || 0,
          M = () => {
            var P;
            if ((j.delete(e), !Pe())) {
              X.delete(e);
              return;
            }
            const A = ((P = X.get(e)) == null ? void 0 : P.pending) || p;
            (X.set(e, {
              bounds: { ...A.bounds },
              zoomFactor: A.zoomFactor,
              sentAt: Date.now(),
              pending: null,
            }),
              F("update-automation-bounds", A));
          };
        if (l - y >= nn) {
          (X.set(e, {
            bounds: { ...t },
            zoomFactor: a,
            sentAt: l,
            pending: null,
          }),
            F("update-automation-bounds", p));
          return;
        }
        (X.set(e, {
          bounds: (u == null ? void 0 : u.bounds) || { ...t },
          zoomFactor: (u == null ? void 0 : u.zoomFactor) || a,
          sentAt: y,
          pending: p,
        }),
          j.has(e) || j.set(e, setTimeout(M, nn - (l - y))));
      }
      function me(e = 0) {
        const t = () => {
          ((Q = 0),
            (ie = 0),
            S(() => {
              (N(), q.forEach(Le));
            }));
        };
        if ((Q && (clearTimeout(Q), (Q = 0)), e > 0)) Q = setTimeout(t, e);
        else {
          if (ie) return;
          ie = requestAnimationFrame(() => {
            ie = requestAnimationFrame(t);
          });
        }
      }
      function mt() {
        me();
      }
      function ke(e) {
        (F("set-views-visible", !1),
          e({
            afterClose: () => {
              try {
                L({ force: !0 });
              } catch {}
            },
          }));
      }
      const Mn = () => {
        ke(({ afterClose: e }) => {
          Be.confirm({
            title: "确定停止所有批量任务？",
            content:
              "停止后将立即取消当前页面动作、批量间隙和剩余队列；已经成功提交的平台动作不会撤回。各账号页面与日志会保留。",
            okText: "停止任务",
            okType: "danger",
            cancelText: "取消",
            afterClose: e,
            onOk: () => {
              F("stop-batch-action");
            },
          });
        });
      };
      function ze(e) {
        if (!e || e.isSessionFinished) return !1;
        if (e.dashboardKind) return e.canStop === true;
        const t = ee(e);
        return m.batchRunningStates.get(t)
          ? !0
          : e.taskKind === "batch"
            ? !1
            : e.taskKind === "entity_leadgen"
              ? m.entityLeadgenRunningTasks.some(
                  (n) => String(n.id) === String(e.id),
                )
              : e.taskKind === "work_publish"
                ? m.workPublishRunningTasks.some(
                    (n) => String(n.id) === String(e.id),
                  )
                : e.taskKind === "video_monitor"
                  ? m.monitorRunningTasks.some(
                      (n) => String(n.id) === String(e.id),
                    )
                  : e.taskKind === "nurture"
                    ? m.nurtureRunningTasks.some((n) => n.id === e.id)
                    : m.runningTasks.some((n) => n.id === e.id);
      }
      function ft(e) {
        if (e.dashboardKind) {
          ke(({afterClose}) => Be.confirm({title:"停止本次评论点赞？",
            content:"将停止本次运行；已提交且结果待核验的动作不会重复提交。",
            okText:"停止任务",cancelText:"取消",okType:"danger",afterClose,
            onOk:async()=>{await stopLiveTask(e);te.info("已请求停止，等待执行器确认");}}));
          return;
        }
        if ((e == null ? void 0 : e.taskKind) === "work_publish") {
          ke(({ afterClose: t }) => {
            Be.confirm({
              title: "停止该发布任务？",
              content:
                "将停止该发布任务内所有账号；已经提交的作品请在创作者中心确认结果。",
              okText: "停止",
              okType: "danger",
              cancelText: "取消",
              afterClose: t,
              onOk: () => an(e.workPublishTaskId),
            });
          });
          return;
        }
        if ((e == null ? void 0 : e.taskKind) === "entity_leadgen") {
          ke(({ afterClose: t }) => {
            Be.confirm({
              title: `停止该${de(e)}？`,
              content: `确认后将结束该${de(e)}任务。`,
              okText: "停止",
              okType: "danger",
              cancelText: "取消",
              afterClose: t,
              onOk: async () => {
                await Cn(e);
              },
            });
          });
          return;
        }
        if ((e == null ? void 0 : e.taskKind) === "video_monitor") {
          ke(({ afterClose: t }) => {
            Be.confirm({
              title: "停止该监控任务？",
              content: "确认后将立即结束该监控任务（不只是收起画面）。",
              okText: "停止",
              okType: "danger",
              cancelText: "取消",
              afterClose: t,
              onOk: async () => {
                await Ln(e);
              },
            });
          });
          return;
        }
        ke(({ afterClose: t }) => {
          Be.confirm({
            title: "停止该账号任务？",
            content: "仅停止当前账号并关闭对应页面，其他账号继续运行。",
            okText: "停止",
            okType: "danger",
            cancelText: "取消",
            afterClose: t,
            onOk: () => An(e),
          });
        });
      }
      async function Cn(e) {
        const t = e == null ? void 0 : e.entityLeadgenTaskId;
        if (!t) {
          Me(e);
          return;
        }
        const n = m.entityLeadgenRunningTasks.filter(
            (a) => String(a.entityLeadgenTaskId) === String(t),
          ),
          o = await ln(t, e);
        (n.forEach((a) => {
          (fe.delete(a.id),
            m.markTaskFinished({
              accountId: a.id,
              taskId: t,
              reason: "manual_stop",
              viewCloseReason: "manual_stop",
              taskKind: "entity_leadgen",
            }));
        }),
          fe.delete(e.id),
          (B.value = []),
          (H = !1),
          L({ force: !0 }),
          o && te.success(`${de(e)}已停止`));
      }
      async function Ln(e) {
        const t = e == null ? void 0 : e.monitorTaskId;
        if (!t) {
          Me(e);
          return;
        }
        const n = m.monitorRunningTasks.filter(
            (a) => String(a.monitorTaskId) === String(t),
          ),
          o = await rn(t, e);
        (n.forEach((a) => {
          (fe.delete(a.id),
            m.markTaskFinished({
              accountId: a.id,
              taskId: t,
              reason: "manual_stop",
              viewCloseReason: "manual_stop",
              taskKind: "video_monitor",
            }));
        }),
          fe.delete(e.id),
          (V.value = []),
          (H = !1),
          L({ force: !0 }),
          o && te.success("监控任务已停止"));
      }
      function An(e) {
        const t = ee(e);
        if (m.batchRunningStates.get(t) || e.taskKind === "batch") {
          F("stop-batch-account", { viewKey: t, reason: "manual_stop" });
          return;
        }
        F("stop-task", {
          viewKey: t,
          reason: "manual_stop",
          source: "user_action",
          requestedAt: Date.now(),
        });
      }
      function Rn() {
        return m.accounts
          .map((e) => {
            const t = ot(e.platform);
            return {
              id: e.id,
              platform: t,
              name: e.name,
              nickname: e.nickname,
              viewKey: lt(t, e.id),
              taskKind: "batch",
            };
          })
          .filter((e) => m.batchRunningStates.get(e.viewKey));
      }
      const b = ue(() => {
          const e = (r) => {
              const K = r.id || r.accountId,
                s = K ? m.accounts.find((he) => he.id === K) : null,
                w =
                  r.taskKind === "entity_leadgen" ||
                  String(r.viewKey || "").startsWith("entity_"),
                ve =
                  r.taskKind === "video_monitor" ||
                  String(r.viewKey || "").startsWith("monitor_");
              return {
                ...r,
                name: r.name || (s == null ? void 0 : s.name) || "",
                nickname: r.nickname || (s == null ? void 0 : s.nickname) || "",
                platform: ot(r.platform || (s == null ? void 0 : s.platform)),
                viewKey:
                  r.taskKind === "work_publish"
                    ? `publish_${K}`
                    : w
                      ? String(r.viewKey || "").startsWith("entity_")
                        ? r.viewKey
                        : K
                          ? `entity_${K}`
                          : r.viewKey
                      : ve
                        ? String(r.viewKey || "").startsWith("monitor_")
                          ? r.viewKey
                          : K
                            ? `monitor_${K}`
                            : r.viewKey
                        : lt(
                            r.platform || (s == null ? void 0 : s.platform),
                            K,
                          ),
              };
            },
            t = m.runningTasks.map((r) => ({
              ...e(r),
              taskKind: r.taskKind || "leadgen",
            })),
            n = m.nurtureRunningTasks.map((r) => ({
              ...e(r),
              taskKind: "nurture",
            })),
            o = m.entityLeadgenRunningTasks
              .filter((r) => yt(r, Re.value))
              .map((r) => {
                var ve;
                const K = r.id || r.accountId,
                  w =
                    (B.value || []).find(
                      (he) =>
                        he === `entity_${K}` ||
                        (String(K).includes(":") &&
                          String(he).startsWith("entity_guest_")),
                    ) ||
                    (String(K).includes(":") ? "" : `entity_${K}`) ||
                    `entity_${K}`;
                return {
                  id: K,
                  platform: "douyin",
                  name: r.name || de(r),
                  nickname: r.nickname || r.name || de(r),
                  sourceTypes:
                    r.sourceTypes ||
                    ((ve = r.configSnapshot) == null
                      ? void 0
                      : ve.sourceTypes) ||
                    [],
                  viewKey: w,
                  taskKind: "entity_leadgen",
                  entityLeadgenTaskId: r.entityLeadgenTaskId,
                };
              })
              .filter((r) => r.viewKey),
            a = new Set(o.map((r) => r.viewKey).filter(Boolean)),
            u =
              m.entityLeadgenRunningTasks.length > 0
                ? (B.value || [])
                    .filter((r) => !a.has(r))
                    .filter((r) =>
                      m.entityLeadgenRunningTasks.some((K) => {
                        if (!yt(K, Re.value)) return !1;
                        const s = String(K.id || K.accountId || "");
                        return s
                          ? r === `entity_${s}` ||
                              (s.includes(":") &&
                                String(r).startsWith("entity_guest_"))
                          : !1;
                      }),
                    )
                    .map((r) => ({
                      id: `entity-preview:${r}`,
                      platform: "douyin",
                      name: "线索采集",
                      nickname: "线索采集",
                      viewKey: r,
                      taskKind: "entity_leadgen",
                    }))
                : [],
            c = m.monitorRunningTasks
              .map((r) => {
                const K = r.id || r.accountId,
                  s = fs(r);
                return {
                  id: K,
                  platform: "douyin",
                  name: r.name || "监控任务",
                  nickname: r.nickname || r.name || "监控任务",
                  viewKey: s,
                  taskKind: "video_monitor",
                  monitorTaskId: r.monitorTaskId,
                };
              })
              .filter((r) => r.viewKey),
            p = m.workPublishRunningTasks.map(e),
            l = m.finishedTaskSnapshots
              .filter((r) => r.taskKind !== "batch")
              .map((r) => ({ ...e(r), isSessionFinished: !0 })),
            y = new Map();
          (l.forEach((r) => y.set(r.id, r)),
            [...t, ...n, ...o, ...u, ...c, ...p].forEach((r) =>
              y.set(r.id, r),
            ));
          const M = Rn();
          M.forEach((r) => {
            const K = y.get(r.id);
            (!K || K.isSessionFinished) && y.set(r.id, r);
          });
          const occupied = new Set([...y.values()].filter(card => !card.isSessionFinished).map(card => card.id));
          projectLiveCards(liveDashboard.items, occupied, liveDashboard.dismissed).forEach(card => y.set(card.id, card));
          let A = Array.from(y.values());
          const P = new Set();
          if (
            (t.forEach((r) => {
              r.viewKey && P.add(r.viewKey);
            }),
            n.forEach((r) => {
              r.viewKey && P.add(r.viewKey);
            }),
            o.forEach((r) => {
              r.viewKey && P.add(r.viewKey);
            }),
            u.forEach((r) => {
              r.viewKey && P.add(r.viewKey);
            }),
            p.forEach((r) => {
              r.viewKey && P.add(r.viewKey);
            }),
            c.forEach((r) => {
              r.viewKey && P.add(r.viewKey);
            }),
            M.forEach((r) => P.add(r.viewKey)),
            P.size === 1)
          ) {
            const r = Array.from(P)[0],
              K = o.length > 0 || u.length > 0,
              s = c.length > 0;
            A = A.filter((w) =>
              w.viewKey === r
                ? !0
                : (K &&
                      w.isSessionFinished &&
                      w.taskKind === "entity_leadgen") ||
                    (s && w.isSessionFinished && w.taskKind === "video_monitor")
                  ? !1
                  : !!w.isSessionFinished,
            );
          }
          return (
            (A = A.filter((r) => {
              if (r.taskKind === "entity_leadgen") {
                const K = m.entityLeadgenRunningTasks.find(
                  (s) => String(s.id || s.accountId) === String(r.id) && Qe(s),
                );
                if (K && !yt(K, Re.value)) return !1;
              }
              return ze(r) ? (fe.delete(r.id), !0) : !fe.has(r.id);
            })),
            A
          );
        }),
        Ke = ue(() => b.value.filter((e) => !e.isSessionFinished || e.dashboardKind && liveDashboard.frames[e.runId])),
        In = ue(() =>
          b.value.map((e, t) => {
            const n = Xe(e),
              o = zt(e) ? ` · ${e.nickname}` : "",
              a =
                {
                  nurture: "养号",
                  video_monitor: "监控",
                  work_publish: "发布",
                  entity_leadgen: de(e),
                  batch: "批量",
                }[e.taskKind] || "评论获客",
              u = e.dashboardKind ? liveStatusText(e) : e.isSessionFinished
                ? "已结束"
                : T(e.id).status === "finished"
                  ? "已完成"
                  : "运行中";
            return { value: e.id, label: `${t + 1}. ${n}${o} · ${a} · ${u}` };
          }),
        ),
        xe = ue(() =>
          b.value.findIndex((e) => String(e.id) === String(se.value)),
        );
      function Ft() {
        if (!b.value.length) {
          se.value = null;
          return;
        }
        if (xe.value >= 0) return;
        const e = b.value.find((t) => String(t.id) === String(Y.value));
        se.value = (e == null ? void 0 : e.id) ?? b.value[0].id;
      }
      function vt(e, t = "smooth") {
        const n = Ve.value,
          o = Ne[String(e)];
        if (!n || !o) return;
        const a =
          o.offsetLeft - Math.max(0, (n.clientWidth - o.offsetWidth) / 2);
        n.scrollTo({ left: Math.max(0, a), behavior: t });
      }
      function $t(e, { focusLive: t = !0, behavior: n = "smooth" } = {}) {
        const o = b.value.find((a) => String(a.id) === String(e));
        o && ((se.value = o.id), S(() => vt(o.id, n)), t && Ce(o.id));
      }
      function Bt(e) {
        Ft();
        const t = Math.max(
            0,
            Math.min(b.value.length - 1, xe.value + Number(e || 0)),
          ),
          n = b.value[t];
        n && $t(n.id);
      }
      function kn() {
        _e = 0;
        const e = Ve.value;
        if (!e || !b.value.length) return;
        const t = e.scrollLeft + e.clientWidth / 2;
        let n = null,
          o = Number.POSITIVE_INFINITY;
        for (const a of b.value) {
          const u = Ne[String(a.id)];
          if (!u) continue;
          const c = u.offsetLeft + u.offsetWidth / 2,
            p = Math.abs(c - t);
          p < o && ((n = a), (o = p));
        }
        n && (se.value = n.id);
      }
      function Vt() {
        _e || (_e = requestAnimationFrame(kn));
      }
      function zn(e) {
        const t = Ve.value;
        !t ||
          t.scrollWidth <= t.clientWidth ||
          Math.abs(e.deltaX) >= Math.abs(e.deltaY) ||
          Math.abs(e.deltaY) < 1 ||
          (e.preventDefault(), (t.scrollLeft += e.deltaY), Vt());
      }
      const Ee = ue(
          () => R.value && ae.value === "current" && Ke.value.length > 1,
        ),
        Nt = ue(() => {
          const e = Ke.value;
          if (!Ee.value) return new Set(e.map((o) => o.id));
          const t = new Set();
          return (
            Y.value && e.some((o) => o.id === Y.value)
              ? t.add(Y.value)
              : e[0] && t.add(e[0].id),
            q.forEach((o) => {
              e.some((a) => a.id === o.taskId) && t.add(o.taskId);
            }),
            t
          );
        }),
        En = ue(() =>
          b.value
            .map((e) => `${ee(e)}:${e.isSessionFinished ? 1 : 0}`)
            .join("|"),
        ),
        Pn = ue(() => [...Nt.value].map(String).sort().join("|"));
      function Pe() {
        return R.value || q.length > 0;
      }
      function Dt() {
        localStorage.setItem(sn, ae.value);
      }
      function Fe() {
        (De.forEach((e) => clearTimeout(e)), De.clear());
      }
      function Fn() {
        (ie && (cancelAnimationFrame(ie), (ie = 0)),
          Q && (clearTimeout(Q), (Q = 0)),
          j.forEach((e) => clearTimeout(e)),
          j.clear(),
          Fe(),
          X.clear());
      }
      function $n() {
        (Fe(),
          un.forEach((e) => {
            const t = setTimeout(() => {
              (De.delete(t),
                Pe() &&
                  S(() => {
                    Pe() && ht({ force: !1 });
                  }));
            }, e);
            De.add(t);
          }));
      }
      function L({ force: e = !1 } = {}) {
        if ((Fe(), !Pe())) {
          F("set-views-visible", !1);
          return;
        }
        const t = Ht();
        (is(!0, { light: t || Ee.value }),
          S(() => {
            (ht({ force: e || t }), $n());
          }));
      }
      function ht({ force: e = !1 } = {}) {
        (X.clear(),
          j.forEach((t) => clearTimeout(t)),
          j.clear(),
          N({ force: e, refresh: e }),
          q.forEach(Le));
      }
      function pt(e = ae.value) {
        ((H = !0),
          (e === "all" || e === "current") && ((ae.value = e), Dt()),
          (R.value = !0),
          Ze(),
          X.clear(),
          S(() => {
            if (!R.value) {
              H = !1;
              return;
            }
            requestAnimationFrame(() => {
              requestAnimationFrame(() => {
                if (!R.value) {
                  H = !1;
                  return;
                }
                (L({ force: !0 }), (H = !1));
              });
            });
          }));
      }
      function $e() {
        ((H = !1),
          (R.value = !1),
          (B.value = []),
          (V.value = []),
          (W.value = []),
          Fn(),
          L());
      }
      function Bn(e) {
        if (!(!e || e.isSessionFinished)) {
          if (!R.value) {
            ((Y.value = e.id), pt("current"));
            return;
          }
          Ce(e.id);
        }
      }
      function Vn(e) {
        return !R.value && !Te(e.id) ? "画面已关闭" : "后台运行中";
      }
      function Nn(e) {
        return !R.value && !Te(e.id)
          ? "任务在后台正常执行，点击打开画面"
          : "画面已隐藏以省内存，点一下切换查看";
      }
      function je(e) {
        if (!e || e.isSessionFinished) return !1;
        if (Te(e.id)) return !0;
        if (!R.value) return !1;
        const t = ee(e);
        return (B.value || []).includes(t) ||
          (V.value || []).includes(t) ||
          W.value.includes(t) ||
          ae.value === "all"
          ? !0
          : Nt.value.has(e.id);
      }
      function gt(e) {
        return !R.value && !Te(e.id) ? !0 : Ee.value && !je(e);
      }
      function Wt(e) {
        const t = e?.dashboardKind ? (e.logs || []).map(line => ({...line, msg:line.message,
          time:new Date(line.ts).toLocaleTimeString("zh-CN",{hour12:false})})) : we[e == null ? void 0 : e.id] || [],
          n = gt(e) ? Eo : zo;
        return t.slice(-n);
      }
      function Ot(e) {
        return (we[e] || []).slice(-80);
      }
      function Ze() {
        if (!Ke.value.length) {
          Y.value = null;
          return;
        }
        (Y.value && Ke.value.some((e) => e.id === Y.value)) ||
          (Y.value = Ke.value[0].id);
      }
      const fe = Ae(new Set()),
        Me = (e) => {
          if (!e) return;
          if (e.dashboardKind) dismissLiveTask(e);
          const t = ee(e);
          if (
            ((W.value = W.value.filter((n) => n !== t)),
            t &&
              F("release-automation-view", {
                viewKey: t,
                reason: "close_idle_card",
              }),
            fe.add(e.id),
            m.dismissFinishedTask(e.id),
            q.some((n) => n.taskId === e.id) && Je(e.id),
            O.value === e.id && (O.value = null),
            e.taskKind === "entity_leadgen")
          ) {
            ((B.value = (B.value || []).filter((n) => n !== t)),
              !B.value.length && !V.value.length && !W.value.length
                ? $e()
                : L({ force: !0 }),
              te.success(`已关闭该${de(e)}卡片与画面，相关内存已释放`));
            return;
          }
          if (e.taskKind === "video_monitor") {
            ((V.value = (V.value || []).filter((n) => n !== t)),
              !B.value.length && !V.value.length && !W.value.length
                ? $e()
                : L({ force: !0 }),
              te.success("已关闭该监控卡片与画面，相关内存已释放"));
            return;
          }
          (te.success(`已关闭 [${Xe(e)}] 的画面与卡片`), S(N));
        },
        Dn = (e) => {
          var t, n, o, a, u, c, p, l;
          if (e?.dashboardKind) return `${liveStatusText(e)}：${e.phase || "本次运行已结束"}`;
          return e != null && e.isSessionFinished
            ? Ts(e.finishReason || e.viewCloseReason, {
                progress: e.progress,
                videoDone:
                  (n = (t = e.progress) == null ? void 0 : t.videos) == null
                    ? void 0
                    : n.done,
                videoPlanned:
                  (a = (o = e.progress) == null ? void 0 : o.videos) == null
                    ? void 0
                    : a.planned,
                interactionDone:
                  (c = (u = e.progress) == null ? void 0 : u.interactions) ==
                  null
                    ? void 0
                    : c.done,
                interactionPlanned:
                  (l = (p = e.progress) == null ? void 0 : p.interactions) ==
                  null
                    ? void 0
                    : l.planned,
              })
            : "";
        },
        qt = (e) =>
          !(e != null && e.isSessionFinished) ||
          (e.taskKind && e.taskKind !== "leadgen")
            ? ""
            : e.runSummaryCompact
              ? e.runSummaryCompact
              : Ks(
                  e.finishReason || e.viewCloseReason,
                  { progress: e.progress },
                  T(e.id),
                ),
        Wn = (e) =>
          Ss(
            (e == null ? void 0 : e.finishReason) ||
              (e == null ? void 0 : e.viewCloseReason),
            { progress: e == null ? void 0 : e.progress },
            T(e == null ? void 0 : e.id),
          ),
        On = (e) => {
          const t = b.value.find((n) => n.id === e);
          t
            ? Me(t)
            : (m.dismissFinishedTask(e),
              O.value === e && (O.value = null),
              S(N));
        },
        qn = (e) => (e === "douyin" ? ys : wt),
        Yn = (e) => {
          if (!J.value) return { w: 200, h: 356 };
          const cell = dashboardGrid(J.value.getBoundingClientRect(), e)[0];
          return cell ? {w: cell.width, h: cell.height} : {w:200,h:356};
        },
        Yt = (e, t) => {
          if (re[e]) return;
          const n = b.value.length,
            { w: o, h: a } = Yn(n),
            u = n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4,
            c = 6,
            p = t % u,
            l = Math.floor(t / u);
          re[e] = {
            x: c + p * (o + c),
            y: c + l * (a + c),
            w: o,
            h: a,
            z: st.value++,
          };
        },
        Gn = (e) => {
          const t = re[e];
          return t
            ? {
                transform: `translate(${t.x}px, ${t.y}px)`,
                width: `${t.w}px`,
                height: `${t.h}px`,
                zIndex: t.z,
              }
            : {};
        },
        Ce = (e) => {
          const t = Y.value !== e;
          (t && re[e] && (re[e].z = st.value++), (Y.value = e));
          const n = b.value.find((o) => o.id === e);
          n &&
            !n.isSessionFinished &&
            (R.value && Ee.value
              ? L({ force: !0 })
              : R.value && t && S(() => ht({ force: !0 })),
            je(n) &&
              F("focus-automation-view", { viewKey: ee(n), bringToFront: !0 }));
        },
        Hn = (e, t) => {
          Ce(t);
          const n = re[t],
            o = e.clientX - n.x,
            a = e.clientY - n.y,
            u = (p) => {
              if (!J.value) return;
              const l = J.value.getBoundingClientRect();
              ((n.x = Math.max(0, Math.min(p.clientX - o, l.width - n.w))),
                (n.y = Math.max(0, Math.min(p.clientY - a, l.height - n.h))),
                N());
            },
            c = () => {
              (document.removeEventListener("mousemove", u),
                document.removeEventListener("mouseup", c));
            };
          (document.addEventListener("mousemove", u),
            document.addEventListener("mouseup", c));
        },
        Un = (e, t) => {
          Ce(t);
          const n = re[t],
            o = n.w,
            a = e.clientX,
            u = (p) => {
              if (!J.value) return;
              const l = J.value.getBoundingClientRect();
              let y = Math.max(140, o + (p.clientX - a));
              (n.x + y > l.width && (y = l.width - n.x),
                (n.w = y),
                (n.h = Math.round(y * (16 / 9))),
                N());
            },
            c = () => {
              (document.removeEventListener("mousemove", u),
                document.removeEventListener("mouseup", c));
            };
          (document.addEventListener("mousemove", u),
            document.addEventListener("mouseup", c));
        },
        U = () => {
          if (!J.value) return;
          const accounts = b.value.filter(card => !card.isSessionFinished ||
            (card.dashboardKind && liveDashboard.frames[card.runId]));
          const area=J.value.getBoundingClientRect();
          const cells = dashboardGrid({width:area.width,height:area.height}, accounts.length);
          accounts.forEach((card,index) => {
            const layout=re[card.id],cell=cells[index];
            if(layout&&cell)Object.assign(layout,{x:cell.x,y:cell.y,w:cell.width,h:cell.height});
          });
          S(N);
        };
      function Xn(e) {
        const t = Math.max(0, Number(e == null ? void 0 : e.width) || 0),
          n = Math.max(0, Number(e == null ? void 0 : e.height) || 0);
        if (t < 20 || n < 20) return;
        const o = qe;
        if (
          ((qe = { width: t, height: n }),
          !o ||
            o.width < 20 ||
            o.height < 20 ||
            (Math.abs(o.width - t) < 1 && Math.abs(o.height - n) < 1))
        )
          return;
        const a = t / o.width,
          u = n / o.height;
        Object.values(re).forEach((c) => {
          if (!c) return;
          const p = Math.min(140, t),
            l = Math.min(120, n);
          ((c.x = Math.max(
            0,
            Math.min(Math.round(c.x * a), Math.max(0, t - p)),
          )),
            (c.y = Math.max(
              0,
              Math.min(Math.round(c.y * u), Math.max(0, n - l)),
            )),
            (c.w = Math.max(p, Math.min(Math.round(c.w * a), t - c.x))),
            (c.h = Math.max(l, Math.min(Math.round(c.h * u), n - c.y))));
        });
      }
      function jn(e) {
        ((Oe = e),
          !Se &&
            (Se = requestAnimationFrame(() => {
              Se = 0;
              const t = Oe;
              ((Oe = null), Xn(t), me());
            })));
      }
      function Gt(e) {
        e !== it &&
          (le == null || le.disconnect(),
          (le = null),
          (it = e || null),
          (qe = null),
          e &&
            ((le = new ResizeObserver((t) => {
              const n = t.find((a) => a.target === e) || t[0],
                o =
                  (n == null ? void 0 : n.contentRect) ||
                  e.getBoundingClientRect();
              jn({ width: o.width, height: o.height });
            })),
            le.observe(e),
            S(() => {
              (U(), S(() => N({ force: !0, refresh: !0 })));
            })));
      }
      oe(J, (e) => Gt(e), { flush: "post" });
      oe(
        () => {
          const t = b.value.find((u) => !u.isSessionFinished);
          return t ? t.id : "";
        },
        (e) => {
          if (e && !O.value)
            ((O.value = e), Mt(!0), S(() => N()));
        },
        { immediate: !0 }
      );
      function Ht() {
        return (
          liveDashboard.items.some(item => !item.isSessionFinished) ||
          m.runningTasks.length > 0 ||
          m.nurtureRunningTasks.length > 0 ||
          m.entityLeadgenRunningTasks.length > 0 ||
          m.monitorRunningTasks.length > 0 ||
          m.workPublishRunningTasks.length > 0 ||
          m.isBatchActive ||
          Array.from(m.batchRunningStates.values()).some(Boolean)
        );
      }
      const Le = (e) => {
          S(() => {
            const t = Ye[e.taskId];
            if (!t) return;
            const n = ct(t),
              o = b.value.find((u) => u.id === e.taskId);
            if (!o || o.isSessionFinished || !n) return;
            const a = ee(o);
            (dt(a, n),
              F("focus-automation-view", { viewKey: a, bringToFront: !0 }));
          });
        },
        Zn = (e) => {
          const t = q.find((n) => n.taskId === e);
          t && ((t.z = 9e3 + st.value++), Le(t));
        },
        Jn = (e) => {
          if (q.find((n) => n.taskId === e.id)) return;
          const t = {
            taskId: e.id,
            taskName: e.name,
            nickname: e.nickname,
            platform: e.platform,
            fullscreen: !1,
            x: 120 + q.length * 30,
            y: 80 + q.length * 30,
            w: 720,
            h: 520,
            z: 9e3 + q.length,
          };
          (q.push(t),
            S(() => {
              (L({ force: !0 }), Le(t));
            }));
        },
        Je = (e) => {
          const t = q.findIndex((n) => n.taskId === e);
          (t !== -1 && q.splice(t, 1), L({ force: !0 }), me(80));
        },
        Qn = (e) => {
          Je(e);
        },
        Ut = (e) => {
          const t = q.find((n) => n.taskId === e);
          t && ((t.fullscreen = !t.fullscreen), me(50));
        },
        es = (e) =>
          e.fullscreen
            ? {
                top: 0,
                left: 0,
                width: "100vw",
                height: "100vh",
                zIndex: e.z + 100,
              }
            : {
                left: `${e.x}px`,
                top: `${e.y}px`,
                width: `${e.w}px`,
                height: `${e.h}px`,
                zIndex: e.z,
              },
        ts = (e, t) => {
          if (t.fullscreen) return;
          const n = e.clientX - t.x,
            o = e.clientY - t.y,
            a = (c) => {
              ((t.x = c.clientX - n), (t.y = c.clientY - o), Le(t));
            },
            u = () => {
              (document.removeEventListener("mousemove", a),
                document.removeEventListener("mouseup", u));
            };
          (document.addEventListener("mousemove", a),
            document.addEventListener("mouseup", u));
        },
        ns = (e, t) => {
          const n = t.w,
            o = t.h,
            a = e.clientX,
            u = e.clientY,
            c = (l) => {
              ((t.w = Math.max(400, n + (l.clientX - a))),
                (t.h = Math.max(300, o + (l.clientY - u))),
                Le(t));
            },
            p = () => {
              (document.removeEventListener("mousemove", c),
                document.removeEventListener("mouseup", p));
            };
          (document.addEventListener("mousemove", c),
            document.addEventListener("mouseup", p));
        };
      function Xt(e = []) {
        const t = [...new Set((e || []).map(String).filter(Boolean))];
        if (!t.length) return {};
        let n = { x: 48, y: 140, width: 960, height: 640 };
        try {
          const l = en(J.value, { minWidth: 80, minHeight: 80 });
          l && (n = l);
        } catch {}
        const cells = dashboardGrid(n, t.length);
        return Object.fromEntries(t.map((key,index)=>[key,cells[index]]));
      }
      function N({ force: e = !1, refresh: t = !1 } = {}) {
        const n = new Set();
        b.value.forEach((c) => {
          if (c.isSessionFinished && !(c.dashboardKind && liveDashboard.frames[c.runId])) return;
          const p = ee(c);
          if (Te(c.id)) {
            n.add(p);
            return;
          }
          if (!je(c)) return;
          const l = He[c.id],
            y = ct(l);
          if (!y) {
            l &&
              console.warn(
                `[Dashboard] 任务 ${c.id} 的内容区尺寸无效，跳过坐标同步`,
              );
            return;
          }
          (dt(p, y, { force: e, refresh: t || e }),
            n.add(p),
            Ge[c.id] || (Ge[c.id] = !0));
        });
        const a = [...(B.value || []), ...(V.value || []), ...W.value]
            .map(String)
            .filter(Boolean)
            .filter((c) => !n.has(c)),
          u = Xt(a);
        a.forEach((c) => {
          const p = u[c];
          p && dt(c, p, { force: e, refresh: t || e });
        });
      }
      function ss(e) {
        const t = new Set(e),
          n = {};
        return (
          b.value.forEach((o) => {
            if (o.isSessionFinished) return;
            const a = ee(o);
            if (!t.has(a)) return;
            const c = Te(o.id) ? Ye[o.id] : He[o.id],
              p = ct(c);
            p && ((n[a] = { bounds: p, zoomFactor: ut(p) }), (Ge[o.id] = !0));
          }),
          n
        );
      }
      function is(e, { light: t = !1 } = {}) {
        const n = b.value
            .filter((A) => !A.isSessionFinished)
            .filter((A) => je(A))
            .map(ee),
          o = (B.value || []).map(String).filter(Boolean),
          a = (V.value || []).map(String).filter(Boolean),
          u = W.value,
          c = [...new Set([...n, ...o, ...a, ...u])];
        if (c.length === 0) {
          F("set-views-visible", !1);
          return;
        }
        let p;
        {
          p = ss(c) || {};
          const A = [...o, ...a, ...u];
          if (A.length) {
            const P = Xt(A);
            A.forEach((r) => {
              var s;
              if ((s = p[r]) != null && s.bounds) return;
              const K = P[r];
              K && (p[r] = { bounds: { ...K }, zoomFactor: ut(K) });
            });
          }
        }
        const l = b.value.find((A) => A.id === Y.value),
          y = l ? ee(l) : "",
          M = c.includes(y) ? y : c[0] || "";
        F("set-views-visible", e, c.length ? c : void 0, {
          light: t,
          boundsByViewKey: p,
          focusViewKey: M,
        });
      }
      (oe(En, () => {
        const e = b.value,
          t = new Set(
            e
              .filter((n) => n.taskKind === "video_monitor")
              .map((n) => n.viewKey),
          );
        if (
          ((V.value = V.value.filter((n) => t.has(n))),
          e.forEach((n, o) => Yt(n.id, o)),
          Ze(),
          Ft(),
          S(() => vt(se.value, "auto")),
          e.length > 0)
        ) {
          const n = Ht();
          (!n &&
            m.status === "idle" &&
            m.nurtureRunningTasks.length === 0 &&
            e.forEach((a) => {
              if (a.isSessionFinished) return;
              const u = m.accounts.find(
                (c) => c.id === a.id && c.platform === a.platform,
              );
              F("init-automation-view", {
                platform: a.platform,
                accountId: a.id,
                name: a.name,
                proxy: u == null ? void 0 : u.proxy,
              });
            }),
            L({ force: n }));
        } else L();
      }),
        oe(Y, (e) => {
          b.value.some((t) => String(t.id) === String(e)) &&
            ((se.value = e), S(() => vt(e)));
        }),
        oe(ae, () => {
          (Dt(), !(!R.value || H) && (Ze(), X.clear(), L({ force: !0 })));
        }),
        oe(Pn, () => {
          !R.value || H || L({ force: !0 });
        }));
      const jt = () => {
        (U(), S(N));
      };
      return (
        oe(
          () => m.isBatchActive,
          () => {
            (U(),
              S(N),
              setTimeout(() => {
                (U(), S(N));
              }, 150),
              setTimeout(() => {
                (U(), S(N));
              }, 350));
          },
        ),
        oe(O, () => {
          const e = Date.now(),
            t = 350,
            n = () => {
              (U(), Date.now() - e < t && requestAnimationFrame(n));
            };
          (requestAnimationFrame(n),
            setTimeout(() => {
              (U(), S(N));
            }, t + 50));
        }),
        oe(
          () => m.isModalOpen,
          (e) => {
            e
              ? F("set-views-visible", !1)
              : (R.value ||
                  b.value.length > 0 ||
                  B.value.length > 0 ||
                  V.value.length > 0 ||
                  W.value.length > 0) &&
                L({ force: !0 });
          },
        ),
        vs(() => {
          ((R.value = !1),
            F("set-views-visible", !1),
            ie && (cancelAnimationFrame(ie), (ie = 0)),
            Q && (clearTimeout(Q), (Q = 0)),
            Fe(),
            be && (cancelAnimationFrame(be), (be = 0)),
            _e && (cancelAnimationFrame(_e), (_e = 0)),
            Se && (cancelAnimationFrame(Se), (Se = 0)),
            (Oe = null),
            (qe = null),
            j.forEach((e) => clearTimeout(e)),
            j.clear(),
            X.clear(),
            le && le.disconnect(),
            (le = null),
            (it = null),
            window.removeEventListener("resize", Pt),
            window.removeEventListener("radar-security-challenge", Et),
            z.forEach((e) => e()),
            (z.length = 0));
        }),
        (e, t) => {
          const n = Z("a-button"),
            o = Z("a-select"),
            a = Z("a-tag"),
            u = Z("a-badge"),
            c = Z("a-tooltip"),
            p = Z("a-table"),
            l = Z("a-modal"),
            y = Z("a-menu-item"),
            M = Z("a-menu"),
            A = Z("a-dropdown"),
            P = Z("a-segmented"),
            r = Z("a-space"),
            K = Z("a-card");
          return (
            d(),
            v("div", ks, [
              liveDashboard.error ? (d(), v("div", {key:"live-connection-error",role:"status",style:"padding:10px 14px;color:#faad14;border:1px solid #785d24;border-radius:8px;margin-bottom:10px"},
                `实况状态核对失败：${liveDashboard.error}。保留上次结果，正在重连。`,1)) : C("",true),
              i("div", zs, [
                b.value.length > 1
                  ? (d(),
                    v("div", Es, [
                      i("div", Ps, [
                        g(h(et), { size: 14 }),
                        t[15] || (t[15] = i("span", null, "账号概况", -1)),
                        i("strong", null, f(b.value.length), 1),
                      ]),
                      i("div", Fs, [
                        g(
                          n,
                          {
                            type: "text",
                            size: "small",
                            class: "progress-nav-btn",
                            disabled: xe.value <= 0,
                            title: "上一个账号",
                            onClick: t[0] || (t[0] = (s) => Bt(-1)),
                          },
                          { default: _(() => [g(h(As), { size: 14 })]), _: 1 },
                          8,
                          ["disabled"],
                        ),
                        g(
                          o,
                          {
                            value: se.value,
                            "onUpdate:value":
                              t[1] || (t[1] = (s) => (se.value = s)),
                            class: "progress-account-select",
                            size: "small",
                            "show-search": "",
                            "option-filter-prop": "label",
                            options: In.value,
                            onChange: $t,
                          },
                          null,
                          8,
                          ["value", "options"],
                        ),
                        i(
                          "span",
                          $s,
                          f(Math.max(1, xe.value + 1)) +
                            "/" +
                            f(b.value.length),
                          1,
                        ),
                        g(
                          n,
                          {
                            type: "text",
                            size: "small",
                            class: "progress-nav-btn",
                            disabled:
                              xe.value < 0 || xe.value >= b.value.length - 1,
                            title: "下一个账号",
                            onClick: t[2] || (t[2] = (s) => Bt(1)),
                          },
                          { default: _(() => [g(h(Ms), { size: 14 })]), _: 1 },
                          8,
                          ["disabled"],
                        ),
                      ]),
                      t[16] ||
                        (t[16] = i(
                          "span",
                          { class: "progress-nav-tip" },
                          "可用滚轮横向浏览",
                          -1,
                        )),
                    ]))
                  : C("", !0),
                i(
                  "div",
                  {
                    ref_key: "progressRowRef",
                    ref: Ve,
                    class: "progress-monitor-row",
                    onScroll: Vt,
                    onWheel: zn,
                  },
                  [
                    b.value.length === 0
                      ? (d(),
                        v("div", Bs, [
                          g(h(et), { size: 16 }),
                          t[17] ||
                            (t[17] = i(
                              "span",
                              null,
                              "暂无正在运行的任务概况，请先开启任务",
                              -1,
                            )),
                        ]))
                      : C("", !0),
                    (d(!0),
                    v(
                      D,
                      null,
                      pe(b.value, (s) => {
                        var w, ve, he, Zt, Jt;
                        return (
                          d(),
                          v(
                            "div",
                            {
                              key: s.id,
                              ref_for: !0,
                              ref: (I) => pn(I, s.id),
                              class: ne([
                                "account-progress-card clickable-card",
                                { "active-card": tt.value === s.id },
                              ]),
                              onClick: (I) => gn(s.id),
                            },
                            [
                              i("div", Ns, [
                                i("div", Ds, [
                                  i("div", Ws, [
                                    i(
                                      "span",
                                      {
                                        class: ne([
                                          "acc-remark",
                                          { "is-empty": !at(s) },
                                        ]),
                                        title: Xe(s),
                                      },
                                      f(Xe(s)),
                                      11,
                                      Os,
                                    ),
                                    zt(s)
                                      ? (d(),
                                        v(
                                          "span",
                                          {
                                            key: 0,
                                            class: "acc-name",
                                            title: s.nickname || "",
                                          },
                                          f(s.nickname),
                                          9,
                                          qs,
                                        ))
                                      : C("", !0),
                                    s.taskKind === "nurture"
                                      ? (d(),
                                        $(
                                          a,
                                          {
                                            key: 1,
                                            color: "green",
                                            size: "small",
                                            class: "kind-tag",
                                          },
                                          {
                                            default: _(() => [
                                              ...(t[18] ||
                                                (t[18] = [x("养号", -1)])),
                                            ]),
                                            _: 1,
                                          },
                                        ))
                                      : s.taskKind === "work_publish"
                                        ? (d(),
                                          $(
                                            a,
                                            {
                                              key: 2,
                                              color: "cyan",
                                              size: "small",
                                              class: "kind-tag",
                                            },
                                            {
                                              default: _(() => [
                                                ...(t[19] ||
                                                  (t[19] = [x("发布", -1)])),
                                              ]),
                                              _: 1,
                                            },
                                          ))
                                        : s.taskKind === "video_monitor"
                                          ? (d(),
                                            $(
                                              a,
                                              {
                                                key: 3,
                                                color: "blue",
                                                size: "small",
                                                class: "kind-tag",
                                              },
                                              {
                                                default: _(() => [
                                                  ...(t[20] ||
                                                    (t[20] = [x("监控", -1)])),
                                                ]),
                                                _: 1,
                                              },
                                            ))
                                          : s.taskKind === "entity_leadgen"
                                            ? (d(),
                                              $(
                                                a,
                                                {
                                                  key: 4,
                                                  color: "purple",
                                                  size: "small",
                                                  class: "kind-tag",
                                                },
                                                {
                                                  default: _(() => [
                                                    x(f(h(de)(s)), 1),
                                                  ]),
                                                  _: 2,
                                                },
                                                1024,
                                              ))
                                            : s.taskKind === "batch"
                                              ? (d(),
                                                $(
                                                  a,
                                                  {
                                                    key: 5,
                                                    color: "orange",
                                                    size: "small",
                                                    class: "kind-tag",
                                                  },
                                                  {
                                                    default: _(() => [
                                                      ...(t[21] ||
                                                        (t[21] = [
                                                          x("批量", -1),
                                                        ])),
                                                    ]),
                                                    _: 1,
                                                  },
                                                ))
                                              : C("", !0),
                                  ]),
                                  i("div", Ys, [
                                    s.dashboardKind ? (d(), $(a, {key:"live-status",color: s.stats?.commentLikeUncertain ? "orange" : "blue", class:"status-tag"},
                                      {default:_(()=>[x(liveStatusText(s),1)]),_:2},1024)) : s.isSessionFinished
                                      ? (d(),
                                        $(
                                          a,
                                          {
                                            key: 0,
                                            color: "orange",
                                            size: "small",
                                            class: "status-tag",
                                          },
                                          {
                                            default: _(() => [
                                              ...(t[22] ||
                                                (t[22] = [x(" 已结束 ", -1)])),
                                            ]),
                                            _: 1,
                                          },
                                        ))
                                      : T(s.id).status === "finished"
                                        ? (d(),
                                          $(
                                            a,
                                            {
                                              key: 1,
                                              color: "orange",
                                              size: "small",
                                              class: "status-tag",
                                            },
                                            {
                                              default: _(() => [
                                                ...(t[23] ||
                                                  (t[23] = [
                                                    x(" 已完成 ", -1),
                                                  ])),
                                              ]),
                                              _: 1,
                                            },
                                          ))
                                        : s.taskKind === "nurture" &&
                                            T(s.id).nurtureSafetyPaused
                                          ? (d(),
                                            $(
                                              a,
                                              {
                                                key: 2,
                                                color: "orange",
                                                size: "small",
                                                class: "status-tag",
                                              },
                                              {
                                                default: _(() => [
                                                  ...(t[24] ||
                                                    (t[24] = [
                                                      x(" 已暂停 ", -1),
                                                    ])),
                                                ]),
                                                _: 1,
                                              },
                                            ))
                                          : T(s.id).status === "running"
                                            ? (d(),
                                              $(
                                                a,
                                                {
                                                  key: 3,
                                                  color: "green",
                                                  size: "small",
                                                  class: "status-tag",
                                                },
                                                {
                                                  default: _(() => [
                                                    ...(t[25] ||
                                                      (t[25] = [
                                                        x(" 运行中 ", -1),
                                                      ])),
                                                  ]),
                                                  _: 1,
                                                },
                                              ))
                                            : C("", !0),
                                    ze(s)
                                      ? (d(),
                                        $(
                                          n,
                                          {
                                            key: 4,
                                            type: "text",
                                            danger: "",
                                            size: "small",
                                            class: "acc-stop-btn",
                                            title: "仅停止此账号",
                                            "aria-label": "仅停止此账号",
                                            onClick: E((I) => ft(s), ["stop"]),
                                          },
                                          {
                                            default: _(() => [
                                              i("span", Gs, [
                                                g(h(ge), { size: 9 }),
                                              ]),
                                              t[26] ||
                                                (t[26] = x(" 停止 ", -1)),
                                            ]),
                                            _: 1,
                                          },
                                          8,
                                          ["onClick"],
                                        ))
                                      : (d(),
                                        $(
                                          n,
                                          {
                                            key: 5,
                                            type: "text",
                                            size: "small",
                                            class: "acc-stop-btn",
                                            title:
                                              "关闭顶部卡片及对应画面，释放内存",
                                            "aria-label":
                                              "关闭顶部卡片及对应画面",
                                            onClick: E((I) => Me(s), ["stop"]),
                                          },
                                          {
                                            default: _(() => [
                                              i("span", Hs, [
                                                g(h(ge), { size: 9 }),
                                              ]),
                                              t[27] ||
                                                (t[27] = x(" 关闭 ", -1)),
                                            ]),
                                            _: 1,
                                          },
                                          8,
                                          ["onClick"],
                                        )),
                                  ]),
                                ]),
                                i("div", Us, [
                                  s.taskKind === "nurture" &&
                                  T(s.id).nurtureRemainingMin
                                    ? (d(),
                                      v(
                                        "span",
                                        Xs,
                                        " 剩余 " +
                                          f(T(s.id).nurtureRemainingMin) +
                                          " 分钟 ",
                                        1,
                                      ))
                                    : C("", !0),
                                  s.taskKind === "nurture" && T(s.id).videos > 0
                                    ? (d(),
                                      v(
                                        "span",
                                        js,
                                        " 已浏览 " + f(T(s.id).videos),
                                        1,
                                      ))
                                    : C("", !0),
                                  s.taskKind === "nurture"
                                    ? (d(),
                                      v(
                                        D,
                                        { key: 2 },
                                        [
                                          i("span", Zs, [
                                            t[28] ||
                                              (t[28] = i(
                                                "em",
                                                null,
                                                "点赞成功",
                                                -1,
                                              )),
                                            i(
                                              "strong",
                                              null,
                                              f(rt(s).likes),
                                              1,
                                            ),
                                          ]),
                                          i("span", Js, [
                                            t[29] ||
                                              (t[29] = i(
                                                "em",
                                                null,
                                                "收藏成功",
                                                -1,
                                              )),
                                            i(
                                              "strong",
                                              null,
                                              f(rt(s).collections),
                                              1,
                                            ),
                                          ]),
                                          i("span", Qs, [
                                            t[30] ||
                                              (t[30] = i(
                                                "em",
                                                null,
                                                "复制链接成功",
                                                -1,
                                              )),
                                            i(
                                              "strong",
                                              null,
                                              f(rt(s).videoShares),
                                              1,
                                            ),
                                          ]),
                                        ],
                                        64,
                                      ))
                                    : C("", !0),
                                  s.taskKind === "work_publish"
                                    ? (d(),
                                      v(
                                        "span",
                                        ei,
                                        f(
                                          s.isSessionFinished
                                            ? "发布任务已结束"
                                            : "作品发布中",
                                        ),
                                        1,
                                      ))
                                    : s.taskKind === "video_monitor"
                                      ? (d(),
                                        v(
                                          D,
                                          { key: 4 },
                                          [
                                            i("span", ti, [
                                              t[31] ||
                                                (t[31] = i(
                                                  "em",
                                                  null,
                                                  "轮询",
                                                  -1,
                                                )),
                                              i(
                                                "strong",
                                                null,
                                                f(ce(s.id).checks || 0),
                                                1,
                                              ),
                                            ]),
                                            i("span", ni, [
                                              t[32] ||
                                                (t[32] = i(
                                                  "em",
                                                  null,
                                                  "新评论",
                                                  -1,
                                                )),
                                              i(
                                                "strong",
                                                null,
                                                f(
                                                  ce(s.id).newComments ||
                                                    ce(s.id).commentsTotal ||
                                                    0,
                                                ),
                                                1,
                                              ),
                                            ]),
                                            i("span", si, [
                                              t[33] ||
                                                (t[33] = i(
                                                  "em",
                                                  null,
                                                  "命中",
                                                  -1,
                                                )),
                                              i(
                                                "strong",
                                                null,
                                                f(ce(s.id).matched || 0),
                                                1,
                                              ),
                                            ]),
                                            ce(s.id).likes > 0
                                              ? (d(),
                                                v("span", ii, [
                                                  t[34] ||
                                                    (t[34] = i(
                                                      "em",
                                                      null,
                                                      "点赞",
                                                      -1,
                                                    )),
                                                  i(
                                                    "strong",
                                                    null,
                                                    f(ce(s.id).likes),
                                                    1,
                                                  ),
                                                ]))
                                              : C("", !0),
                                            ce(s.id).replies > 0
                                              ? (d(),
                                                v("span", oi, [
                                                  t[35] ||
                                                    (t[35] = i(
                                                      "em",
                                                      null,
                                                      "回复",
                                                      -1,
                                                    )),
                                                  i(
                                                    "strong",
                                                    null,
                                                    f(ce(s.id).replies),
                                                    1,
                                                  ),
                                                ]))
                                              : C("", !0),
                                          ],
                                          64,
                                        ))
                                      : s.taskKind === "entity_leadgen" &&
                                          (w = s.sourceTypes) != null &&
                                          w.includes("comment_like")
                                        ? (d(),
                                          v(
                                            D,
                                            { key: 5 },
                                            [
                                              i("span", li, [
                                                t[36] ||
                                                  (t[36] = i(
                                                    "em",
                                                    null,
                                                    "已处理",
                                                    -1,
                                                  )),
                                                i(
                                                  "strong",
                                                  null,
                                                  f(
                                                    ((ve = T(
                                                      s.id,
                                                    ).commentLikeStats) == null
                                                      ? void 0
                                                      : ve.processed) || 0,
                                                  ) +
                                                    "/" +
                                                    f(
                                                      ((he = T(
                                                        s.id,
                                                      ).commentLikeStats) ==
                                                      null
                                                        ? void 0
                                                        : he.planned) || 0,
                                                    ),
                                                  1,
                                                ),
                                              ]),
                                              i("span", ri, [
                                                t[37] ||
                                                  (t[37] = i(
                                                    "em",
                                                    null,
                                                    "新增点赞",
                                                    -1,
                                                  )),
                                                i(
                                                  "strong",
                                                  null,
                                                  f(
                                                    ((Zt = T(
                                                      s.id,
                                                    ).commentLikeStats) == null
                                                      ? void 0
                                                      : Zt.success) || 0,
                                                  ),
                                                  1,
                                                ),
                                              ]),
                                              i("span", ai, [
                                                t[38] ||
                                                  (t[38] = i(
                                                    "em",
                                                    null,
                                                    "已赞",
                                                    -1,
                                                  )),
                                                i(
                                                  "strong",
                                                  null,
                                                  f(
                                                    ((Jt = T(
                                                      s.id,
                                                    ).commentLikeStats) == null
                                                      ? void 0
                                                      : Jt.already) || 0,
                                                  ),
                                                  1,
                                                ),
                                              ]),
                                            ],
                                            64,
                                          ))
                                        : s.taskKind === "entity_leadgen"
                                          ? (d(),
                                            v(
                                              D,
                                              { key: 6 },
                                              [
                                                i(
                                                  "span",
                                                  {
                                                    class:
                                                      "metric-pill metric-pill--success",
                                                    title: h(Qe)(s)
                                                      ? "当前执行账号产生的检测记录数，判定结果请查看任务详情"
                                                      : "本任务累计写入线索库的条数；有上限时显示 已采集/上限",
                                                  },
                                                  [
                                                    i(
                                                      "em",
                                                      null,
                                                      f(
                                                        h(Qe)(s)
                                                          ? "检测记录"
                                                          : "已采集",
                                                      ),
                                                      1,
                                                    ),
                                                    i("strong", null, [
                                                      x(
                                                        f(
                                                          Ue(s.id).collected ||
                                                            0,
                                                        ),
                                                        1,
                                                      ),
                                                      !h(Qe)(s) && kt(s.id) > 0
                                                        ? (d(),
                                                          v(
                                                            D,
                                                            { key: 0 },
                                                            [
                                                              x(
                                                                "/" +
                                                                  f(kt(s.id)),
                                                                1,
                                                              ),
                                                            ],
                                                            64,
                                                          ))
                                                        : C("", !0),
                                                    ]),
                                                  ],
                                                  8,
                                                  ci,
                                                ),
                                                (d(!0),
                                                v(
                                                  D,
                                                  null,
                                                  pe(
                                                    Tn(s.id),
                                                    (I) => (
                                                      d(),
                                                      v(
                                                        "span",
                                                        {
                                                          key: `${s.id}-${I.key}`,
                                                          class:
                                                            "metric-pill metric-pill--info",
                                                        },
                                                        [
                                                          i(
                                                            "em",
                                                            null,
                                                            f(I.label),
                                                            1,
                                                          ),
                                                          i(
                                                            "strong",
                                                            null,
                                                            f(I.value),
                                                            1,
                                                          ),
                                                        ],
                                                      )
                                                    ),
                                                  ),
                                                  128,
                                                )),
                                                Ue(s.id).duplicates > 0
                                                  ? (d(),
                                                    v("span", ui, [
                                                      t[39] ||
                                                        (t[39] = i(
                                                          "em",
                                                          null,
                                                          "重复",
                                                          -1,
                                                        )),
                                                      i(
                                                        "strong",
                                                        null,
                                                        f(Ue(s.id).duplicates),
                                                        1,
                                                      ),
                                                    ]))
                                                  : C("", !0),
                                              ],
                                              64,
                                            ))
                                          : s.taskKind !== "nurture"
                                            ? (d(),
                                              v(
                                                D,
                                                { key: 7 },
                                                [
                                                  s.isSessionFinished
                                                    ? (d(!0),
                                                      v(
                                                        D,
                                                        { key: 0 },
                                                        pe(
                                                          Wn(s),
                                                          (I) => (
                                                            d(),
                                                            v(
                                                              "span",
                                                              {
                                                                key: `${s.id}-${I.key}`,
                                                                class: ne([
                                                                  "metric-pill",
                                                                  `metric-pill--${I.kind || "info"}`,
                                                                ]),
                                                                title: I.title,
                                                              },
                                                              [
                                                                i(
                                                                  "em",
                                                                  null,
                                                                  f(I.label),
                                                                  1,
                                                                ),
                                                                i(
                                                                  "strong",
                                                                  null,
                                                                  f(I.value),
                                                                  1,
                                                                ),
                                                              ],
                                                              10,
                                                              di,
                                                            )
                                                          ),
                                                        ),
                                                        128,
                                                      ))
                                                    : (d(),
                                                      v(
                                                        D,
                                                        { key: 1 },
                                                        [
                                                          T(s.id)
                                                            .interactionTotal
                                                            ? (d(),
                                                              v("span", mi, [
                                                                t[40] ||
                                                                  (t[40] = i(
                                                                    "em",
                                                                    null,
                                                                    "互动上限",
                                                                    -1,
                                                                  )),
                                                                i(
                                                                  "strong",
                                                                  null,
                                                                  f(
                                                                    T(s.id)
                                                                      .interactionTotal
                                                                      .current,
                                                                  ) +
                                                                    "/" +
                                                                    f(
                                                                      T(s.id)
                                                                        .interactionTotal
                                                                        .limit,
                                                                    ),
                                                                  1,
                                                                ),
                                                              ]))
                                                            : C("", !0),
                                                          T(s.id).targetVideos >
                                                          0
                                                            ? (d(),
                                                              v("span", fi, [
                                                                i(
                                                                  "em",
                                                                  null,
                                                                  f(Kn(s.id)),
                                                                  1,
                                                                ),
                                                                i(
                                                                  "strong",
                                                                  null,
                                                                  f(
                                                                    T(s.id)
                                                                      .videos,
                                                                  ) +
                                                                    "/" +
                                                                    f(
                                                                      T(s.id)
                                                                        .targetVideos,
                                                                    ),
                                                                  1,
                                                                ),
                                                              ]))
                                                            : C("", !0),
                                                        ],
                                                        64,
                                                      )),
                                                ],
                                                64,
                                              ))
                                            : C("", !0),
                                ]),
                              ]),
                              s.dashboardKind ? (d(), v("div", {key:"live-target",class:"live-task-target",style:"padding:8px 14px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px"}, [
                                i("strong",null,s.name,1),
                                i("div",null,`账号：${s.nickname} · 真机执行`,1),
                                i("div",null,`手动启动 · ${new Date(s.startedAt).toLocaleTimeString("zh-CN",{hour12:false})}${s.endedAt ? " → " + new Date(s.endedAt).toLocaleTimeString("zh-CN",{hour12:false}) : ""}`,1),
                                i("div",null,`视频：${s.target?.videoUrl || ""}`,1),
                                i("div",null,`评论：${s.target?.authorName || ""}：${s.target?.content || ""}`,1),
                                i("div",null,`${liveStatusText(s)}：${s.phase || ""}`,1),
                                liveDashboard.error ? (d(),v("div",{key:"live-error",style:"color:#faad14"},liveDashboard.error,1)) : C("",true),
                              ])) : C("",true),
                              T(s.id).action
                                ? (d(),
                                  v(
                                    "div",
                                    {
                                      key: 0,
                                      class: ne([
                                        "acc-action-line",
                                        {
                                          "acc-action-security":
                                            T(s.id).actionLevel === "security",
                                          "acc-action-warning":
                                            T(s.id).actionLevel === "warning",
                                        },
                                      ]),
                                    },
                                    [
                                      t[41] ||
                                        (t[41] = i(
                                          "span",
                                          { class: "pulse-dot" },
                                          null,
                                          -1,
                                        )),
                                      i(
                                        "span",
                                        {
                                          class: ne([
                                            "action-text",
                                            {
                                              "action-text-security":
                                                T(s.id).actionLevel ===
                                                "security",
                                              "action-text-warning":
                                                T(s.id).actionLevel ===
                                                "warning",
                                            },
                                          ]),
                                          title: T(s.id).action,
                                        },
                                        f(T(s.id).action),
                                        11,
                                        vi,
                                      ),
                                    ],
                                    2,
                                  ))
                                : C("", !0),
                              i(
                                "div",
                                {
                                  class: "card-terminal-bubble",
                                  onClick:
                                    t[3] || (t[3] = E(() => {}, ["stop"])),
                                },
                                [
                                  i("div", hi, [
                                    i("div", pi, [
                                      t[42] ||
                                        (t[42] = i(
                                          "span",
                                          { class: "title-text" },
                                          "📡 运行轨迹",
                                          -1,
                                        )),
                                      T(s.id).keyword
                                        ? (d(),
                                          v(
                                            "span",
                                            {
                                              key: 0,
                                              class: "terminal-active-keyword",
                                              title: `当前入口：${T(s.id).keyword}`,
                                            },
                                            " 🎯 " + f(T(s.id).keyword),
                                            9,
                                            gi,
                                          ))
                                        : C("", !0),
                                    ]),
                                    i("div", yi, [
                                      i(
                                        "button",
                                        {
                                          type: "button",
                                          class: "clear-logs-btn",
                                          disabled: !(h(we)[s.id] || []).length,
                                          title: "清空当前账号运行轨迹",
                                          onClick: E((I) => Ct(s.id), ["stop"]),
                                        },
                                        "清空",
                                        8,
                                        wi,
                                      ),
                                      g(
                                        h(hs),
                                        {
                                          size: 11,
                                          class: ne([
                                            "copy-icon",
                                            {
                                              disabled: !(h(we)[s.id] || [])
                                                .length,
                                            },
                                          ]),
                                          title: "复制当前账号全部运行轨迹",
                                          onClick: E((I) => Lt(s.id), ["stop"]),
                                        },
                                        null,
                                        8,
                                        ["class", "onClick"],
                                      ),
                                      g(
                                        h(bt),
                                        {
                                          size: 11,
                                          class: "zoom-icon",
                                          title: "展开运行轨迹",
                                          onClick: (I) => fn(s.id),
                                        },
                                        null,
                                        8,
                                        ["onClick"],
                                      ),
                                    ]),
                                  ]),
                                  i(
                                    "div",
                                    {
                                      class: "terminal-bubble-body",
                                      ref_for: !0,
                                      ref: (I) => hn(I, s.id),
                                    },
                                    [
                                      Wt(s).length === 0
                                        ? (d(),
                                          v("div", _i, " 等待任务启动... "))
                                        : (d(!0),
                                          v(
                                            D,
                                            { key: 1 },
                                            pe(
                                              Wt(s),
                                              (I, os) => (
                                                d(),
                                                v(
                                                  "div",
                                                  {
                                                    key: os,
                                                    class: ne([
                                                      "terminal-line",
                                                      "terminal-line--" +
                                                        (I.level || "normal"),
                                                    ]),
                                                  },
                                                  [
                                                    i(
                                                      "span",
                                                      bi,
                                                      "[" + f(I.time) + "]",
                                                      1,
                                                    ),
                                                    i(
                                                      "span",
                                                      {
                                                        class: "msg",
                                                        title: I.msg,
                                                      },
                                                      f(I.msg),
                                                      9,
                                                      Si,
                                                    ),
                                                  ],
                                                  2,
                                                )
                                              ),
                                            ),
                                            128,
                                          )),
                                    ],
                                    512,
                                  ),
                                ],
                              ),
                            ],
                            10,
                            Vs,
                          )
                        );
                      }),
                      128,
                    )),
                  ],
                  544,
                ),
              ]),
              g(
                gs,
                { name: "slide-up", onAfterEnter: jt, onAfterLeave: jt },
                {
                  default: _(() => [
                    h(m).isBatchActive
                      ? (d(),
                        v(
                          "div",
                          {
                            key: 0,
                            class: "batch-progress-panel clickable",
                            onClick: t[4] || (t[4] = (s) => (Ie.value = !0)),
                          },
                          [
                            t[49] ||
                              (t[49] = i(
                                "div",
                                { class: "panel-blur" },
                                null,
                                -1,
                              )),
                            i("div", Ti, [
                              i("div", Ki, [
                                i("div", xi, [
                                  h(m).batchProgress.status === "preparing"
                                    ? (d(),
                                      v(
                                        D,
                                        { key: 0 },
                                        [
                                          g(h(tn), {
                                            size: 18,
                                            class: "spin active-icon",
                                          }),
                                          t[43] ||
                                            (t[43] = i(
                                              "div",
                                              { class: "text-group" },
                                              [
                                                i(
                                                  "span",
                                                  { class: "label" },
                                                  "正在准备自动化环境...",
                                                ),
                                                i(
                                                  "span",
                                                  { class: "detail" },
                                                  "正在拉取并初始化账号，请稍后",
                                                ),
                                              ],
                                              -1,
                                            )),
                                        ],
                                        64,
                                      ))
                                    : (d(),
                                      v(
                                        D,
                                        { key: 1 },
                                        [
                                          g(h(wt), {
                                            size: 18,
                                            class: "active-icon",
                                          }),
                                          i("div", Mi, [
                                            h(m).batchRuntimeRuns.some(
                                              (s) => s.paused,
                                            )
                                              ? (d(),
                                                v(
                                                  "span",
                                                  Ci,
                                                  "批量跟进正在等待授权连接恢复（点击查看详情）",
                                                ))
                                              : (d(),
                                                v(
                                                  "span",
                                                  Li,
                                                  "正在执行 " +
                                                    f(
                                                      h(m).batchRuntimeRuns
                                                        .length || 1,
                                                    ) +
                                                    " 项批量跟进（点击查看详情）",
                                                  1,
                                                )),
                                            i("span", Ai, [
                                              t[44] ||
                                                (t[44] = x(" 当前: ", -1)),
                                              i(
                                                "strong",
                                                Ri,
                                                f(
                                                  h(m).batchProgress
                                                    .lastLeadName || "准备中",
                                                ),
                                                1,
                                              ),
                                              h(m).batchProgress.lastAction
                                                ? (d(),
                                                  v(
                                                    "span",
                                                    Ii,
                                                    " (" +
                                                      f(
                                                        h(Rt)(
                                                          h(m).batchProgress
                                                            .lastAction,
                                                        ),
                                                      ) +
                                                      ") ",
                                                    1,
                                                  ))
                                                : C("", !0),
                                            ]),
                                          ]),
                                        ],
                                        64,
                                      )),
                                ]),
                                i("div", ki, [
                                  i("div", zi, [
                                    t[45] || (t[45] = x("成功: ", -1)),
                                    i(
                                      "span",
                                      Ei,
                                      f(h(m).batchProgress.success),
                                      1,
                                    ),
                                  ]),
                                  i("div", Pi, [
                                    t[46] || (t[46] = x("失败: ", -1)),
                                    i(
                                      "span",
                                      Fi,
                                      f(h(m).batchProgress.failed),
                                      1,
                                    ),
                                  ]),
                                  i("div", $i, [
                                    t[47] || (t[47] = x("进度: ", -1)),
                                    i(
                                      "strong",
                                      null,
                                      f(h(m).batchProgress.current) +
                                        " / " +
                                        f(h(m).batchProgress.total),
                                      1,
                                    ),
                                  ]),
                                ]),
                              ]),
                              i("div", Bi, [
                                i(
                                  "div",
                                  {
                                    class: "progress-bar",
                                    style: _t({
                                      width:
                                        (h(m).batchProgress.current /
                                          h(m).batchProgress.total) *
                                          100 +
                                        "%",
                                    }),
                                  },
                                  null,
                                  4,
                                ),
                              ]),
                              g(
                                n,
                                {
                                  type: "primary",
                                  danger: "",
                                  size: "small",
                                  onClick: E(Mn, ["stop"]),
                                  class: "stop-btn",
                                },
                                {
                                  default: _(() => [
                                    g(h(ge), { size: 14 }),
                                    t[48] || (t[48] = x(" 停止任务 ", -1)),
                                  ]),
                                  _: 1,
                                },
                              ),
                            ]),
                          ],
                        ))
                      : C("", !0),
                  ]),
                  _: 1,
                },
              ),
              g(
                l,
                {
                  open: Ie.value,
                  "onUpdate:open": t[5] || (t[5] = (s) => (Ie.value = s)),
                  title: "批量操作执行详情",
                  width: "900px",
                  footer: null,
                  centered: "",
                  class: "premium-modal",
                },
                {
                  default: _(() => [
                    i("div", Vi, [
                      i("div", Ni, [
                        g(
                          a,
                          { color: "processing" },
                          {
                            default: _(() => [
                              x("总计: " + f(h(m).batchProgress.total), 1),
                            ]),
                            _: 1,
                          },
                        ),
                        g(
                          a,
                          { color: "success" },
                          {
                            default: _(() => [
                              x("成功: " + f(h(m).batchProgress.success), 1),
                            ]),
                            _: 1,
                          },
                        ),
                        g(
                          a,
                          { color: "error" },
                          {
                            default: _(() => [
                              x("失败: " + f(h(m).batchProgress.failed), 1),
                            ]),
                            _: 1,
                          },
                        ),
                      ]),
                      g(
                        n,
                        {
                          type: "primary",
                          size: "small",
                          onClick: e.exportResults,
                        },
                        {
                          icon: _(() => [g(h(Cs), { size: 14 })]),
                          default: _(() => [
                            t[50] || (t[50] = x(" 导出记录 ", -1)),
                          ]),
                          _: 1,
                        },
                        8,
                        ["onClick"],
                      ),
                    ]),
                    g(
                      p,
                      {
                        dataSource: h(m).batchProgress.results,
                        columns: wn,
                        size: "small",
                        pagination: { pageSize: 10 },
                        class: "result-table",
                      },
                      {
                        bodyCell: _(({ column: s, record: w }) => [
                          s.key === "success"
                            ? (d(),
                              v(
                                D,
                                { key: 0 },
                                [
                                  w.success
                                    ? (d(),
                                      $(u, {
                                        key: 0,
                                        status: "success",
                                        text: "成功",
                                      }))
                                    : (d(),
                                      $(
                                        c,
                                        { key: 1, title: w.error },
                                        {
                                          default: _(() => [
                                            g(u, {
                                              status: "error",
                                              text: "失败",
                                            }),
                                          ]),
                                          _: 1,
                                        },
                                        8,
                                        ["title"],
                                      )),
                                ],
                                64,
                              ))
                            : C("", !0),
                          s.key === "type"
                            ? (d(),
                              v(D, { key: 1 }, [x(f(h(Rt)(w.type)), 1)], 64))
                            : C("", !0),
                        ]),
                        _: 1,
                      },
                      8,
                      ["dataSource"],
                    ),
                  ]),
                  _: 1,
                },
                8,
                ["open"],
              ),
              i("div", Di, [
                i("div", Wi, [
                  i("div", Oi, [
                    g(
                      K,
                      { bordered: !1, class: "main-card workspace-card" },
                      {
                        title: _(() => [
                          ...(t[51] ||
                            (t[51] = [
                              i(
                                "span",
                                { class: "card-title" },
                                "实况大屏",
                                -1,
                              ),
                            ])),
                        ]),
                        extra: _(() => [
                          g(
                            r,
                            { size: 8 },
                            {
                              default: _(() => [
                                R.value
                                  ? (d(),
                                    v(
                                      D,
                                      { key: 1 },
                                      [
                                        g(
                                          P,
                                          {
                                            value: ae.value,
                                            "onUpdate:value":
                                              t[7] ||
                                              (t[7] = (s) => (ae.value = s)),
                                            size: "small",
                                            options: mn,
                                          },
                                          null,
                                          8,
                                          ["value"],
                                        ),
                                        g(
                                          n,
                                          { size: "small", onClick: $e },
                                          {
                                            default: _(() => [
                                              ...(t[55] ||
                                                (t[55] = [x("关闭画面", -1)])),
                                            ]),
                                            _: 1,
                                          },
                                        ),
                                      ],
                                      64,
                                    ))
                                  : (d(),
                                    $(
                                      A,
                                      {
                                        key: 0,
                                        disabled: Ke.value.length === 0,
                                      },
                                      {
                                        overlay: _(() => [
                                          g(
                                            M,
                                            {
                                              onClick:
                                                t[6] ||
                                                (t[6] = ({ key: s }) => pt(s)),
                                            },
                                            {
                                              default: _(() => [
                                                g(
                                                  y,
                                                  { key: "current" },
                                                  {
                                                    default: _(() => [
                                                      ...(t[53] ||
                                                        (t[53] = [
                                                          x("看当前账号", -1),
                                                        ])),
                                                    ]),
                                                    _: 1,
                                                  },
                                                ),
                                                g(
                                                  y,
                                                  { key: "all" },
                                                  {
                                                    default: _(() => [
                                                      ...(t[54] ||
                                                        (t[54] = [
                                                          x("看全部账号", -1),
                                                        ])),
                                                    ]),
                                                    _: 1,
                                                  },
                                                ),
                                              ]),
                                              _: 1,
                                            },
                                          ),
                                        ]),
                                        default: _(() => [
                                          g(
                                            n,
                                            { type: "primary", size: "small" },
                                            {
                                              default: _(() => [
                                                ...(t[52] ||
                                                  (t[52] = [
                                                    x(" 打开画面 ", -1),
                                                  ])),
                                              ]),
                                              _: 1,
                                            },
                                          ),
                                        ]),
                                        _: 1,
                                      },
                                      8,
                                      ["disabled"],
                                    )),
                                g(
                                  c,
                                  { title: "一键自适应排列" },
                                  {
                                    default: _(() => [
                                      g(
                                        n,
                                        {
                                          type: "primary",
                                          size: "small",
                                          onClick: U,
                                        },
                                        {
                                          icon: _(() => [
                                            g(h(Rs), { size: 13 }),
                                          ]),
                                          default: _(() => [
                                            t[56] ||
                                              (t[56] = x(" 磁吸排列 ", -1)),
                                          ]),
                                          _: 1,
                                        },
                                      ),
                                    ]),
                                    _: 1,
                                  },
                                ),
                              ]),
                              _: 1,
                            },
                          ),
                        ]),
                        default: _(() => [
                          i(
                            "div",
                            {
                              class: "workspace-canvas",
                              ref_key: "canvasRef",
                              ref: J,
                            },
                            [
                              b.value.length === 0 &&
                              B.value.length === 0 &&
                              V.value.length === 0 &&
                              W.value.length === 0
                                ? (d(),
                                  v("div", qi, [
                                    g(h(St), { size: 40 }),
                                    t[57] ||
                                      (t[57] = i(
                                        "p",
                                        null,
                                        "暂无需要监控的窗口",
                                        -1,
                                      )),
                                    t[58] ||
                                      (t[58] = i(
                                        "span",
                                        null,
                                        "启动获客或养号任务后，此处显示各账号卡片；点击「打开画面」或卡片即可查看浏览器实况",
                                        -1,
                                      )),
                                  ]))
                                : b.value.length === 0 &&
                                    (B.value.length > 0 ||
                                      V.value.length > 0 ||
                                      W.value.length > 0)
                                  ? (d(),
                                    v("div", Yi, [
                                      g(h(St), { size: 40 }),
                                      t[59] ||
                                        (t[59] = i("p", null, "任务实况", -1)),
                                      t[60] ||
                                        (t[60] = i(
                                          "span",
                                          null,
                                          "BrowserView 叠在本区域；可点「关闭画面」收起",
                                          -1,
                                        )),
                                    ]))
                                  : C("", !0),
                              (d(!0),
                              v(
                                D,
                                null,
                                pe(
                                  b.value,
                                  (s) => (
                                    d(),
                                    v(
                                      "div",
                                      {
                                        key: s.id,
                                        class: ne([
                                          "task-window",
                                          { "active-window": Y.value === s.id },
                                        ]),
                                        style: _t(Gn(s.id)),
                                        onMousedown: (w) => Ce(s.id),
                                        ref_for: !0,
                                        ref: (w) => bn(w, s.id),
                                      },
                                      [
                                        i(
                                          "div",
                                          {
                                            class: "window-header",
                                            onMousedown: (w) => Hn(w, s.id),
                                          },
                                          [
                                            i("div", Ui, [
                                              i(
                                                "span",
                                                {
                                                  class: "dot red",
                                                  title: ze(s)
                                                    ? "仅停止此账号任务"
                                                    : "关闭画面与卡片（释放内存）",
                                                  onMousedown:
                                                    t[8] ||
                                                    (t[8] = E(() => {}, [
                                                      "stop",
                                                    ])),
                                                  onClick: E(
                                                    (w) =>
                                                      ze(s) ? ft(s) : Me(s),
                                                    ["stop"],
                                                  ),
                                                },
                                                null,
                                                40,
                                                Xi,
                                              ),
                                              t[61] ||
                                                (t[61] = i(
                                                  "span",
                                                  { class: "dot yellow" },
                                                  null,
                                                  -1,
                                                )),
                                              t[62] ||
                                                (t[62] = i(
                                                  "span",
                                                  { class: "dot green" },
                                                  null,
                                                  -1,
                                                )),
                                            ]),
                                            i("div", ji, [
                                              (d(),
                                              $(Qt(qn(s.platform)), {
                                                size: 11,
                                              })),
                                              x(
                                                " " +
                                                  f(It(s.platform)) +
                                                  " - " +
                                                  f(s.nickname || "未登录") +
                                                  " - " +
                                                  f(s.name) +
                                                  " ",
                                                1,
                                              ),
                                              h(m).batchRunningStates.get(
                                                s.viewKey,
                                              )
                                                ? (d(),
                                                  $(
                                                    a,
                                                    {
                                                      key: 0,
                                                      color: "success",
                                                      size: "small",
                                                      class: "batch-badge",
                                                    },
                                                    {
                                                      icon: _(() => [
                                                        g(h(wt), { size: 10 }),
                                                      ]),
                                                      default: _(() => [
                                                        t[63] ||
                                                          (t[63] = x(
                                                            " 批量任务中 ",
                                                            -1,
                                                          )),
                                                      ]),
                                                      _: 1,
                                                    },
                                                  ))
                                                : C("", !0),
                                            ]),
                                            i("div", Zi, [
                                              ze(s)
                                                ? (d(),
                                                  $(
                                                    n,
                                                    {
                                                      key: 0,
                                                      type: "text",
                                                      danger: "",
                                                      size: "small",
                                                      class: "window-stop-btn",
                                                      title: "仅停止此账号",
                                                      "aria-label":
                                                        "仅停止此账号",
                                                      onMousedown:
                                                        t[9] ||
                                                        (t[9] = E(() => {}, [
                                                          "stop",
                                                        ])),
                                                      onClick: E(
                                                        (w) => ft(s),
                                                        ["stop"],
                                                      ),
                                                    },
                                                    {
                                                      default: _(() => [
                                                        i("span", Ji, [
                                                          g(h(ge), { size: 8 }),
                                                        ]),
                                                        t[64] ||
                                                          (t[64] = x(
                                                            " 停止 ",
                                                            -1,
                                                          )),
                                                      ]),
                                                      _: 1,
                                                    },
                                                    8,
                                                    ["onClick"],
                                                  ))
                                                : (d(),
                                                  $(
                                                    n,
                                                    {
                                                      key: 1,
                                                      type: "text",
                                                      size: "small",
                                                      class: "window-close-btn",
                                                      title:
                                                        "关闭画面与卡片（释放内存）",
                                                      "aria-label":
                                                        "关闭画面与卡片（释放内存）",
                                                      onMousedown:
                                                        t[10] ||
                                                        (t[10] = E(() => {}, [
                                                          "stop",
                                                        ])),
                                                      onClick: E(
                                                        (w) => Me(s),
                                                        ["stop"],
                                                      ),
                                                    },
                                                    {
                                                      default: _(() => [
                                                        i("span", Qi, [
                                                          g(h(ge), { size: 9 }),
                                                        ]),
                                                        t[65] ||
                                                          (t[65] = x(
                                                            " 关闭 ",
                                                            -1,
                                                          )),
                                                      ]),
                                                      _: 1,
                                                    },
                                                    8,
                                                    ["onClick"],
                                                  )),
                                              g(
                                                h(bt),
                                                {
                                                  size: 11,
                                                  class: "action-icon",
                                                  onMousedown:
                                                    t[11] ||
                                                    (t[11] = E(() => {}, [
                                                      "stop",
                                                    ])),
                                                  onClick: E(
                                                    (w) => Jn(s),
                                                    ["stop"],
                                                  ),
                                                },
                                                null,
                                                8,
                                                ["onClick"],
                                              ),
                                            ]),
                                          ],
                                          40,
                                          Hi,
                                        ),
                                        i("div", eo, [
                                          s.isSessionFinished && !(s.dashboardKind && liveDashboard.frames[s.runId])
                                            ? (d(),
                                              v("div", to, [
                                                g(h(et), {
                                                  size: 22,
                                                  class: "finished-icon",
                                                }),
                                                t[67] ||
                                                  (t[67] = i(
                                                    "span",
                                                    { class: "finished-title" },
                                                    "任务已结束",
                                                    -1,
                                                  )),
                                                i("span", no, f(Dn(s)), 1),
                                                qt(s)
                                                  ? (d(),
                                                    v("span", so, f(qt(s)), 1))
                                                  : C("", !0),
                                                g(
                                                  n,
                                                  {
                                                    size: "small",
                                                    type: "text",
                                                    class:
                                                      "dismiss-finished-btn",
                                                    onClick: E(
                                                      (w) => On(s.id),
                                                      ["stop"],
                                                    ),
                                                  },
                                                  {
                                                    default: _(() => [
                                                      ...(t[66] ||
                                                        (t[66] = [
                                                          x(" 收起此卡片 ", -1),
                                                        ])),
                                                    ]),
                                                    _: 1,
                                                  },
                                                  8,
                                                  ["onClick"],
                                                ),
                                              ]))
                                            : gt(s)
                                              ? (d(),
                                                v(
                                                  "div",
                                                  {
                                                    key: 1,
                                                    class:
                                                      "live-suspended-overlay",
                                                    onClick: E(
                                                      (w) => Bn(s),
                                                      ["stop"],
                                                    ),
                                                  },
                                                  [
                                                    g(h(St), {
                                                      size: 22,
                                                      class: "suspended-icon",
                                                    }),
                                                    i("span", oo, f(Vn(s)), 1),
                                                    i("span", lo, f(Nn(s)), 1),
                                                  ],
                                                  8,
                                                  io,
                                                ))
                                              : Ge[s.id]
                                                ? C("", !0)
                                                : (d(),
                                                  v("div", ro, [
                                                    g(h(tn), {
                                                      size: 20,
                                                      class: "spin",
                                                    }),
                                                    t[68] ||
                                                      (t[68] = i(
                                                        "span",
                                                        null,
                                                        "校准视图...",
                                                        -1,
                                                      )),
                                                  ])),
                                          Ie.value
                                            ? (d(),
                                              v("div", ao, [
                                                t[71] ||
                                                  (t[71] = i(
                                                    "div",
                                                    {
                                                      class: "glow-background",
                                                    },
                                                    null,
                                                    -1,
                                                  )),
                                                i("div", co, [
                                                  g(h(et), {
                                                    size: 20,
                                                    class: "pulse-icon",
                                                  }),
                                                  t[69] ||
                                                    (t[69] = i(
                                                      "span",
                                                      { class: "status-title" },
                                                      "后台极智遥测中",
                                                      -1,
                                                    )),
                                                  t[70] ||
                                                    (t[70] = i(
                                                      "span",
                                                      { class: "status-desc" },
                                                      "已安全挂起前台实景以保障渲染层穿透安全",
                                                      -1,
                                                    )),
                                                ]),
                                              ]))
                                            : C("", !0),
                                          t[72] ||
                                            (t[72] = i(
                                              "div",
                                              {
                                                class: "automation-placeholder",
                                              },
                                              null,
                                              -1,
                                            )),
                                        ]),
                                        i(
                                          "div",
                                          {
                                            class: "resize-handle",
                                            onMousedown: E(
                                              (w) => Un(w, s.id),
                                              ["stop"],
                                            ),
                                          },
                                          null,
                                          40,
                                          uo,
                                        ),
                                      ],
                                      46,
                                      Gi,
                                    )
                                  ),
                                ),
                                128,
                              )),
                            ],
                            512,
                          ),
                        ]),
                        _: 1,
                      },
                    ),
                  ]),
                  O.value && nt(O.value)
                    ? (d(),
                      v("div", mo, [
                        i("div", fo, [
                          i("div", vo, [
                            i(
                              "span",
                              {
                                class: ne([
                                  "pulse-dot",
                                  T(O.value).status || "idle",
                                ]),
                              },
                              null,
                              2,
                            ),
                            i(
                              "span",
                              ho,
                              f(nt(O.value).nickname || "未登录") +
                                " - 轨迹详情 ",
                              1,
                            ),
                          ]),
                          i("div", po, [
                            g(
                              n,
                              {
                                type: "text",
                                size: "small",
                                class: "copy-btn",
                                onClick: t[12] || (t[12] = (s) => Lt(O.value)),
                              },
                              {
                                default: _(() => [
                                  ...(t[73] || (t[73] = [x(" 复制 ", -1)])),
                                ]),
                                _: 1,
                              },
                            ),
                            g(
                              n,
                              {
                                type: "text",
                                size: "small",
                                class: "clear-btn",
                                onClick: t[13] || (t[13] = (s) => Ct(O.value)),
                              },
                              {
                                default: _(() => [
                                  ...(t[74] || (t[74] = [x(" 清空 ", -1)])),
                                ]),
                                _: 1,
                              },
                            ),
                            g(h(ge), {
                              size: 14,
                              class: "close-icon",
                              onClick:
                                t[14] ||
                                (t[14] = (s) => {
                                  ((O.value = null), S(() => N()));
                                }),
                            }),
                          ]),
                        ]),
                        i(
                          "div",
                          {
                            class: "sidebar-body",
                            ref_key: "sidebarBodyRef",
                            ref: xt,
                          },
                          [
                            Ot(O.value).length === 0
                              ? (d(), v("div", go, " 暂无运行日志... "))
                              : (d(!0),
                                v(
                                  D,
                                  { key: 1 },
                                  pe(
                                    Ot(O.value),
                                    (s, w) => (
                                      d(),
                                      v(
                                        "div",
                                        {
                                          key: w,
                                          class: ne([
                                            "terminal-line",
                                            "terminal-line--" +
                                              (s.level || "normal"),
                                          ]),
                                        },
                                        [
                                          i(
                                            "span",
                                            yo,
                                            "[" + f(s.time) + "]",
                                            1,
                                          ),
                                          i(
                                            "span",
                                            { class: "msg", title: s.msg },
                                            f(s.msg),
                                            9,
                                            wo,
                                          ),
                                        ],
                                        2,
                                      )
                                    ),
                                  ),
                                  128,
                                )),
                          ],
                          512,
                        ),
                      ]))
                    : C("", !0),
                ]),
              ]),
              (d(),
              $(ws, { to: "body" }, [
                (d(!0),
                v(
                  D,
                  null,
                  pe(
                    q,
                    (s) => (
                      d(),
                      v(
                        "div",
                        {
                          key: s.taskId,
                          ref_for: !0,
                          ref: (w) => _n(w, s.taskId),
                          class: ne([
                            "popup-overlay",
                            { "popup-fullscreen": s.fullscreen },
                          ]),
                          style: _t(es(s)),
                          onMousedown: (w) => Zn(s.taskId),
                        },
                        [
                          i(
                            "div",
                            {
                              class: "popup-titlebar",
                              onMousedown: (w) => ts(w, s),
                            },
                            [
                              i("div", So, [
                                i(
                                  "span",
                                  {
                                    class: "dot red",
                                    onClick: E((w) => Je(s.taskId), ["stop"]),
                                  },
                                  null,
                                  8,
                                  To,
                                ),
                                i(
                                  "span",
                                  {
                                    class: "dot yellow",
                                    onClick: E((w) => Qn(s.taskId), ["stop"]),
                                  },
                                  null,
                                  8,
                                  Ko,
                                ),
                                i(
                                  "span",
                                  {
                                    class: "dot green",
                                    onClick: E((w) => Ut(s.taskId), ["stop"]),
                                  },
                                  null,
                                  8,
                                  xo,
                                ),
                              ]),
                              i(
                                "span",
                                Mo,
                                f(It(s.platform)) +
                                  " - " +
                                  f(s.nickname || "未登录") +
                                  " - " +
                                  f(s.taskName) +
                                  " — 预览窗",
                                1,
                              ),
                              i("div", Co, [
                                (d(),
                                $(
                                  Qt(s.fullscreen ? h(Ls) : h(bt)),
                                  {
                                    size: 12,
                                    class: "action-icon",
                                    onClick: E((w) => Ut(s.taskId), ["stop"]),
                                  },
                                  null,
                                  8,
                                  ["onClick"],
                                )),
                                g(
                                  h(ge),
                                  {
                                    size: 12,
                                    class: "action-icon",
                                    onClick: E((w) => Je(s.taskId), ["stop"]),
                                  },
                                  null,
                                  8,
                                  ["onClick"],
                                ),
                              ]),
                            ],
                            40,
                            bo,
                          ),
                          t[75] ||
                            (t[75] = i(
                              "div",
                              { class: "popup-body" },
                              null,
                              -1,
                            )),
                          s.fullscreen
                            ? C("", !0)
                            : (d(),
                              v(
                                "div",
                                {
                                  key: 0,
                                  class: "popup-resize",
                                  onMousedown: E((w) => ns(w, s), ["stop"]),
                                },
                                null,
                                40,
                                Lo,
                              )),
                        ],
                        46,
                        _o,
                      )
                    ),
                  ),
                  128,
                )),
              ])),
            ])
          );
        }
      );
    },
  },
  Yo = ls(Po, [["__scopeId", "data-v-d216d53f"]]);
export { Yo as default };
