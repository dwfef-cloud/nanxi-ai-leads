// JSON-line fixture transport for backend end-to-end offline acceptance.
// Never imports Electron or accesses the system clipboard/network.
const readline = require("node:readline");
const { fixture } = require("./offline_dom_fixture");
const { createOfflineActionExecutor } = require("../action-executor");
const COMMAND_TYPES = { post_comment: "post-comment", reply_comment: "reply-comment",
  dm: "send-dm", favorite: "favorite-video", copy_share_link: "copy-video-link" };
const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

async function main() {
  for await (const line of input) {
    try {
      const item = JSON.parse(line);
      if (!COMMAND_TYPES[item.action]) throw new Error("unsupported fixture action");
      const fake = fixture({ mode: item.action === "dm" ? "dm" : item.action === "reply_comment" ? "reply" : "comment",
        commentId: item.target, recipientId: item.target, ...item.fixture });
      fake.io.accountId = item.account_id;
      const result = await createOfflineActionExecutor(fake.io).execute({
        type: COMMAND_TYPES[item.action], accountId: item.account_id,
        videoUrl: item.action === "reply_comment" ? item.video_url : item.target,
        commentId: item.target, recipientId: item.target, text: item.text || "",
      });
      const latest = (fake.window.__commentReceipts || []).slice(-1)[0];
      const receipt = item.action === "post_comment" || item.action === "reply_comment"
        ? (latest ? { http_status: latest.status, body: JSON.parse(latest.body) } : null)
        : item.action === "dm"
          ? (result.status === "simulated_ok" ? { http_status: 200, body: { status_code: 0,
              message: { id: "fixture-message", recipient_id: item.target, text: item.text } } }
            : result.status === "simulated_fail" ? { http_status: 200, body: { status_code: 1001 } } : null)
        : item.action === "favorite"
          ? (result.status === "simulated_ok" ? { http_status: 200, body: {
              status_code: 0, is_favorite: true } } : null)
          : { copied: result.status === "simulated_ok", url: item.target };
      // The backend must observe the executor's verification decision too. A
      // mismatched receipt alone must never become a successful task action.
      process.stdout.write(JSON.stringify({ execution: result,
        receipt: result.status === "simulated_ok" || result.status === "simulated_fail" ? receipt : null }) + "\n");
    } catch (_) {
      process.stdout.write(JSON.stringify({ execution: { status: "fixture_error" }, receipt: null }) + "\n");
    }
  }
}
main().catch(() => { process.exitCode = 1; });
