import { projectCommentLikeTask } from "./CommentLikeMode.js";
import {
 C as pt } from "./CommentVisibilitySamplePicker-vpTGBcRe.js";

import {
 ci as Je,
 cj as Xe,
 y as kt,
 an as Ye,
 z as Ze,
 ct as f,
 cu as v,
 cv as C,
 i as s,
 cy as u,
 bt as c,
 cw as n,
 cC as O,
 cx as m,
 cz as j,
 at as T,
 A as p,
 o as H,
 cW as et,
 cn as _,
 e4 as tt,
 ck as ft,
 cl as gt,
 e5 as We,
 cA as De,
 aG as yt,
 e6 as de,
 cQ as wt } from "./index-1pPBwPwk.js";

function bt(A,
 y,
 I = Date.now()) {
 const q = g => String(g || "").normalize("NFKC").replace(/[\u200b\u200e\u200f\u2060\ufeff]/g,
 "").replace(/\s+/g,
 " ").trim().toLowerCase(),
 B = q(y.nickname),
 P = q(y.keyword),
 R = Number(y.days) || 0;
 return A.filter(g => (!B || q(g.authorName).includes(B)) && (!P || q(g.content).includes(P)) && (y.kind === "all" || y.kind === g.kind) && (!R || Number.isFinite(g.commentedAt) && I - g.commentedAt < R * 864e5));
 }
function Lt(A) {
 const y = {
 videoUrl: A.videoUrl,
 authorName: String(A.authorName || "").trim().replace(/^@+/,
 "").trim(),
 content: String(A.content || "").trim() };
 return {
 id: `read:${A.key}`,
 ...y,
 sampleKey: A.key,
 kind: A.kind,
 identity: {
 ...y } };
 }
const _t = {
 class: "reader-scroll" },
 Ct = {
 class: "reader-source" },
 St = {
 class: "reader-link" },
 ht = {
 class: "reader-controls" },
 It = {
 class: "reader-filters" },
 Ut = {
 class: "reader-status" },
 $t = {
 class: "comment-body" },
 Tt = {
 class: "comment-number" },
 Mt = {
 class: "reader-footer" },
 xt = {
 __name: "CommentLikeReader",
 props: {
 accounts: {
 type: Array,
 default: () => [] },
 active: Boolean },
 emits: ["selected"],
 setup(A,
 {
 emit: y }) {
 const I = A,
 q = y,
 {
 invoke: B,
 on: P } = Xe(),
 R = p(!1),
 g = p(!1),
 U = p(!1),
 N = p(""),
 M = p(void 0),
 F = p(40),
 Q = p(!1),
 i = p([]),
 S = p([]),
 W = p(""),
 $ = p({
 nickname: "",
 keyword: "",
 days: 0,
 kind: "all" }),
 re = H(() => bt(i.value,
 $.value)),
 me = H(() => I.accounts.map(k => ({
 value: String(k.id),
 label: et(k) }))),
 le = [{
 value: 0,
 label: "时间不限" },
 {
 value: 1,
 label: "1 天内" },
 {
 value: 3,
 label: "3 天内" },
 {
 value: 7,
 label: "7 天内" },
 {
 value: 30,
 label: "30 天内" }],
 Ce = [{
 value: "all",
 label: "主评及回复" },
 {
 value: "main",
 label: "仅主评" },
 {
 value: "reply",
 label: "仅回复" }],
 Se = [{
 title: "作者",
 dataIndex: "authorName",
 width: 160 },
 {
 title: "评论内容",
 key: "content" },
 {
 title: "类型",
 key: "kind",
 width: 75 },
 {
 title: "评论时间",
 key: "time",
 width: 115 }],
 he = H(() => ({
 selectedRowKeys: S.value,
 preserveSelectedRowKeys: !0,
 onChange: k => {
 S.value = k;
 } }));
 let x = "",
 V,
 Z = !1;
 const E = new Set;
 function G() {
 var k;
 R.value = !0,
 M.value || (M.value = (k = me.value[0]) == null ? void 0 : k.value);
 } async function ce() {
 if (g.value)
        return;
 const k = tt(N.value)[0];
 if (!k)
        return _.warning("请填写抖音作品链接");
 if (!M.value)
        return _.warning("请选择读取账号");
 N.value = k,
 i.value = [],
 S.value = [],
 E.clear();
 const o = crypto.randomUUID();
 x = o,
 g.value = !0,
 U.value = !1,
 W.value = "正在准备读取…";
 try {

        const w = await B("start-comment-like-reader",
 {
 sessionId: o,
 accountId: M.value,
 videoUrl: k,
 limit: F.value,
 includeReplies: Q.value });

        if (Z || x !== o || !R.value) {

            w != null && w.success && await B("stop-comment-like-reader",
 {
 sessionId: o });

            return;

        }
        if (!(w != null && w.success))
            throw new Error((w == null ? void 0 : w.error) || "读取启动失败");

        U.value && await B("stop-comment-like-reader",
 {
 sessionId: o });

    }
    catch (w) {

        x === o && (g.value = !1,
 U.value = !1,
 W.value = w.message || "读取失败",
 x = "");

    } } function Ie(k) {
 if (!x || k.sessionId !== x || Z || !R.value)
        return;
 const o = (k.rows || []).filter(w => E.has(w.key) ? !1 : (E.add(w.key),
 !0));
 o.length && i.value.push(...o),
 k.message && (W.value = k.message),
 k.done && (g.value = !1,
 U.value = !1,
 x = "");
 } async function se() {
 if (!(!x || U.value)) {

        U.value = !0,
 W.value = "正在停止读取，已读评论会保留…";

        try {

            await B("stop-comment-like-reader",
 {
 sessionId: x });

        }
        catch (k) {

            U.value = !1,
 _.error(k.message || "停止读取失败，请重试");

        }
    } } function J() {
 const k = x;
 x = "",
 R.value = !1,
 g.value = !1,
 U.value = !1,
 k && B("stop-comment-like-reader",
 {
 sessionId: k }).catch(() => {
 });
 } function oe() {
 const k = new Set(S.value);
 q("selected",
 i.value.filter(o => k.has(o.key)).map(Lt)),
 J();
 } return kt(() => I.active,
 k => {
 k || J();
 }),
 Ye(() => {
 V = P("comment-like-read-progress",
 Ie);
 }),
 Ze(() => {
 Z = !0,
 J(),
 V == null || V();
 }),
 (k,
 o) => {
 const w = f("a-button"),
 ee = f("a-input"),
 te = f("a-select"),
 Ue = f("a-input-number"),
 $e = f("a-checkbox"),
 ve = f("a-space"),
 ne = f("a-spin"),
 K = f("a-table"),
 pe = f("a-modal");
 return v(),
 C(T,
 null,
 [s(w,
 {
 onClick: G },
 {
 default: u(() => [...o[8] || (o[8] = [c("读取评论并选择",
 -1)])]),
 _: 1 }),
 s(pe,
 {
 open: R.value,
 title: "读取评论并选择",
 centered: "",
 width: "min(1080px, 96vw)",
 footer: null,
 "mask-closable": !1,
 onCancel: J },
 {
 default: u(() => [n("div",
 _t,
 [n("div",
 Ct,
 [n("div",
 St,
 [o[9] || (o[9] = n("label",
 null,
 "视频链接",
 -1)),
 s(ee,
 {
 value: N.value,
 "onUpdate:value": o[0] || (o[0] = L => N.value = L),
 disabled: g.value,
 placeholder: "https://www.douyin.com/video/视频编号" },
 null,
 8,
 ["value",
 "disabled"])]),
 n("div",
 null,
 [o[10] || (o[10] = n("label",
 null,
 "读取账号",
 -1)),
 s(te,
 {
 value: M.value,
 "onUpdate:value": o[1] || (o[1] = L => M.value = L),
 disabled: g.value,
 "show-search": "",
 "option-filter-prop": "label",
 options: me.value,
 placeholder: "选择一个已登录账号",
 style: {
 width: "100%" } },
 null,
 8,
 ["value",
 "disabled",
 "options"])]),
 n("div",
 null,
 [o[11] || (o[11] = n("label",
 null,
 "读取上限",
 -1)),
 s(Ue,
 {
 value: F.value,
 "onUpdate:value": o[2] || (o[2] = L => F.value = L),
 disabled: g.value,
 min: 1,
 max: 60,
 precision: 0,
 style: {
 width: "100%" } },
 null,
 8,
 ["value",
 "disabled"])])]),
 n("div",
 ht,
 [s($e,
 {
 checked: Q.value,
 "onUpdate:checked": o[3] || (o[3] = L => Q.value = L),
 },
 {
 default: u(() => [...o[12] || (o[12] = [c("楼中楼回复",
 -1)])]),
 _: 1 },
 8,
 ["checked"]),
 s(ve,
 null,
 {
 default: u(() => [g.value ? (v(),
 O(w,
 {
 key: 0,
 loading: U.value,
 onClick: se },
 {
 default: u(() => [...o[13] || (o[13] = [c("停止读取",
 -1)])]),
 _: 1 },
 8,
 ["loading"])) : (v(),
 O(w,
 {
 key: 1,
 type: "primary",
 onClick: ce },
 {
 default: u(() => [c(m(i.value.length ? "重新读取" : "开始读取"),
 1)]),
 _: 1 }))]),
 _: 1 })]),
 o[14] || (o[14] = n("p",
 {
 class: "reader-note" },
 "只读当前页评论（最多60条，不代表完整评论区），完成后显示；不会点赞。打开「楼中楼回复」会连二级回复一起读，回复被折叠时先点展开。停止保留已读内容，重新读取清空列表。",
 -1)),
 n("div",
 It,
 [s(ee,
 {
 value: $.value.nickname,
 "onUpdate:value": o[4] || (o[4] = L => $.value.nickname = L),
 "allow-clear": "",
 placeholder: "筛选评论作者昵称（支持关键词）" },
 null,
 8,
 ["value"]),
 s(ee,
 {
 value: $.value.keyword,
 "onUpdate:value": o[5] || (o[5] = L => $.value.keyword = L),
 "allow-clear": "",
 placeholder: "筛选正文关键词" },
 null,
 8,
 ["value"]),
 s(te,
 {
 value: $.value.days,
 "onUpdate:value": o[6] || (o[6] = L => $.value.days = L),
 options: le },
 null,
 8,
 ["value"]),
 s(te,
 {
 value: $.value.kind,
 "onUpdate:value": o[7] || (o[7] = L => $.value.kind = L),
 options: Ce },
 null,
 8,
 ["value"])]),
 n("div",
 Ut,
 [g.value ? (v(),
 O(ne,
 {
 key: 0,
 size: "small" })) : j("",
 !0),
 n("span",
 null,
 m(W.value || "开始读取后可筛选和勾选评论"),
 1)]),
 s(K,
 {
 "data-source": re.value,
 columns: Se,
 "row-key": "key",
 size: "small",
 "row-selection": he.value,
 pagination: {
 pageSize: 20,
 showSizeChanger: !1 },
 scroll: {
 x: 700,
 y: "min(30vh, 300px)" } },
 {
 bodyCell: u(({
 column: L,
 record: X }) => [L.key === "content" ? (v(),
 C(T,
 {
 key: 0 },
 [n("div",
 $t,
 m(X.content),
 1),
 n("div",
 Tt,
 "记录 " + m(X.key),
 1)],
 64)) : L.key === "kind" ? (v(),
 C(T,
 {
 key: 1 },
 [c(m(X.kind === "reply" ? "回复" : "主评"),
 1)],
 64)) : L.key === "time" ? (v(),
 C(T,
 {
 key: 2 },
 [c(m(X.time || "未知"),
 1)],
 64)) : j("",
 !0)]),
 _: 1 },
 8,
 ["data-source",
 "row-selection"])]),
 n("div",
 Mt,
 [n("span",
 null,
 "已读取 " + m(i.value.length) + " 条 · 筛选后 " + m(re.value.length) + " 条 · 已选 " + m(S.value.length) + " 条",
 1),
 s(ve,
 null,
 {
 default: u(() => [s(w,
 {
 onClick: J },
 {
 default: u(() => [...o[15] || (o[15] = [c("关闭",
 -1)])]),
 _: 1 }),
 s(w,
 {
 type: "primary",
 disabled: !S.value.length || U.value,
 onClick: oe },
 {
 default: u(() => [...o[16] || (o[16] = [c("添加所选评论",
 -1)])]),
 _: 1 },
 8,
 ["disabled"])]),
 _: 1 })])]),
 _: 1 },
 8,
 ["open"])],
 64);
 };
 } },
 zt = Je(xt,
 [["__scopeId",
 "data-v-a0b3da26"]]),
 At = {
 class: "comment-like-page" },
 Rt = {
 class: "page-heading" },
 Nt = {
 class: "muted" },
 Dt = {
 class: "muted" },
 Ot = {
 class: "success" },
 Et = {
 class: "muted" },
 Kt = {
 class: "config-scroll" },
 Bt = {
 class: "config-section" },
 Pt = {
 class: "config-section" },
 Ft = {
 key: 0,
 class: "config-section" },
 Vt = {
 key: 1,
 class: "config-section" },
 jt = {
 class: "section-heading manual-heading" },
 qt = {
 class: "manual-comment-heading" },
 Gt = {
 class: "manual-comment-fields" },
 Ht = {
 class: "config-section execution-settings" },
 Qt = {
 class: "section-heading" },
 Wt = {
 class: "muted" },
 Jt = {
 class: "setting-grid" },
 Xt = {
 class: "number-range" },
 Yt = {
 class: "config-footer" },
 Zt = {
 class: "muted" },
 en = {
 class: "detail-summary" },
 tn = {
 class: "detail-toolbar" },
 nn = {
 class: "muted" },
 an = {
 class: "detail-toolbar" },
 ln = {
 class: "log-list" },
 sn = {
 __name: "CommentLikeTasks",
 setup(A) {

        const y = ft(),
 {
 invoke: I,
 on: q } = Xe(),
 {
 stopEntityLeadgenTask: B } = gt(),
 P = p([]),
 R = p(!1),
 g = p(!1),
 U = p(!1),
 N = p(!1),
 M = p(""),
 F = p("");

        function Q() {
 return {
 sourceTypes: ["comment_like"],
 selectedAccounts: [],
 visibilitySampleSource: "local",
 visibilityDetectionMethod: "window",
 visibilityLocalSampleKinds: ["main",
 "reply",
 "profile"],
 visibilityLocalSampleIds: {
},
 commentLikeSource: "local",
 commentLikeManualTargets: [],
 commentLikeTimeDays: 0,
 commentLikeLimit: 1,
 commentLikeIntervalMin: 5,
 commentLikeIntervalMax: 10 };
 }
        const i = p(Q()),
 S = p(1),
 W = H(() => i.value.commentLikeManualTargets.slice((S.value - 1) * 10,
 S.value * 10).map((t,
 e) => ({
 target: t,
 index: (S.value - 1) * 10 + e }))),
 $ = H(() => y.accounts.filter(t => t.platform === "douyin")),
 re = H(() => $.value.map(t => ({
 value: String(t.id),
 label: et(t) }))),
 me = [{
 title: "任务",
 key: "name",
 width: 220 },
 {
 title: "状态",
 key: "status",
 width: 95 },
 {
 title: "执行范围",
 key: "scope",
 width: 190 },
 {
 title: "本次结果",
 key: "result",
 width: 230 },
 {
 title: "操作",
 key: "action",
 width: 420 }],
 le = {
 simulated_success: "模拟成功",
 success: "真实点赞确认",
 already_liked: "已赞/去重",
 not_found: "未找到",
 failed: "失败/未执行",
 skipped: "达到上限",
 uncertain: "结果不确定" },
 Ce = t => le[t] || t,
 Se = [{
 value: "all",
 label: "全部结果" },
 ...Object.entries(le).map(([t,
 e]) => ({
 value: t,
 label: e }))],
 he = Object.entries(le).map(([t,
 e]) => ({
 key: t,
 label: e })),
 x = t => ({
 main: "视频主评",
 reply: "回复评论",
 profile: "首作评论",
 manual: "指定评论" })[t] || "评论",
 V = t => (t == null ? void 0 : t.commentLikeSource) === "manual" ? (t.commentLikeManualTargets || []).filter(e => {
 var l,
 h,
 r;
 return ((l = e.videoUrl) == null ? void 0 : l.trim()) && ((h = e.authorName) == null ? void 0 : h.trim()) && ((r = e.content) == null ? void 0 : r.trim());
 }).length : Object.values((t == null ? void 0 : t.visibilityLocalSampleIds) || {
}).reduce((e,
 l) => e + (Array.isArray(l) ? l.length : 0),
 0),
 Z = t => t ? new Date(t).toLocaleString("zh-CN",
 {
 hour12: !1 }) : "—",
 activeSimulations = p([]),
 offlineTools = p(false),
 nativeReal = !!window.__desktopShell?.commentLikeReal,
 liveStatus = p({available:false,note:"真实点赞未授权"}),
 E = t => activeSimulations.value.includes(t.id);

        async function G(quiet = false) {
 if (!quiet) R.value = !0;
 try {

            const [tasks, runtime] = await Promise.all([I("get-entity-leadgen-tasks"), I("get-comment-like-runtime")]);
            const t = (tasks || []).map(record => projectCommentLikeTask(record, runtime.taskModes?.[record.id], offlineTools.value));
            activeSimulations.value = runtime.taskIds || [];
            liveStatus.value = runtime.live || {available:false,note:"真实点赞未授权"};

            P.value = (t || []).filter(e => {
 var l,
 h;
 return ((h = (l = e.configSnapshot) == null ? void 0 : l.sourceTypes) == null ? void 0 : h[0]) === "comment_like";
 });

        }
        catch (t) {

            _.error(t.message || "任务读取失败");

        }
        finally {

            if (!quiet) R.value = !1;

        } }
        function ce() {
 S.value = 1,
 M.value = "",
 F.value = `评论点赞 ${new Date().toLocaleString("zh-CN",
 {
 hour12: !1 })}`,
 i.value = {
 ...Q(),
 ...y.commentLikeDraft || {
} },
 y.commentLikeDraft = null,
 N.value = !0;
 }
        function Ie(t) {
 S.value = 1,
 M.value = t.id,
 F.value = t.name,
 i.value = {
 ...Q(),
 ...de(t.configSnapshot) },
 N.value = !0;
 }
        function se() {
 i.value.commentLikeManualTargets.push({
 id: crypto.randomUUID(),
 videoUrl: "",
 authorName: "",
 content: "" }),
 S.value = Math.ceil(i.value.commentLikeManualTargets.length / 10);
 }
        function J(t) {
 i.value.commentLikeManualTargets.splice(t,
 1),
 S.value = Math.min(S.value,
 Math.max(1,
 Math.ceil(i.value.commentLikeManualTargets.length / 10)));
 }
        function oe(t) {
 delete t.sampleKey, delete t.commentId,
 delete t.parentCommentId,
 delete t.kind,
 delete t.identity;
 }
        function k(t) {
 const e = i.value.commentLikeManualTargets.filter(r => {
 var b,
 D,
 Y;
 return ((b = r.videoUrl) == null ? void 0 : b.trim()) || ((D = r.authorName) == null ? void 0 : D.trim()) || ((Y = r.content) == null ? void 0 : Y.trim());
 }),
 l = new Set(e.map(r => r.sampleKey || `${r.videoUrl}:${r.authorName}:${r.content}`)),
 h = t.filter(r => {
 const b = r.sampleKey || `${r.videoUrl}:${r.authorName}:${r.content}`;
 return l.has(b) ? !1 : (l.add(b),
 !0);
 });
 i.value.commentLikeManualTargets = [...e,
 ...h],
 S.value = 1,
 _.success(`已添加 ${h.length} 条评论${t.length > h.length ? "，重复评论已跳过" : ""}`);
 }
        function o() {
 i.value.commentLikeSource === "manual" && !i.value.commentLikeManualTargets.length && se();
 }
        function w(t) {
 var e;
 t.videoUrl = tt(t.videoUrl)[0] || ((e = t.videoUrl) == null ? void 0 : e.trim()) || "";
 }
        async function ee(t) {
 var e,
 l,
 h;
 if (i.value.commentLikeSource === "manual") {

            if (!i.value.commentLikeManualTargets.length)
                return _.warning("请至少添加一条指定评论");

            for (const [r,
 b] of i.value.commentLikeManualTargets.entries()) {

                w(b);

                const D = (e = b.videoUrl) != null && e.trim() ? (l = b.authorName) != null && l.trim().replace(/^@+/,
 "").trim() ? (h = b.content) != null && h.trim() ? "" : "完整评论内容" : "评论作者昵称" : "视频链接";

                if (D)
                    return _.warning(`第 ${r + 1} 条指定评论：请填写${D}`);

            }
        }
        else if (!V(i.value))
            return _.warning("请至少选择一条评论");
 if (!i.value.selectedAccounts.length)
            return _.warning("请选择执行账号");
 if (i.value.commentLikeIntervalMax < i.value.commentLikeIntervalMin)
            return _.warning("间隔最大值不能小于最小值");
 U.value = !0;
 try {

            const r = P.value.find(Y => Y.id === M.value),
 b = {
 ...r || {
},
 id: M.value || `comment_like_${crypto.randomUUID()}`,
 name: F.value.trim() || "评论点赞",
 createdAt: (r == null ? void 0 : r.createdAt) || Date.now(),
 status: (r == null ? void 0 : r.status) || "pending",
 configSnapshot: de(i.value) },
 D = await I("save-entity-leadgen-task",
 de(b));

            if (!D || D.success !== true)
                throw new Error(D?.error || "保存任务失败");

            N.value = !1,
 await G(),
 t ? await confirmLive(D) : _.success("任务已保存，可开始真机模拟");

        }
        catch (r) {

            _.error(r.message || "保存失败");

        }
        finally {

            U.value = !1;

        } }
        async function te(t, mode = "live") {
 if (!(g.value || E(t))) {

            g.value = !0;

            try {

                if(mode === "offline" && !offlineTools.value)throw new Error("离线模拟仅可在离线测试入口运行");
                const r = await I(mode === "live" ? (nativeReal ? "start-comment-like-real" : "start-comment-like-live") : "start-comment-like-simulation", {taskId:t.id, executionMode:mode});

                if(r?.cancelled)return;

                if (!(r != null && r.success))
                    throw new Error((r == null ? void 0 : r.error) || "启动失败");

                _.success(mode === "live" ? "真机模拟已启动，将在桌面评论区执行真实点赞" : "离线测试已启动，未执行真实点赞"),
 await G();

            }
            catch (e) {

                _.error(e.message || "启动失败");

            }
            finally {

                g.value = !1;

            }
        } }
        async function confirmLive(task) {
 try {
 if(offlineTools.value)return te(task,"offline");
 if(nativeReal)return te(task,"live");
 if(!liveStatus.value.available || liveStatus.value.taskId!==task.id || liveStatus.value.expiresAt<=Date.now())
 throw new Error("请使用新版桌面客户端，确认账号和评论后开始真机模拟");
 const config = task.configSnapshot || {};
 let targets = config.commentLikeManualTargets || [];
 if((config.commentLikeSource || "local") === "local") {
 targets = [];
 for(const [accountId,selectedIds] of Object.entries(config.visibilityLocalSampleIds || {})) {
 if(!selectedIds.length)continue;
 const result = await I("get-comment-visibility-candidates",{accountId,selectedIds,kinds:config.visibilityLocalSampleKinds || ["main"]});
 if(!result?.success)throw new Error(result?.error || "无法核对所选评论");
 targets.push(...(result.items || []));
 }
 }
 if(targets.length !== 1 || config.selectedAccounts?.length !== 1)throw new Error("本轮真机验证请选择一个执行账号和一条评论");
 const target = targets[0];
 wt.confirm({title:"核对并开始一次真实评论点赞？",content:`视频：${target.videoUrl}；作者：${target.authorName}；评论：${target.content}。仅允许当前所选账号的一次尝试；刷新后仍已赞才计成功，结果不确定不重试。`,okText:"开始真机点赞",onOk:()=>te(task,"live")});
 }catch(error){_.error(error.message || "无法核对真机目标，请重新选择");}
 }
        async function Ue(t) {
 try {
 const result = await I("stop-comment-like-simulation", {taskId:t.id});
 if (!result?.success) return _.error(result?.error || "停止失败");
 _.success("已请求停止当前点赞任务"); await G();
 } catch (error) { _.error(error.message || "停止失败，请重试"); }
 }
        async function $e(t) {
 try {

            const e = await I("show-entity-leadgen-monitor",
 {
 accountId: t.configSnapshot.selectedAccounts[0] });

            if (!(e != null && e.success))
                return _.warning((e == null ? void 0 : e.error) || "账号窗口准备中，请稍后查看");

            y.activeKey = "dashboard";

        }
        catch (e) {

            _.warning(e.message || "打开实况失败");

        } }
        function ve(t) {
 wt.confirm({
 title: "删除该点赞任务？",
 content: "删除任务配置；历史执行结果及去重记录仍保留。",
 okType: "danger",
 onOk: async () => {
 const result = await I("delete-entity-leadgen-tasks", [t.id]);
 if (!result?.success) throw new Error(result?.error || "删除失败");
 await G();
 } });
 }
        const ne = p(!1),
 K = p(null),
 pe = p([]),
 L = p(0),
 X = p({
}),
 Te = p(!1),
 ke = p(1),
 fe = p("all"),
 ge = p(void 0),
 ye = p(!1),
 nt = H(() => {
 var t;
 return (((t = K.value) == null ? void 0 : t.runs) || []).map(e => ({
 value: String(e.accountId),
 label: e.nickname || e.name || e.accountId }));
 }),
 at = [{
 title: "目标评论",
 key: "comment",
 width: 270 },
 {
 title: "执行账号",
 dataIndex: "accountName",
 width: 130 },
 {
 title: "结果",
 key: "status",
 width: 115 },
 {
 title: "具体原因",
 key: "reason",
 width: 280 },
 {
 title: "时间",
 key: "time",
 width: 165 }],
 lt = t => `${t.runId}:${t.accountId}:${t.sample.id}`;

        let ie = 0;

        async function we() {
 var e;
 if (!K.value)
            return;
 const t = ++ie;
 Te.value = !0;
 try {

            const l = await I("get-comment-like-results",
 {
 taskId: K.value.id,
 runId: ye.value ? void 0 : ((e = K.value.stats) == null ? void 0 : e.commentLikeRunId) || "unstarted",
 accountId: ge.value,
 status: fe.value,
 offset: (ke.value - 1) * 20,
 limit: 20 });

            if (t !== ie)
                return;

            if (!(l != null && l.success))
                throw new Error((l == null ? void 0 : l.error) || "详情读取失败");

            pe.value = l.items,
 L.value = l.total,
 X.value = l.counts;

        }
        catch (l) {

            t === ie && _.error(l.message);

        }
        finally {

            t === ie && (Te.value = !1);

        } }
        function be() {
 ke.value = 1,
 we();
 }
        async function st(t) {
 K.value = t,
 fe.value = "all",
 ge.value = void 0,
 ye.value = !1,
 ne.value = !0,
 be();
 }
        const Me = p(!1),
 ue = p(null),
 xe = H(() => {
 var t;
 return y.taskLogs[(t = ue.value) == null ? void 0 : t.id] || [];
 });

        async function ot(t) {
 ue.value = t,
 Me.value = !0;
 try {

            const e = await I("get-entity-leadgen-task-runtime-logs",
 {
 taskId: t.id });

            y.mergeTaskLogs(t.id,
 (e || []).map(l => ({
 ...l,
 msg: l.message || l.msg })));

        }
        catch (e) {

            _.error(e.message || "日志读取失败");

        } }
        async function it() {
 try {

            await I("clear-entity-leadgen-task-runtime-logs",
 {
 taskId: ue.value.id }),
 y.clearTaskLogs(ue.value.id);

        }
        catch (t) {

            _.error(t.message || "日志清空失败");

        } }
        async function ut() {

            try {

                await navigator.clipboard.writeText(xe.value.map(t => `[${new Date(t.ts).toLocaleTimeString()}] ${t.msg}`).join(`
`)),
 _.success("已复制运行日志");

            }
            catch {

                _.warning("复制失败，请重试");

            }
        }
        let Le,
 refreshTimer,
 refreshing = false,
 _e,
 ze = !1;

        return Ye(async () => {
 Le = q("comment-like-task-event",
 t => {
 if (t.type === "log") y.mergeTaskLogs(t.taskId, [{ts:t.ts, msg:t.message, level:t.level || "normal"}]);
 !P.value.some(e => e.id === t.taskId) || !["stats",
 "finished",
 "task-updated"].includes(t.type) || _e || (_e = setTimeout(async () => {
 _e = null,
 await G(),
 !ze && ne.value && (K.value = P.value.find(e => {
 var l;
 return e.id === ((l = K.value) == null ? void 0 : l.id);
 }) || K.value,
 await we());
 },
 1e3));
 }),
 await G(),
 refreshTimer = setInterval(async () => {
 if (ze || refreshing || !activeSimulations.value.length) return;
 refreshing = true;
 try {
 await G(true);
 if (!ze && ne.value) { K.value = P.value.find(task => task.id === K.value?.id) || K.value; await we(); }
 } finally { refreshing = false; }
 }, 3000),
 !ze && y.commentLikeDraft && ce();
 }),
 Ze(() => {
 ze = !0,
 Le == null || Le(),
 clearTimeout(_e),
 clearInterval(refreshTimer),
 ie++;
 }),
 (t,
 e) => {
 var Ke,
 Be;
 const l = f("a-button"),
 h = f("a-alert"),
 r = f("a-tag"),
 b = f("a-table"),
 D = f("a-input"),
 Y = f("a-radio"),
 dt = f("a-radio-group"),
 Ae = f("a-space"),
 rt = f("a-textarea"),
 mt = f("a-pagination"),
 Oe = f("a-empty"),
 Re = f("a-select"),
 Ne = f("a-input-number"),
 ct = f("a-modal"),
 vt = f("a-checkbox"),
 Ee = f("a-drawer");
 return v(),
 C("div",
 At,
 [n("header",
 Rt,
 [e[18] || (e[18] = n("div",
 null,
 [n("h2",
 null,
 "评论点赞"),
 n("p",
 null,
 "使用已登录账号，在桌面视频评论区执行真实点赞，并核对刷新后的结果。")],
 -1)),
 s(l,
 {
 type: "primary",
 onClick: ce },
 {
 default: u(() => [...e[17] || (e[17] = [c("新建点赞任务",
 -1)])]),
 _: 1 }),
 s(l,{type:"link",onClick:async()=>{offlineTools.value=!offlineTools.value;ne.value=false;await G();}},
 {default:u(()=>[c(offlineTools.value?"返回真机操作":"离线测试记录")]),_:1})]),
 s(h,
 {
 class: "page-note",
 type: "info",
 "show-icon": "",
 message: offlineTools.value ? "离线测试：此处只验证流程，不会操作平台，也不增加真实点赞。" : "真机模拟会真实操作已登录账号。每次核对一个账号和一条评论；已赞跳过，刷新后仍保留才计成功，结果不明确不重试。" }),
 s(b,
 {
 "data-source": P.value,
 columns: me,
 "row-key": "id",
 loading: R.value,
 pagination: {
 pageSize: 20,
 showSizeChanger: !0 },
 scroll: {
 x: 1155 },
 size: "middle" },
 {
 bodyCell: u(({
 column: a,
 record: d }) => {
 var z,
 Pe,
 Fe,
 Ve,
 je,
 qe,
 Ge,
 He,
 Qe;
 return [a.key === "name" ? (v(),
 C(T,
 {
 key: 0 },
 [n("strong",
 null,
 m(d.name),
 1),
 n("div",
 Nt,
 (offlineTools.value ? "离线测试 · " : "真机模拟 · ") + m(d.startedAt ? Z(d.startedAt) : "尚未执行"),
 1)],
 64)) : a.key === "status" ? (v(),
 O(r,
 {
 key: 1,
 color: E(d) ? "blue" : d.status === "completed" ? "green" : d.status === "failed" ? "red" : "default" },
 {
 default: u(() => [c(m(E(d) ? (d.executionMode === "live" ? "真机运行中" : "离线测试中") : ({completed:d.executionMode === "live" ? "真机完成" : "离线测试完成", stopped:"已停止", failed:"执行失败", interrupted:"中断待检查"})[d.status] || (offlineTools.value ? "待测试" : "待真机执行")),
 1)]),
 _: 2 },
 1032,
 ["color"])) : a.key === "scope" ? (v(),
 C(T,
 {
 key: 2 },
 [c(m(((Pe = (z = d.configSnapshot) == null ? void 0 : z.selectedAccounts) == null ? void 0 : Pe.length) || 0) + " 个执行账号 × " + m(V(d.configSnapshot)) + " 条评论",
 1),
 n("div",
 Dt,
 m(((Fe = d.configSnapshot) == null ? void 0 : Fe.commentLikeSource) === "manual" ? "指定评论" : "已有评论"),
 1)],
 64)) : a.key === "result" ? (v(),
 C(T,
 {
 key: 3 },
 [n("span",
 Ot,
 (d.executionMode === "live" ? "真实新增 " : "模拟新增 ") + m(((Ve = d.stats) == null ? void 0 : Ve.commentLikeSuccess) || 0),
 1),
 n("div",
 Et,
 "已处理 " + m(((je = d.stats) == null ? void 0 : je.commentLikeProcessed) || 0) + "/" + m(((qe = d.stats) == null ? void 0 : qe.commentLikePlanned) || 0) + " · 已赞 " + m(((Ge = d.stats) == null ? void 0 : Ge.commentLikeAlready) || 0) + " · 未找到 " + m(((He = d.stats) == null ? void 0 : He.commentLikeNotFound) || 0) + " · 失败 " + m(((Qe = d.stats) == null ? void 0 : Qe.commentLikeFailed) || 0) + " · 待核验 " + m(d.stats?.commentLikeUncertain || 0),
 1)],
 64)) : a.key === "action" ? (v(),
 C(T,
 {
 key: 4 },
 [E(d) ? (v(),
 O(l,
 {
 key: 0,
 type: "link",
 size: "small",
 danger: "",
 onClick: ae => Ue(d) },
 {
 default: u(() => [...e[19] || (e[19] = [c("停止",
 -1)])]),
 _: 1 },
 8,
 ["onClick"])) : (v(),
 O(l,
 {
 key: 1,
 type: "link",
 size: "small",
 disabled: g.value,
 onClick: ae => confirmLive(d) },
 {
 default: u(() => [c(offlineTools.value ? "离线测试" : "真机模拟")]),
 _: 1 },
 8,
 ["disabled",
 "onClick"])),
 s(l,
 {
 type: "link",
 size: "small",
 onClick: ae => st(d) },
 {
 default: u(() => [...e[21] || (e[21] = [c("详情",
 -1)])]),
 _: 1 },
 8,
 ["onClick"]),
 s(l,
 {
 type: "link",
 size: "small",
 onClick: ae => ot(d) },
 {
 default: u(() => [...e[22] || (e[22] = [c("日志",
 -1)])]),
 _: 1 },
 8,
 ["onClick"]),
 E(d) && d.executionMode !== "offline" ? (v(),
 O(l,
 {
 key: 2,
 type: "link",
 size: "small",
 onClick: ae => $e(d) },
 {
 default: u(() => [...e[23] || (e[23] = [c("实况",
 -1)])]),
 _: 1 },
 8,
 ["onClick"])) : j("",
 !0),
 s(l,
 {
 type: "link",
 size: "small",
 disabled: E(d),
 onClick: ae => Ie(d) },
 {
 default: u(() => [...e[24] || (e[24] = [c("设置",
 -1)])]),
 _: 1 },
 8,
 ["disabled",
 "onClick"]),
 s(l,
 {
 type: "link",
 size: "small",
 danger: "",
 disabled: E(d),
 onClick: ae => ve(d) },
 {
 default: u(() => [...e[25] || (e[25] = [c("删除",
 -1)])]),
 _: 1 },
 8,
 ["disabled",
 "onClick"])],
 64)) : j("",
 !0)];
 }),
 _: 1 },
 8,
 ["data-source",
 "loading"]),
 s(ct,
 {
 open: N.value,
 "onUpdate:open": e[10] || (e[10] = a => N.value = a),
 title: M.value ? "点赞任务设置" : "新建评论点赞任务",
 width: 1e3,
 footer: null,
 "destroy-on-close": !0 },
 {
 default: u(() => [n("div",
 Kt,
 [n("div",
 Bt,
 [e[26] || (e[26] = n("label",
 {
 class: "section-label" },
 "任务名称",
 -1)),
 s(D,
 {
 value: F.value,
 "onUpdate:value": e[0] || (e[0] = a => F.value = a),
 placeholder: "输入便于识别的任务名称" },
 null,
 8,
 ["value"])]),
 n("div",
 Pt,
 [e[29] || (e[29] = n("label",
 {
 class: "section-label" },
 "评论来源",
 -1)),
 s(dt,
 {
 value: i.value.commentLikeSource,
 "onUpdate:value": e[1] || (e[1] = a => i.value.commentLikeSource = a),
 onChange: o },
 {
 default: u(() => [s(Y,
 {
 value: "local" },
 {
 default: u(() => [...e[27] || (e[27] = [c("已有评论",
 -1)])]),
 _: 1 }),
 s(Y,
 {
 value: "manual" },
 {
 default: u(() => [...e[28] || (e[28] = [c("指定评论",
 -1)])]),
 _: 1 })]),
 _: 1 },
 8,
 ["value"])]),
 i.value.commentLikeSource !== "manual" ? (v(),
 C("div",
 Ft,
 [s(pt,
 {
 config: i.value,
 accounts: $.value,
 purpose: "like" },
 null,
 8,
 ["config",
 "accounts"])])) : (v(),
 C("div",
 Vt,
 [n("div",
 jt,
 [e[31] || (e[31] = n("div",
 null,
 [n("strong",
 null,
 "指定评论"),
 n("p",
 {
 class: "muted" },
 "先读取视频评论并勾选真实目标，再选择已登录的执行账号。点击真机模拟后核对账号、视频和正文，确认后执行真实点赞。")],
 -1)),
 s(Ae,
 null,
 {
 default: u(() => [s(zt,
 {
 accounts: $.value,
 active: N.value,
 onSelected: k },
 null,
 8,
 ["accounts",
 "active"]),
 s(l,
 {
 size: "small",
 onClick: se },
 {
 default: u(() => [...e[30] || (e[30] = [c("添加评论",
 -1)])]),
 _: 1 })]),
 _: 1 })]),
 (v(!0),
 C(T,
 null,
 De(W.value,
 ({
 target: a,
 index: d }) => (v(),
 C("div",
 {
 key: a.id,
 class: "manual-comment" },
 [n("div",
 qt,
 [s(Ae,
 null,
 {
 default: u(() => [n("strong",
 null,
 "评论 " + m(d + 1),
 1),
 a.sampleKey ? (v(),
 O(r,
 {
 key: 0,
 color: "blue" },
 {
 default: u(() => [...e[32] || (e[32] = [c("已记录评论编号",
 -1)])]),
 _: 1 })) : j("",
 !0)]),
 _: 2 },
 1024),
 s(l,
 {
 type: "text",
 size: "small",
 danger: "",
 onClick: z => J(d) },
 {
 default: u(() => [...e[33] || (e[33] = [c("删除",
 -1)])]),
 _: 1 },
 8,
 ["onClick"])]),
 n("div",
 Gt,
 [n("div",
 null,
 [e[34] || (e[34] = n("label",
 {
 class: "section-label" },
 "视频链接",
 -1)),
 s(D,
 {
 value: a.videoUrl,
 "onUpdate:value": z => a.videoUrl = z,
 placeholder: "https://www.douyin.com/video/视频编号",
 onChange: z => oe(a),
 onBlur: z => w(a) },
 null,
 8,
 ["value",
 "onUpdate:value",
 "onChange",
 "onBlur"])]),
 n("div",
 null,
 [e[35] || (e[35] = n("label",
 {
 class: "section-label" },
 "评论作者昵称",
 -1)),
 s(D,
 {
 value: a.authorName,
 "onUpdate:value": z => a.authorName = z,
 placeholder: "填写该条评论的作者昵称",
 onChange: z => oe(a) },
 null,
 8,
 ["value",
 "onUpdate:value",
 "onChange"])])]),
 e[36] || (e[36] = n("label",
 {
 class: "section-label" },
 "完整评论内容",
 -1)),
 s(rt,
 {
 value: a.content,
 "onUpdate:value": z => a.content = z,
 "auto-size": {
 minRows: 2,
 maxRows: 6 },
 placeholder: "粘贴完整评论正文，保留标点和表情",
 onChange: z => oe(a) },
 null,
 8,
 ["value",
 "onUpdate:value",
 "onChange"])]))),
 128)),
 i.value.commentLikeManualTargets.length > 10 ? (v(),
 O(mt,
 {
 key: 0,
 current: S.value,
 "onUpdate:current": e[2] || (e[2] = a => S.value = a),
 total: i.value.commentLikeManualTargets.length,
 "page-size": 10,
 "show-size-changer": !1,
 size: "small",
 style: {
 "margin-top": "14px" } },
 null,
 8,
 ["current",
 "total"])) : j("",
 !0),
 i.value.commentLikeManualTargets.length ? j("",
 !0) : (v(),
 O(Oe,
 {
 key: 1,
 description: "添加需要点赞的评论",
 image: null },
 {
 default: u(() => [s(l,
 {
 onClick: se },
 {
 default: u(() => [...e[37] || (e[37] = [c("添加评论",
 -1)])]),
 _: 1 })]),
 _: 1 }))])),
 n("div",
 Ht,
 [n("div",
 Qt,
 [e[38] || (e[38] = n("strong",
 null,
 "执行设置",
 -1)),
 n("span",
 Wt,
 m(i.value.commentLikeSource === "manual" ? "每个账号分别处理全部指定评论" : "评论所属账号与执行账号分别选择"),
 1)]),
 e[44] || (e[44] = n("label",
 {
 class: "section-label" },
 "执行账号（可多选）",
 -1)),
 s(Re,
 {
 value: i.value.selectedAccounts,
 "onUpdate:value": e[3] || (e[3] = a => i.value.selectedAccounts = a),
 mode: "multiple",
 "allow-clear": "",
 "option-filter-prop": "label",
 options: re.value,
 placeholder: "选择用来点赞的账号",
 style: {
 width: "100%" } },
 null,
 8,
 ["value",
 "options"]),
 n("div",
 Jt,
 [n("div",
 null,
 [e[39] || (e[39] = n("label",
 {
 class: "section-label" },
 "每账号本次新点赞上限",
 -1)),
 s(Ne,
 {
 value: i.value.commentLikeLimit,
 "onUpdate:value": e[4] || (e[4] = a => i.value.commentLikeLimit = a),
 min: 0,
 precision: 0,
 style: {
 width: "100%" } },
 null,
 8,
 ["value"]),
 e[40] || (e[40] = n("p",
 {
 class: "muted" },
 "本轮真机验证每次一个账号、一条评论；已赞跳过。",
 -1))]),
 n("div",
 null,
 [e[42] || (e[42] = n("label",
 {
 class: "section-label" },
 "每条操作间隔（秒）",
 -1)),
 n("div",
 Xt,
 [s(Ne,
 {
 value: i.value.commentLikeIntervalMin,
 "onUpdate:value": e[5] || (e[5] = a => i.value.commentLikeIntervalMin = a),
 min: 1,
 precision: 0 },
 null,
 8,
 ["value"]),
 e[41] || (e[41] = n("span",
 null,
 "至",
 -1)),
 s(Ne,
 {
 value: i.value.commentLikeIntervalMax,
 "onUpdate:value": e[6] || (e[6] = a => i.value.commentLikeIntervalMax = a),
 min: i.value.commentLikeIntervalMin || 1,
 precision: 0 },
 null,
 8,
 ["value",
 "min"])]),
 e[43] || (e[43] = n("p",
 {
 class: "muted" },
 "真机操作会打开目标视频并定位评论；点击停止会中断后续操作。",
 -1))])])])]),
 n("footer",
 Yt,
 [n("span",
 Zt,
 m(i.value.selectedAccounts.length) + " 个执行账号 · " + m(V(i.value)) + " 条评论 · " + (offlineTools.value ? "离线测试，不执行点赞" : "确认后真实执行（本轮每次一条）"),
 1),
 s(Ae,
 null,
 {
 default: u(() => [s(l,
 {
 onClick: e[7] || (e[7] = a => N.value = !1) },
 {
 default: u(() => [...e[45] || (e[45] = [c("取消",
 -1)])]),
 _: 1 }),
 s(l,
 {
 loading: U.value,
 onClick: e[8] || (e[8] = a => ee(!1)) },
 {
 default: u(() => [...e[46] || (e[46] = [c("保存",
 -1)])]),
 _: 1 },
 8,
 ["loading"]),
 s(l,
 {
 type: "primary",
 loading: U.value,
 onClick: e[9] || (e[9] = a => ee(!0)) },
 {
 default: u(() => [c(offlineTools.value ? "保存并离线测试" : "保存并真机模拟")]),
 _: 1 },
 8,
 ["loading"])]),
 _: 1 })])]),
 _: 1 },
 8,
 ["open",
 "title"]),
 s(Ee,
 {
 open: ne.value,
 "onUpdate:open": e[15] || (e[15] = a => ne.value = a),
 title: `点赞详情 · ${((Ke = K.value) == null ? void 0 : Ke.name) || ""}`,
 width: "min(1100px, 95vw)" },
 {
 default: u(() => [n("div",
 en,
 [(v(!0),
 C(T,
 null,
 De(yt(he).filter(a=>offlineTools.value ? a.key!=="success" : a.key!=="simulated_success"),
 a => (v(),
 C("div",
 {
 key: a.key },
 [n("span",
 null,
 m(a.label),
 1),
 n("strong",
 null,
 m(X.value[a.key] || 0),
 1)]))),
 128))]),
 n("div",
 tn,
 [s(Re,
 {
 value: fe.value,
 "onUpdate:value": e[11] || (e[11] = a => fe.value = a),
 options: Se.filter(a=>offlineTools.value ? a.value!=="success" : a.value!=="simulated_success"),
 style: {
 width: "150px" },
 onChange: be },
 null,
 8,
 ["value"]),
 s(Re,
 {
 value: ge.value,
 "onUpdate:value": e[12] || (e[12] = a => ge.value = a),
 "allow-clear": "",
 placeholder: "执行账号",
 options: nt.value,
 style: {
 width: "200px" },
 onChange: be },
 null,
 8,
 ["value",
 "options"]),
 s(vt,
 {
 checked: ye.value,
 "onUpdate:checked": e[13] || (e[13] = a => ye.value = a),
 onChange: be },
 {
 default: u(() => [...e[48] || (e[48] = [c("包含以往运行",
 -1)])]),
 _: 1 },
 8,
 ["checked"]),
 s(l,
 {
 size: "small",
 onClick: we },
 {
 default: u(() => [...e[49] || (e[49] = [c("刷新",
 -1)])]),
 _: 1 })]),
 s(b,
 {
 "data-source": pe.value,
 columns: at,
 "row-key": lt,
 size: "small",
 loading: Te.value,
 pagination: {
 current: ke.value,
 pageSize: 20,
 total: L.value,
 showSizeChanger: !1 },
 scroll: {
 x: 850 },
 onChange: e[14] || (e[14] = a => {
 ke.value = a.current,
 we();
 }) },
 {
 bodyCell: u(({
 column: a,
 record: d }) => [a.key === "comment" ? (v(),
 C(T,
 {
 key: 0 },
 [n("div",
 null,
 m(d.sample.content || "本地评论记录不可用"),
 1),
 n("div",
 nn,
 m(x(d.sample.kind)) + " · " + m(d.ownerName),
 1)],
 64)) : a.key === "status" ? (v(),
 O(r,
 {
 key: 1,
 color: ["simulated_success","success"].includes(d.status) ? "green" : d.status === "failed" ? "red" : d.status === "uncertain" ? "orange" : "default" },
 {
 default: u(() => [c(m(Ce(d.status)),
 1)]),
 _: 2 },
 1032,
 ["color"])) : a.key === "time" ? (v(),
 C(T,
 {
 key: 2 },
 [c(m(Z(d.updatedAt)),
 1)],
 64)) : a.key === "reason" ? (v(),
 C(T,
 {
 key: 3 },
 [c((d.mode === "live" ? "[真机] " : "[模拟] ") + m(d.reason),
 1)],
 64)) : j("",
 !0)]),
 _: 1 },
 8,
 ["data-source",
 "loading",
 "pagination"]),
 e[50] || (e[50] = n("p",
 {
 class: "muted" },
 "真实结果以提交回执和刷新后的状态为准。已赞不取消，结果不明确不重试；历史离线测试不计入真实统计。",
 -1))]),
 _: 1 },
 8,
 ["open",
 "title"]),
 s(Ee,
 {
 open: Me.value,
 "onUpdate:open": e[16] || (e[16] = a => Me.value = a),
 title: `运行日志 · ${((Be = ue.value) == null ? void 0 : Be.name) || ""}`,
 width: 720 },
 {
 default: u(() => [n("div",
 an,
 [s(l,
 {
 size: "small",
 onClick: ut },
 {
 default: u(() => [...e[51] || (e[51] = [c("一键复制",
 -1)])]),
 _: 1 }),
 s(l,
 {
 size: "small",
 onClick: it },
 {
 default: u(() => [...e[52] || (e[52] = [c("清空日志",
 -1)])]),
 _: 1 })]),
 n("div",
 ln,
 [(v(!0),
 C(T,
 null,
 De(xe.value,
 (a,
 d) => (v(),
 C("div",
 {
 key: d },
 [n("time",
 null,
 m(new Date(a.ts).toLocaleTimeString()),
 1),
 n("span",
 null,
 m(a.msg),
 1)]))),
 128)),
 xe.value.length ? j("",
 !0) : (v(),
 O(Oe,
 {
 key: 0,
 description: "暂无运行日志" }))])]),
 _: 1 },
 8,
 ["open",
 "title"])]);
 };

    } },
 dn = Je(sn,
 [["__scopeId",
 "data-v-979b45ef"]]);

export {
 dn as default };

